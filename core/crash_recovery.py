"""Crash-time recovery for Brahma Evo.

This module is intentionally usable without the GUI. The process supervisor runs it
as a separate OS process so a dead Brahma GUI cannot take the repair path down with it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from core.runtime_paths import (
    CRASH_RECOVERY_LOG_PATH,
    CRASH_RECOVERY_STATE_PATH,
    FATAL_CRASH_LOG_PATH,
    PATCH_HISTORY_PATH,
    CONFIG_DIR,
    PATCH_BACKUPS_DIR,
)

logger = logging.getLogger("CrashRecovery")
BASE_DIR = Path(__file__).resolve().parent.parent
BACKUPS_DIR = PATCH_BACKUPS_DIR
PROTECTED_RECOVERY_FILES = {
    "boot_sentry.py",
    "auto_heal_engine.py",
    "crash_recovery.py",
    "process_supervisor.py",
    "setup.py",
    "requirements.txt",
    "version.txt",
}

MAX_CRASH_AGE_SECONDS = 300.0


def _write_exclusive_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _copy_file_exclusive(source: Path, destination: Path, *, mode: int = 0o600) -> None:
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            fd = -1
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _utc_safe_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    try:
        _write_exclusive_text(tmp, json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _log(message: str) -> None:
    try:
        CRASH_RECOVERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_RECOVERY_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except Exception:
        logger.warning(message)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Persistent recovery state is unreadable or corrupted: {path}") from exc


def _load_state() -> dict[str, Any]:
    raw = _load_json(CRASH_RECOVERY_STATE_PATH, {})
    if not isinstance(raw, dict):
        raise RuntimeError("Crash recovery state has an invalid schema.")
    return raw


def _load_history() -> list[dict[str, Any]]:
    raw = _load_json(PATCH_HISTORY_PATH, [])
    if not isinstance(raw, list):
        raise RuntimeError("Crash recovery patch history has an invalid schema.")
    return raw


def _save_history(history: list[dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _utc_safe_write(PATCH_HISTORY_PATH, history)


def _fingerprint(traceback_text: str) -> str:
    # Tracebacks normally contain stable source paths and exception text; a compact
    # digest lets us rate-limit repeated automatic repair attempts safely.
    normalized = "\n".join(line.rstrip() for line in traceback_text.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:20]


def _recent_patch(crash_time: float) -> dict[str, Any] | None:
    history = _load_history()
    for entry in reversed(history):
        if entry.get("status") not in {"applied", "applied_by_crash_recovery"}:
            continue
        try:
            patch_time = float(entry.get("timestamp"))
        except (TypeError, ValueError):
            continue
        age = crash_time - patch_time
        if 0.0 <= age <= MAX_CRASH_AGE_SECONDS:
            return entry
    return None


def _rollback_entry(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    try:
        target = Path(str(entry.get("target_file") or "")).resolve()
        backup = Path(str(entry.get("backup_path") or "")).resolve()
        target.relative_to(BASE_DIR.resolve())
        backup.relative_to(BACKUPS_DIR.resolve())
    except (OSError, ValueError):
        return {
            "success": False,
            "action": "rollback_failed",
            "message": "Automatic repair rollback paths are outside protected roots.",
            "patch_id": entry.get("patch_id"),
        }
    if target.name in PROTECTED_RECOVERY_FILES:
        return {
            "success": False,
            "action": "rollback_failed",
            "message": "Automatic repair recovery refused to modify a protected safety file.",
            "patch_id": entry.get("patch_id"),
        }
    if not target.is_file() or not backup.is_file():
        return {
            "success": False,
            "action": "rollback_failed",
            "message": "Automatic repair backup or target is missing.",
            "patch_id": entry.get("patch_id"),
        }
    try:
        target_tmp = target.with_name(
            f".{target.name}.rollback-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        try:
            _copy_file_exclusive(backup, target_tmp)
            os.replace(target_tmp, target)
        finally:
            try:
                target_tmp.unlink(missing_ok=True)
            except OSError:
                pass

        entry["status"] = "rolled_back_after_crash"
        entry["rolled_back_at"] = __import__("time").time()
        entry["rollback_reason"] = reason
        history = _load_history()
        for item in reversed(history):
            if item.get("patch_id") == entry.get("patch_id"):
                item.update(entry)
                break
        _save_history(history)
        return {
            "success": True,
            "action": "rolled_back",
            "message": f"Rolled back automatic repair {entry.get('patch_id')} after the repair was followed by another crash.",
            "patch_id": entry.get("patch_id"),
        }
    except Exception as exc:
        return {
            "success": False,
            "action": "rollback_failed",
            "message": f"Automatic repair rollback failed: {exc}",
            "patch_id": entry.get("patch_id"),
        }


def recover_from_crash(*, crash_time: float | None = None) -> dict[str, Any]:
    """Attempt at most one automatic source repair for a unique crash signature.

    Safety behavior:
    * A patch applied immediately before the crash is left for BootSentry to roll back.
    * A repair patch that immediately precedes a repeat crash is rolled back and the
      same crash signature is blocked from further automatic patching.
    * A new crash signature gets one isolated repair attempt.
    """
    if not FATAL_CRASH_LOG_PATH.exists():
        return {"success": False, "action": "no_crash_log", "message": "No fatal crash log is available."}

    try:
        crash_time = float(crash_time if crash_time is not None else FATAL_CRASH_LOG_PATH.stat().st_mtime)
        traceback_text = FATAL_CRASH_LOG_PATH.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:
        return {"success": False, "action": "read_failed", "message": f"Unable to read crash log: {exc}"}

    if not traceback_text:
        return {"success": False, "action": "empty_crash_log", "message": "Fatal crash log is empty."}

    fingerprint = _fingerprint(traceback_text)
    state = _load_state()
    same_crash = state.get("last_crash_fingerprint") == fingerprint
    attempts = int(state.get("repair_attempts", 0) or 0) if same_crash else 0
    blocked = bool(state.get("blocked", False)) if same_crash else False

    patch = _recent_patch(crash_time)
    if patch and patch.get("status") == "applied":
        # BootSentry owns the patch-near-crash rollback contract on the next boot.
        _log(f"Deferred to BootSentry for patch {patch.get('patch_id')} ({patch.get('target_file')}).")
        return {
            "success": False,
            "action": "defer_boot_rollback",
            "message": "A recent source patch preceded the crash; BootSentry will restore its backup before restart.",
            "patch_id": patch.get("patch_id"),
        }

    if patch and patch.get("status") == "applied_by_crash_recovery" and same_crash:
        rollback = _rollback_entry(patch, "The crash repeated immediately after an automatic crash repair.")
        state.update(
            {
                "last_crash_fingerprint": fingerprint,
                "repair_attempts": max(1, attempts),
                "blocked": True,
                "last_action": rollback.get("action"),
            }
        )
        _utc_safe_write(CRASH_RECOVERY_STATE_PATH, state)
        _log(rollback.get("message", "Automatic repair rolled back."))
        return rollback

    if blocked or attempts >= 1:
        message = "Automatic repair is blocked for this repeated crash signature after the previous fix did not hold."
        state.update(
            {
                "last_crash_fingerprint": fingerprint,
                "repair_attempts": attempts,
                "blocked": True,
                "last_action": "blocked",
            }
        )
        _utc_safe_write(CRASH_RECOVERY_STATE_PATH, state)
        _log(message)
        return {"success": False, "action": "blocked", "message": message}

    try:
        from actions.auto_heal_engine import AutoHealEngine

        result = AutoHealEngine.heal_traceback(
            traceback_text,
            context_notes=(
                "This repair was launched by the independent crash supervisor after "
                "the Brahma process exited unexpectedly. Make the smallest safe first-party fix."
            ),
            dry_run=False,
        )
    except Exception as exc:
        result = {"success": False, "message": f"Crash repair worker failed: {exc}"}

    attempts += 1
    state.update(
        {
            "last_crash_fingerprint": fingerprint,
            "repair_attempts": attempts,
            "blocked": False,
            "last_action": "repair_attempted",
        }
    )

    if result.get("success") and result.get("patch_id"):
        history = _load_history()
        for entry in reversed(history):
            if entry.get("patch_id") == result.get("patch_id"):
                entry["status"] = "applied_by_crash_recovery"
                entry["crash_fingerprint"] = fingerprint
                break
        _save_history(history)
        state["last_action"] = "repair_applied"
        _utc_safe_write(CRASH_RECOVERY_STATE_PATH, state)
        message = f"Crash repair applied: {result.get('patch_id')}."
        _log(message)
        return {
            "success": True,
            "action": "repair_applied",
            "message": message,
            "patch_id": result.get("patch_id"),
            "target_file": result.get("target_file"),
        }

    _utc_safe_write(CRASH_RECOVERY_STATE_PATH, state)
    message = str(result.get("message") or "Crash repair could not be applied.")
    _log(message)
    return {"success": False, "action": "repair_failed", "message": message}


if __name__ == "__main__":
    result = recover_from_crash()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get("success") or result.get("action") in {"no_crash_log", "defer_boot_rollback", "blocked", "rolled_back"} else 1)
