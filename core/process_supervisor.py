"""Independent process supervisor for Brahma Evo.

The supervisor stays outside the main GUI process. It launches Brahma, records
unexpected exits, runs crash repair in a separate process, and restarts Brahma.
It is intentionally headless and low overhead so it can remain alive while the
GUI, voice stack, or LLM client is being repaired.
"""
from __future__ import annotations

import json
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
        return [str(root / "BrahmaEvo.exe"), "--recover-crash"]
    interpreter = Path(sys.executable)
    return [str(interpreter), str(root / "core" / "crash_recovery.py"), "--recover-crash"]


def _test_ready_marker(root: Path) -> Path:
    return root / ".brahma-ci-ready"


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
            env={**os.environ, "BRAHMA_CRASH_RECOVERY": "1"},
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
            marker = _test_ready_marker(root)
            test_mode = os.environ.get("BRAHMA_EVO_TEST_MODE", "").strip() == "1"
            if test_mode:
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    pass
            child_start = time.time()
            _log(f"Launching Brahma: {' '.join(command)}")
            child_log = None
            try:
                child_stdout = subprocess.DEVNULL
                if test_mode:
                    smoke_log_path = root / ".brahma-ci-child.log"
                    try:
                        smoke_log_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    child_log = smoke_log_path.open("w", encoding="utf-8", errors="replace")
                    child_stdout = child_log

                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=child_stdout,
                    stderr=subprocess.STDOUT if test_mode else subprocess.DEVNULL,
                    env={**os.environ, "BRAHMA_SUPERVISED": "1"},
                    creationflags=_hidden_creationflags(),
                )
                if test_mode:
                    try:
                        smoke_timeout = max(
                            10.0,
                            float(os.environ.get("BRAHMA_EVO_TEST_SUPERVISOR_TIMEOUT", "30")),
                        )
                    except (TypeError, ValueError):
                        smoke_timeout = 30.0
                    try:
                        exit_code = process.wait(timeout=smoke_timeout)
                    except subprocess.TimeoutExpired:
                        ready = False
                        try:
                            ready = marker.exists() and marker.stat().st_mtime >= (child_start - 2.0)
                        except OSError:
                            ready = False
                        try:
                            process.kill()
                            process.wait(timeout=10)
                        except Exception:
                            pass
                        if ready:
                            _log("Packaged smoke test reached Brahma startup-ready marker; forced child cleanup.")
                            return 0
                        try:
                            if child_log is not None:
                                child_log.flush()
                                output = smoke_log_path.read_text(encoding="utf-8", errors="replace")
                                if output.strip():
                                    _log("Packaged child output (tail): " + output[-12000:])
                        except Exception as log_exc:
                            _log(f"Packaged child output read failed: {log_exc}")
                        _log("Packaged smoke test timed out before startup-ready marker.")
                        return 1
                else:
                    exit_code = process.wait()
            except Exception as exc:
                exit_code = 9009
                _log(f"Unable to start Brahma: {exc}")

            if exit_code == 0:
                if test_mode:
                    try:
                        ready = marker.exists() and marker.stat().st_mtime >= (child_start - 2.0)
                    except OSError:
                        ready = False
                    if not ready:
                        _log("Packaged smoke test child exited cleanly without startup-ready marker.")
                        return 1
                    _log("Brahma reached startup-ready marker and exited cleanly.")
                    return 0
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
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("success") or result.get("action") in {
            "no_crash_log",
            "defer_boot_rollback",
            "blocked",
            "rolled_back",
        } else 1

    return supervise()


if __name__ == "__main__":
    raise SystemExit(main())
