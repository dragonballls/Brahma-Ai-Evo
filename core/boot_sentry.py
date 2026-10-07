"""
Boot Sentry for Brahma AI
Runs at absolute startup before any heavy modules or UI to guarantee boot resilience.
Detects if the previous session crashed right after an auto-patch, and safely rolls back.
"""

import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

from core.runtime_paths import PATCH_HISTORY_PATH, FATAL_CRASH_LOG_PATH, PATCH_BACKUPS_DIR

logger = logging.getLogger("BootSentry")

BASE_DIR = Path(__file__).resolve().parent.parent
CRASH_LOG = FATAL_CRASH_LOG_PATH
PATCH_HISTORY_FILE = PATCH_HISTORY_PATH
BACKUPS_DIR = PATCH_BACKUPS_DIR
PROTECTED_BOOT_FILES = {
    "boot_sentry.py",
    "auto_heal_engine.py",
    "crash_recovery.py",
    "process_supervisor.py",
    "setup.py",
    "requirements.txt",
    "version.txt",
}


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


def mark_startup_healthy() -> None:
    """Clear the previous crash marker once Brahma reaches a healthy UI state."""
    try:
        if CRASH_LOG.exists():
            CRASH_LOG.unlink()
            logger.info("Startup marked healthy; cleared stale fatal crash marker.")
    except Exception as exc:
        logger.warning("Unable to clear stale fatal crash marker: %s", exc)


def check_and_recover_on_boot() -> bool:
    """
    Checks if a crash log exists. If a recent patch was applied within the last 5 minutes
    of the crash, automatically rolls back to the backup file to unbrick the system.
    Returns True if a recovery rollback was performed.
    """
    if not CRASH_LOG.exists() or not PATCH_HISTORY_FILE.exists():
        return False

    try:
        with open(PATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return False

    if not history or not isinstance(history, list):
        return False

    # Only recover from a patch that was actually applied immediately before
    # the recorded crash. Older successful patches must never be reverted because
    # of an unrelated crash weeks later.
    try:
        crash_time = CRASH_LOG.stat().st_mtime
    except OSError:
        return False

    last_patch = None
    for entry in reversed(history):
        if entry.get("status") != "applied" or not entry.get("backup_path"):
            continue
        try:
            patch_time = float(entry.get("timestamp"))
        except (TypeError, ValueError):
            continue
        age = crash_time - patch_time
        if 0.0 <= age <= 300.0:
            last_patch = entry
            break

    if last_patch is None:
        return False

    try:
        target_file = Path(last_patch.get("target_file", "")).resolve()
        backup_file = Path(last_patch.get("backup_path", "")).resolve()
        target_file.relative_to(BASE_DIR.resolve())
        backup_file.relative_to(BACKUPS_DIR.resolve())
    except (OSError, ValueError):
        logger.warning("Refusing boot rollback outside protected roots.")
        return False

    if target_file.name in PROTECTED_BOOT_FILES:
        logger.warning("Refusing boot rollback of protected safety file: %s", target_file.name)
        return False

    if not backup_file.is_file() or not target_file.is_file():
        return False

    print(f"[BootSentry] ⚠️ Fatal crash detected after patch '{last_patch.get('patch_id')}'.")
    print(f"[BootSentry] 🛡️ Initiating automatic rollback of '{target_file.name}' from backup...")

    try:
        target_tmp = target_file.with_name(
            f".{target_file.name}.rollback-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        try:
            _copy_file_exclusive(backup_file, target_tmp)
            os.replace(target_tmp, target_file)
        finally:
            try:
                target_tmp.unlink(missing_ok=True)
            except OSError:
                pass

        last_patch["status"] = "rolled_back_on_boot"
        last_patch["rollback_reason"] = "App crashed on startup after patch."

        history_tmp = PATCH_HISTORY_FILE.with_name(
            f".{PATCH_HISTORY_FILE.name}.tmp-{os.getpid()}"
        )
        try:
            _write_exclusive_text(history_tmp, json.dumps(history, indent=4, ensure_ascii=False))
            os.replace(history_tmp, PATCH_HISTORY_FILE)
        finally:
            try:
                history_tmp.unlink(missing_ok=True)
            except OSError:
                pass

        # Archive the crash log without allowing archive-name collisions to
        # turn a successful rollback into a reported failure.
        if CRASH_LOG.exists():
            crash_archive = CRASH_LOG.with_name(
                f"FATAL_CRASH_RECOVERED-{int(crash_time)}.log"
            )
            if crash_archive.exists():
                crash_archive = CRASH_LOG.with_name(
                    f"FATAL_CRASH_RECOVERED-{int(crash_time)}-{os.getpid()}.log"
                )
            try:
                shutil.move(str(CRASH_LOG), str(crash_archive))
            except OSError as archive_exc:
                logger.warning("Rollback succeeded but crash-log archiving failed: %s", archive_exc)

        print(f"[BootSentry] ✅ Successfully restored '{target_file.name}'! Brahma AI recovered.")
        return True
    except Exception as e:
        print(f"[BootSentry] ❌ Recovery failed: {e}")
        return False


# Automatically execute recovery check on import
check_and_recover_on_boot()
