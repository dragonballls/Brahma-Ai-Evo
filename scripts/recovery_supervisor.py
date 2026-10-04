"""External recovery supervisor for Brahma Evo.

This process deliberately lives outside the Brahma application process. It watches the
main app, records crashes, invokes safe recovery when a traceback is available, and
restarts the app with backoff. A Brahma crash therefore does not take the recovery
mechanism down with it.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from core.runtime_paths import CONFIG_DIR, FATAL_CRASH_LOG_PATH, LOG_DIR
except Exception:
    # Keep launcher diagnostics alive even during a damaged import path.
    _here = Path(__file__).resolve()
    _root = _here.parent.parent if _here.name == "recovery_supervisor.py" else Path(sys.executable).resolve().parent
    _local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    CONFIG_DIR = _local / "Brahma_Evo" / "config"
    LOG_DIR = _local / "Brahma_Evo" / "logs"
    FATAL_CRASH_LOG_PATH = LOG_DIR / "FATAL_CRASH.log"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = CONFIG_DIR / "recovery_supervisor.json"
LOG_PATH = LOG_DIR / "recovery_supervisor.log"

MAX_HEAL_ATTEMPTS_PER_CRASH = 2
RAPID_RESTART_WINDOW_SECONDS = 600.0
MAX_RAPID_RESTARTS = 6
STABLE_RUNTIME_SECONDS = 90.0
BACKOFF_SECONDS = (2.0, 5.0, 15.0, 30.0, 60.0, 120.0)

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("BrahmaRecoverySupervisor")


def _root_dir() -> Path:
    override = os.environ.get("BRAHMA_RUNTIME_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root_dir()


def _acquire_single_instance() -> Any:
    """Use a named Windows mutex so two supervisors cannot fight over one app."""
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, "Local\\Brahma-Evo-Recovery-Supervisor.v1")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return (kernel32, handle)
    except Exception:
        logger.exception("Unable to create supervisor mutex; continuing without it.")
        return True


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception:
        logger.exception("Could not persist supervisor state.")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _read_fatal_traceback() -> str:
    try:
        return FATAL_CRASH_LOG_PATH.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def _fingerprint(tb: str) -> str:
    try:
        from actions.auto_heal_engine import AutoHealEngine
        return AutoHealEngine._error_fingerprint(tb)
    except Exception:
        import hashlib
        return hashlib.sha256((tb or "").encode("utf-8", "replace")).hexdigest()[:20]


def _run_recovery(traceback_text: str, state: dict[str, Any]) -> dict[str, Any]:
    """Recover outside the crashed process. Prefer rollback, then one bounded auto-heal."""
    result: dict[str, Any] = {"success": False, "action": "none"}

    try:
        from core.boot_sentry import check_and_recover_on_boot
        if check_and_recover_on_boot():
            result.update(success=True, action="rollback", message="Recent auto-patch rolled back.")
            return result
    except Exception as exc:
        logger.warning("BootSentry recovery failed: %s", exc)

    tb = (traceback_text or "").strip()
    if not tb:
        result["message"] = "No fatal traceback was recorded; restart-only recovery will be used."
        return result

    fingerprint = _fingerprint(tb)
    previous_fp = str(state.get("heal_fingerprint") or "")
    attempts = int(state.get("heal_attempts", 0) or 0)
    if fingerprint != previous_fp:
        attempts = 0
    if attempts >= MAX_HEAL_ATTEMPTS_PER_CRASH:
        result["message"] = "Healing retry limit reached for this crash fingerprint."
        return result

    # A frozen EXE executes embedded bytecode. Do not pretend that patching bundled
    # .py data changes the running executable; restart remains the safe packaged path.
    if getattr(sys, "frozen", False):
        result["message"] = "Frozen runtime: safe restart enabled; source-level auto-heal is deferred to source/update mode."
        state.update(heal_fingerprint=fingerprint, heal_attempts=attempts + 1)
        _save_state(state)
        return result

    try:
        from actions.auto_heal_engine import AutoHealEngine
        heal = AutoHealEngine.heal_traceback(
            tb,
            context_notes=(
                "Recovery supervisor invoked this repair after the Brahma process crashed. "
                "Prefer the smallest first-party fix. Preserve existing interfaces and avoid "
                "touching the recovery supervisor, BootSentry, packaging, or credential infrastructure."
            ),
        )
        state.update(heal_fingerprint=fingerprint, heal_attempts=attempts + 1)
        _save_state(state)
        result.update(heal)
        result["success"] = bool(heal.get("success"))
        result["action"] = "auto_heal"
        return result
    except Exception as exc:
        logger.exception("External auto-heal attempt failed.")
        result["message"] = str(exc)
        return result


def _child_command() -> list[str]:
    if getattr(sys, "frozen", False):
        app = ROOT / "BrahmaEvo.exe"
        if not app.exists():
            raise FileNotFoundError(f"Packaged Brahma executable not found: {app}")
        return [str(app), "--startup"]
    app = ROOT / "main.py"
    if not app.exists():
        raise FileNotFoundError(f"Brahma main.py not found: {app}")
    return [str(sys.executable), str(app), "--startup"]


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["BRAHMA_RUNTIME_ROOT"] = str(ROOT)
    return env


def _start_child() -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "env": _child_environment(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["startupinfo"] = subprocess.STARTUPINFO()
        kwargs["startupinfo"].dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"].wShowWindow = 0
    return subprocess.Popen(_child_command(), **kwargs)


def run_supervisor() -> int:
    singleton = _acquire_single_instance()
    if singleton is None:
        logger.info("Duplicate recovery supervisor launch ignored.")
        return 0

    logger.info("Recovery supervisor started; root=%s frozen=%s", ROOT, getattr(sys, "frozen", False))
    state = _load_state()
    recent_restarts = [
        float(x) for x in state.get("recent_restarts", [])
        if isinstance(x, (int, float)) and time.time() - float(x) <= RAPID_RESTART_WINDOW_SECONDS
    ]

    while True:
        try:
            started = time.time()
            child = _start_child()
            pid = child.pid
            logger.info("Started Brahma child pid=%s", pid)
            state["last_child_pid"] = pid
            _save_state(state)
            exit_code = child.wait()
            runtime = time.time() - started
            logger.warning("Brahma child exited pid=%s code=%s runtime=%.1fs", pid, exit_code, runtime)

            if exit_code == 0:
                logger.info("Brahma exited normally; supervisor is shutting down.")
                state["recent_restarts"] = []
                state["heal_fingerprint"] = ""
                state["heal_attempts"] = 0
                _save_state(state)
                return 0

            now = time.time()
            recent_restarts = [x for x in recent_restarts if now - x <= RAPID_RESTART_WINDOW_SECONDS]
            recent_restarts.append(now)
            state["recent_restarts"] = recent_restarts
            _save_state(state)

            tb = _read_fatal_traceback()
            recovery = _run_recovery(tb, state)
            logger.warning("Crash recovery result: %s", recovery)

            if runtime >= STABLE_RUNTIME_SECONDS:
                # A long healthy run resets the crash-loop pressure, while retaining
                # the last crash fingerprint/attempts as a guard against repeated bad fixes.
                recent_restarts = [now]
                state["recent_restarts"] = recent_restarts
                _save_state(state)

            if len(recent_restarts) > MAX_RAPID_RESTARTS:
                delay = BACKOFF_SECONDS[-1]
                logger.error(
                    "Brahma entered a crash loop (%s restarts in %.0fs); keeping supervisor alive and backing off %.0fs.",
                    len(recent_restarts), RAPID_RESTART_WINDOW_SECONDS, delay,
                )
            else:
                idx = min(len(recent_restarts) - 1, len(BACKOFF_SECONDS) - 1)
                delay = BACKOFF_SECONDS[max(0, idx)]
            time.sleep(delay)

        except KeyboardInterrupt:
            logger.info("Recovery supervisor interrupted.")
            return 0
        except Exception:
            logger.exception("Supervisor loop failure; keeping recovery process alive.")
            time.sleep(BACKOFF_SECONDS[-1])


if __name__ == "__main__":
    raise SystemExit(run_supervisor())
