"""Independent process supervisor for Brahma Evo.

The supervisor stays outside the main GUI process. It launches Brahma, records
unexpected exits, runs crash repair in a separate process, and restarts Brahma.
It is intentionally headless and low overhead so it can remain alive while the
GUI, voice stack, or LLM client is being repaired.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from core.runtime_paths import (
    FATAL_CRASH_LOG_PATH,
    SUPERVISOR_LOG_PATH,
)
from core.single_instance import SingleInstance

RESTART_DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
SUPERVISOR_MUTEX = "Local\\Brahma-Ai-Evo.Supervisor.v1"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _hidden_creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def application_command(base_dir: Path | None = None) -> list[str]:
    root = Path(base_dir or _base_dir()).resolve()

    if getattr(sys, "frozen", False):
        executable = root / "BrahmaEvo.exe"
        return [str(executable), "--startup"]

    venv_candidates = (
        root / ".venv" / "Scripts" / "pythonw.exe",
        root / ".venv" / "Scripts" / "python.exe",
    )
    interpreter = next((p for p in venv_candidates if p.exists()), None)
    if interpreter is None:
        interpreter = Path(sys.executable)
    return [str(interpreter), str(root / "main.py"), "--startup"]


def recovery_command(base_dir: Path | None = None) -> list[str]:
    root = Path(base_dir or _base_dir()).resolve()
    if getattr(sys, "frozen", False):
        return [sys.executable, "--recover-crash"]
    interpreter = Path(sys.executable)
    return [str(interpreter), str(root / "core" / "process_supervisor.py"), "--recover-crash"]


def _log(message: str) -> None:
    try:
        SUPERVISOR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SUPERVISOR_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def _fresh_crash_log(start_time: float) -> bool:
    try:
        # main.py overwrites the fatal log on an unhandled top-level exception.
        return FATAL_CRASH_LOG_PATH.exists() and FATAL_CRASH_LOG_PATH.stat().st_mtime >= (start_time - 2.0)
    except OSError:
        return False


def _run_recovery(root: Path, child_start: float) -> None:
    if not _fresh_crash_log(child_start):
        _log("No fresh fatal crash log; restarting without source repair.")
        return

    command = recovery_command(root)
    _log(f"Starting isolated crash-recovery worker: {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
            env={**os.environ, "BRAHMA_CRASH_RECOVERY=1"},
            creationflags=_hidden_creationflags(),
            check=False,
        )
        output = (result.stdout or "").strip().replace("\x00", "")
        if output:
            _log("Crash-recovery worker: " + output[-4000:])
        _log(f"Crash-recovery worker exited with code {result.returncode}.")
    except Exception as exc:
        _log(f"Crash-recovery worker launch failed: {exc}")


def supervise(base_dir: Path | None = None, *, max_cycles: int | None = None) -> int:
    root = Path(base_dir or _base_dir()).resolve()
    guard = SingleInstance(SUPERVISOR_MUTEX)
    if not guard.acquire():
        _log("Duplicate supervisor launch ignored.")
        return 0

    _log(f"Supervisor online for {root}")
    failures = 0
    cycles = 0

    try:
        while True:
            cycles += 1
            if max_cycles is not None and cycles > max_cycles:
                return 0

            command = application_command(root)
            child_start = time.time()
            _log(f"Launching Brahma: {' '.join(command)}")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "BRAHMA_SUPERVISED": "1"},
                    creationflags=_hidden_creationflags(),
                )
                exit_code = process.wait()
            except Exception as exc:
                exit_code = 9009
                _log(f"Unable to start Brahma: {exc}")

            if exit_code == 0:
                _log("Brahma exited cleanly; supervisor stopping.")
                return 0

            failures += 1
            _log(f"Brahma exited unexpectedly with code {exit_code}; failure #{failures}.")
            _run_recovery(root, child_start)

            delay = RESTART_DELAYS[min(failures - 1, len(RESTART_DELAYS) - 1)]
            _log(f"Restarting Brahma after {delay:.1f}s.")
            time.sleep(delay)
    finally:
        guard.release()


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--recover-crash":
        from core.crash_recovery import recover_from_crash

        result = recover_from_crash()
        print(__import__("json").dumps(result, ensure_ascii=False))
        return 0 if result.get("success") or result.get("action") in {
            "no_crash_log",
            "defer_boot_rollback",
            "blocked",
            "rolled_back",
        } else 1

    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
