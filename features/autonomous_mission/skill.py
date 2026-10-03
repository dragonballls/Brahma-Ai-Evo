from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import psutil

try:
    from core.user_paths import get_user_data_dir
except Exception:
    get_user_data_dir = lambda: Path.home() / ".brahma-evo"

_STATE_DIR = Path(get_user_data_dir()) / "missions"
_STATE_FILE = _STATE_DIR / "missions.json"

_LOCK = threading.RLock()
_MISSIONS: dict[str, dict[str, Any]] = {}

_ACTIVE_STATES = {"pending", "running", "resuming", "cancelling"}
_TERMINAL_STATES = {"completed", "timed_out", "failed", "cancelled"}
_LOCK_STALE_SECONDS = 30.0
_HEARTBEAT_SECONDS = 2.0
_WORKER_STALE_SECONDS = 12.0


def _lock_path() -> Path:
    return Path(str(_STATE_FILE) + ".lock")


@contextmanager
def _file_lock(timeout: float = 15.0):
    lock_path = _lock_path()
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    acquired = False
    while time.monotonic() - started < timeout:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > _LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    if not acquired:
        raise TimeoutError(f"Timed out acquiring mission state lock: {lock_path}")
    try:
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_state_unlocked() -> dict[str, Any]:
    try:
        if not _STATE_FILE.exists():
            return {"missions": {}}
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("missions", {}), dict):
            return data
    except Exception:
        pass
    return {"missions": {}}


def _write_state_unlocked(data: dict[str, Any]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def _load() -> None:
    global _MISSIONS
    try:
        with _file_lock():
            data = _read_state_unlocked()
        saved = data.get("missions", {})
        _MISSIONS = dict(saved) if isinstance(saved, dict) else {}
    except Exception:
        _MISSIONS = {}


def _save() -> None:
    """Compatibility helper for tests and local callers; authoritative writes use _mutate_mission."""
    try:
        with _file_lock():
            _write_state_unlocked({"missions": _MISSIONS})
    except Exception:
        pass


def _refresh() -> dict[str, dict[str, Any]]:
    global _MISSIONS
    try:
        with _file_lock():
            data = _read_state_unlocked()
        saved = data.get("missions", {})
        _MISSIONS = dict(saved) if isinstance(saved, dict) else {}
    except Exception:
        if not _MISSIONS:
            _MISSIONS = {}
    return _MISSIONS


def _mutate_state(mutator: Callable[[dict[str, dict[str, Any]]], Any]) -> Any:
    global _MISSIONS
    with _LOCK:
        with _file_lock():
            data = _read_state_unlocked()
            missions = data.get("missions", {})
            if not isinstance(missions, dict):
                missions = {}
            result = mutator(missions)
            data["missions"] = missions
            _write_state_unlocked(data)
            _MISSIONS = dict(missions)
            return result


def _current_boot_time() -> float:
    try:
        return float(psutil.boot_time())
    except Exception:
        return 0.0


def _process_matches(pid: Any, create_time: Any = None) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0 or pid_int == os.getpid():
        return False
    try:
        proc = psutil.Process(pid_int)
        if not proc.is_running():
            return False
        if create_time not in (None, ""):
            try:
                if abs(float(proc.create_time()) - float(create_time)) > 3.0:
                    return False
            except (TypeError, ValueError, psutil.Error):
                return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _worker_alive(mission: dict[str, Any]) -> bool:
    heartbeat = mission.get("last_heartbeat_at")
    if heartbeat:
        try:
            if time.time() - float(heartbeat) > _WORKER_STALE_SECONDS:
                return False
        except (TypeError, ValueError):
            pass
    return _process_matches(mission.get("worker_pid"), mission.get("worker_create_time"))


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _main_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(_runtime_root() / "main.py"), *args]


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "stdin": subprocess.DEVNULL,
    }


def _worker_log_path() -> Path:
    path = _STATE_DIR / "worker.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _launch_worker_process(mission_id: str) -> tuple[bool, str | None, str]:
    command = _main_command("--mission-worker", mission_id)
    try:
        with _worker_log_path().open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                command,
                cwd=str(_runtime_root()),
                stdout=log,
                stderr=subprocess.STDOUT,
                **_hidden_subprocess_kwargs(),
            )
        return True, str(proc.pid), ""
    except Exception as exc:
        return False, None, str(exc)


def _ensure_recovery_tasks() -> dict[str, Any]:
    """Register hidden Windows recovery triggers once; failures never block the mission itself."""
    if os.name != "nt":
        return {"supported": False, "installed": False, "reason": "Windows Task Scheduler is not required on this OS."}

    try:
        task_names = (
            "Brahma Evo Mission Recovery (Logon)",
            "Brahma Evo Mission Recovery (5m)",
        )
        # Prefer pythonw.exe in source installs so recovery never opens a console window.
        if getattr(sys, "frozen", False):
            interpreter = sys.executable
        else:
            interpreter_path = Path(sys.executable)
            pythonw = interpreter_path.with_name("pythonw.exe")
            interpreter = str(pythonw if pythonw.exists() else interpreter_path)

        supervisor_command = (
            [interpreter, "--mission-supervisor-once"]
            if getattr(sys, "frozen", False)
            else [interpreter, str(_runtime_root() / "main.py"), "--mission-supervisor-once"]
        )
        command = subprocess.list2cmdline(supervisor_command)

        results: dict[str, Any] = {"supported": True, "installed": True, "tasks": []}
        specs = [
            (task_names[0], ["ONLOGON"]),
            (task_names[1], ["MINUTE", "/MO", "5"]),
        ]
        for task_name, trigger_args in specs:
            result = subprocess.run(
                [
                    "schtasks.exe",
                    "/Create",
                    "/TN", task_name,
                    "/SC", trigger_args[0],
                    *trigger_args[1:],
                    "/TR", command,
                    "/RL", "LIMITED",
                    "/F",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                **_hidden_subprocess_kwargs(),
            )
            ok = result.returncode == 0
            results["tasks"].append({
                "name": task_name,
                "installed": ok,
                "returncode": result.returncode,
                "stderr": (result.stderr or "").strip()[-500:],
            })
            if not ok:
                results["installed"] = False
        return results
    except Exception as exc:
        return {"supported": True, "installed": False, "reason": str(exc)}


def _parse_duration(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return min(max(0.0, float(value)), 30 * 86400)
    text = re.sub(r"[,;]", " ", str(value).strip().lower())
    matches = re.findall(
        r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)",
        text,
    )
    if not matches:
        m = re.search(r"\d+(?:\.\d+)?", text)
        return min(max(0.0, float(m.group())), 30 * 86400) if m else None
    factors = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }
    total = sum(float(n) * factors[u] for n, u in matches)
    return min(max(0.0, total), 30 * 86400)


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9_./:%-]+", " ", str(text).lower()).strip()


def _check_completion(mission: dict[str, Any], result: Any) -> bool:
    mode = str(mission.get("completion_type") or ("phrase" if mission.get("until") else "agent_success")).lower()
    result_text = str(result or "")
    norm_result = _normalized(result_text)

    if mode in {"agent_success", "success", "completed"}:
        return bool(result_text) and not any(
            token in norm_result for token in ("task failed", "task aborted", "unknown action", "error:")
        )

    if mode in {"phrase", "until"}:
        target = str(mission.get("completion_target") or mission.get("until") or "").strip()
        if not target:
            return False
        norm_target = _normalized(target)
        if norm_target in norm_result:
            return True
        words = [w for w in norm_target.split() if len(w) > 2]
        return bool(words) and sum(w in norm_result for w in words) / len(words) >= 0.8

    if mode == "regex":
        target = str(mission.get("completion_target") or "").strip()
        try:
            return bool(target) and re.search(target, result_text, flags=re.IGNORECASE | re.DOTALL) is not None
        except re.error:
            return False

    if mode == "file_exists":
        target = mission.get("completion_target")
        return bool(target) and Path(str(target)).expanduser().exists()

    if mode == "process_gone":
        target = str(mission.get("completion_target") or "").strip().lower()
        if not target:
            return False
        for proc in _iter_processes():
            name = proc.get("name", "").lower()
            if target.isdigit() and str(proc.get("pid")) == target:
                return False
            if target == name or target in name:
                return False
        return True

    if mode == "memory_below":
        try:
            threshold = float(mission.get("completion_target"))
            return float(psutil.virtual_memory().percent) <= threshold
        except Exception:
            return False

    return False


def _iter_processes():
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            yield proc.info
        except Exception:
            continue


def launch_worker(mission_id: str) -> str:
    mission_id = str(mission_id or "").strip()
    if not mission_id:
        return "Mission id is required."

    _refresh()

    def reserve(missions: dict[str, dict[str, Any]]):
        mission = missions.get(mission_id)
        if not mission:
            return None
        if mission.get("status") not in _ACTIVE_STATES:
            return {"kind": "terminal", "status": mission.get("status")}
        if _worker_alive(mission):
            return {"kind": "alive", "pid": mission.get("worker_pid")}
        mission["worker_launching_at"] = time.time()
        mission["worker_launch_nonce"] = uuid.uuid4().hex[:12]
        if mission.get("status") == "cancelling" and not mission.get("cancel_requested"):
            mission["cancel_requested"] = True
        return {"kind": "launch"}

    reservation = _mutate_state(reserve)
    if not reservation:
        return f"Mission '{mission_id}' was not found."
    if reservation["kind"] == "terminal":
        return f"Mission {mission_id} is already {reservation['status']}."
    if reservation["kind"] == "alive":
        return f"Mission {mission_id} already has an external worker (PID {reservation['pid']})."

    ok, pid, error = _launch_worker_process(mission_id)

    def commit_launch(missions: dict[str, dict[str, Any]]):
        mission = missions.get(mission_id)
        if not mission:
            return
        mission.pop("worker_launching_at", None)
        mission.pop("worker_launch_nonce", None)
        if ok and pid:
            mission["worker_pid"] = int(pid)
            try:
                mission["worker_create_time"] = psutil.Process(int(pid)).create_time()
            except Exception:
                mission["worker_create_time"] = None
            mission["worker_launch_error"] = ""
        else:
            mission["worker_pid"] = None
            mission["worker_create_time"] = None
            mission["worker_launch_error"] = error
            mission["last_error_at"] = time.time()

    _mutate_state(commit_launch)
    return f"External worker launched for mission {mission_id}." if ok else f"Mission worker launch failed: {error}"


def _finish(mission_id: str, status: str, error: str = "", extra: dict[str, Any] | None = None) -> None:
    def mutate(missions: dict[str, dict[str, Any]]):
        mission = missions.get(mission_id)
        if not mission:
            return
        mission["status"] = status
        mission["finished_at"] = time.time()
        mission["error"] = error
        if extra:
            mission.update(extra)
        mission["worker_pid"] = None
        mission["worker_create_time"] = None
        mission.pop("worker_launching_at", None)
        mission.pop("worker_launch_nonce", None)
    _mutate_state(mutate)


def _mission_loop(mission_id: str) -> None:
    """Compatibility name retained; actual execution is designed for an external worker process."""
    run_worker(mission_id)


def run_worker(mission_id: str) -> int:
    mission_id = str(mission_id or "").strip()
    _refresh()
    mission = dict(_MISSIONS.get(mission_id) or {})
    if not mission:
        return 2
    if mission.get("status") in _TERMINAL_STATES:
        return 0

    now = time.time()
    boot_time = _current_boot_time()
    previous_heartbeat = mission.get("last_heartbeat_at")
    previous_boot = mission.get("worker_boot_time")

    duration_seconds = mission.get("duration_seconds")
    deadline = mission.get("deadline")
    if deadline is None and duration_seconds is not None:
        try:
            origin = float(mission.get("started_at") or mission.get("created_at") or now)
            deadline = origin + float(duration_seconds)
        except (TypeError, ValueError):
            deadline = None

    recovered = bool(mission.get("worker_pid")) and not _process_matches(
        mission.get("worker_pid"), mission.get("worker_create_time")
    )
    recovery_reason = ""
    downtime_seconds = 0.0
    if recovered:
        if previous_boot and boot_time and abs(float(previous_boot) - boot_time) > 1.0:
            recovery_reason = "system reboot"
        else:
            recovery_reason = "Brahma process/worker restart"
        if previous_heartbeat:
            try:
                downtime_seconds = max(0.0, now - float(previous_heartbeat))
            except (TypeError, ValueError):
                downtime_seconds = 0.0

    def claim(missions: dict[str, dict[str, Any]]):
        current = missions.get(mission_id)
        if not current or current.get("status") in _TERMINAL_STATES:
            return False
        current["status"] = "running"
        current["worker_pid"] = os.getpid()
        try:
            current["worker_create_time"] = psutil.Process(os.getpid()).create_time()
        except Exception:
            current["worker_create_time"] = None
        current["worker_boot_time"] = boot_time
        current["last_heartbeat_at"] = now
        current["deadline"] = deadline
        current["worker_started_at"] = now
        current["cancel_requested"] = bool(current.get("cancel_requested", False))
        if recovered or recovery_reason:
            current["recovery_count"] = int(current.get("recovery_count") or 0) + 1
            current["last_recovery_at"] = now
            current["last_recovery_reason"] = recovery_reason or "worker recovery"
            current["last_downtime_seconds"] = round(downtime_seconds, 2)
        else:
            current.setdefault("recovery_count", 0)
        return True

    if not _mutate_state(claim):
        return 0

    from agent.executor import AgentExecutor

    cancel_event = threading.Event()
    stop_heartbeat = threading.Event()

    def heartbeat_loop():
        while not stop_heartbeat.wait(_HEARTBEAT_SECONDS):
            current = status(mission_id)
            if not current.get("success", True):
                return
            current_status = str(current.get("status") or "")
            if current_status == "cancelling" or current.get("cancel_requested"):
                cancel_event.set()
            current_deadline = current.get("deadline")
            if current_deadline:
                try:
                    if time.time() >= float(current_deadline):
                        cancel_event.set()
                except (TypeError, ValueError):
                    pass

            def beat(missions: dict[str, dict[str, Any]]):
                item = missions.get(mission_id)
                if item and item.get("worker_pid") == os.getpid():
                    item["last_heartbeat_at"] = time.time()

            try:
                _mutate_state(beat)
            except Exception:
                pass

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True, name=f"BrahmaMissionHeartbeat-{mission_id}")
    heartbeat_thread.start()

    executor = AgentExecutor()

    try:
        while True:
            current = status(mission_id)
            if not current.get("success", True):
                return 2

            current_status = str(current.get("status") or "")
            if current_status in _TERMINAL_STATES:
                return 0
            if current_status == "cancelling" or current.get("cancel_requested"):
                _finish(mission_id, "cancelled", "Mission cancellation requested.")
                return 0

            current_deadline = current.get("deadline")
            if current_deadline is not None:
                try:
                    if time.time() >= float(current_deadline):
                        _finish(mission_id, "timed_out", "Mission time limit expired.")
                        return 0
                except (TypeError, ValueError):
                    pass

            iteration = int(current.get("iterations") or 0) + 1
            def start_iteration(missions: dict[str, dict[str, Any]]):
                item = missions.get(mission_id)
                if not item:
                    return
                item["iterations"] = iteration
                item["last_started_at"] = time.time()
                item["last_heartbeat_at"] = time.time()

            _mutate_state(start_iteration)

            context = (
                f"This is autonomous mission iteration {iteration}. "
                "Continue making concrete progress on the original goal. "
                "Review previous results, avoid repeating completed work, verify each claimed change, "
                "and stop only when the completion condition is actually satisfied."
            )
            previous = current.get("last_result")
            if previous:
                context += f"\nPrevious iteration result:\n{str(previous)[-3000:]}"
            goal = str(current.get("goal") or "")
            task_goal = f"{goal}\n\nAUTONOMOUS EXECUTION DIRECTIVE:\n{context}"

            try:
                result = executor.execute(goal=task_goal, cancel_flag=cancel_event, player=None)
                finished_now = time.time()
                current_after = status(mission_id).get("status") or ""
                cancellation_requested = cancel_event.is_set() or current_after == "cancelling" or status(mission_id).get("cancel_requested")
                deadline_after = status(mission_id).get("deadline")
                time_limit_reached = False
                if deadline_after is not None:
                    try:
                        time_limit_reached = finished_now >= float(deadline_after)
                    except (TypeError, ValueError):
                        pass
                complete = _check_completion(current, result)
                keep_working = bool(current.get("keep_working"))

                def save_result(missions: dict[str, dict[str, Any]]):
                    item = missions.get(mission_id)
                    if not item:
                        return
                    item["last_result"] = result
                    item["last_finished_at"] = finished_now
                    item["last_heartbeat_at"] = finished_now
                    item["error"] = ""
                    if cancellation_requested:
                        item["status"] = "cancelled"
                        item["finished_at"] = finished_now
                    elif time_limit_reached:
                        item["status"] = "timed_out"
                        item["finished_at"] = finished_now
                    elif complete and not keep_working:
                        item["status"] = "completed"
                        item["finished_at"] = finished_now
                    else:
                        item["status"] = "running"
                        if item.get("until") and not complete:
                            item["keep_working"] = True

                _mutate_state(save_result)
                if cancellation_requested or time_limit_reached or (complete and not keep_working):
                    return 0

            except Exception as exc:
                failure_now = time.time()
                current_now = status(mission_id)
                cancellation_requested = cancel_event.is_set() or current_now.get("status") == "cancelling" or current_now.get("cancel_requested")
                deadline_now = current_now.get("deadline")
                timed_out = False
                if deadline_now is not None:
                    try:
                        timed_out = failure_now >= float(deadline_now)
                    except (TypeError, ValueError):
                        pass

                def save_error(missions: dict[str, dict[str, Any]]):
                    item = missions.get(mission_id)
                    if not item:
                        return
                    item["error"] = str(exc)
                    item["last_finished_at"] = failure_now
                    item["last_heartbeat_at"] = failure_now
                    if cancellation_requested:
                        item["status"] = "cancelled"
                        item["finished_at"] = failure_now
                    elif timed_out:
                        item["status"] = "timed_out"
                        item["finished_at"] = failure_now
                    else:
                        item["status"] = "running"

                _mutate_state(save_error)
                if cancellation_requested or timed_out:
                    return 0

            current = status(mission_id)
            interval = max(5.0, min(float(current.get("interval_seconds") or 30), 3600.0))
            wait_for = interval
            deadline = current.get("deadline")
            if deadline is not None:
                try:
                    wait_for = min(wait_for, max(0.0, float(deadline) - time.time()))
                except (TypeError, ValueError):
                    pass
            if wait_for <= 0:
                continue
            if cancel_event.wait(timeout=wait_for):
                continue
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2.0)
        # Clear the process lease only when this worker still owns the mission.
        def release(missions: dict[str, dict[str, Any]]):
            item = missions.get(mission_id)
            if item and item.get("worker_pid") == os.getpid():
                if item.get("status") not in _TERMINAL_STATES:
                    item["status"] = "running" if not item.get("cancel_requested") else "cancelling"
                item["last_heartbeat_at"] = time.time()
                item["worker_pid"] = None
                item["worker_create_time"] = None
        try:
            _mutate_state(release)
        except Exception:
            pass


def start_mission(
    goal: str,
    duration: Any = None,
    until: str | None = None,
    completion_type: str | None = None,
    completion_target: Any = None,
    interval_seconds: int = 30,
    keep_working: bool | None = None,
) -> str:
    goal = str(goal or "").strip()
    if not goal:
        return "Autonomous mission requires a goal."

    duration_seconds = _parse_duration(duration)
    if duration is not None and duration_seconds is None:
        return "I could not parse the requested duration."

    now = time.time()
    mission_id = "mission-" + uuid.uuid4().hex[:8]
    mission = {
        "mission_id": mission_id,
        "goal": goal,
        "status": "pending",
        "created_at": now,
        "duration_seconds": duration_seconds,
        "deadline": now + duration_seconds if duration_seconds is not None else None,
        "until": until,
        "completion_type": completion_type or ("phrase" if until else "agent_success"),
        "completion_target": completion_target,
        "interval_seconds": max(5, min(int(interval_seconds), 3600)),
        "keep_working": bool(keep_working) if keep_working is not None else (duration_seconds is not None and not until),
        "iterations": 0,
        "last_result": "",
        "error": "",
        "worker_pid": None,
        "worker_create_time": None,
        "worker_boot_time": None,
        "last_heartbeat_at": None,
        "recovery_count": 0,
        "cancel_requested": False,
    }

    _mutate_state(lambda missions: missions.__setitem__(mission_id, mission))
    recovery = _ensure_recovery_tasks()
    launch = launch_worker(mission_id)

    _mutate_state(
        lambda missions: missions[mission_id].update({
            "recovery_registration": recovery,
            "worker_launch_result": launch,
        })
    )

    return (
        f"Started autonomous mission {mission_id} in an external worker. "
        f"It can continue after Brahma restarts and is scheduled for recovery after Windows logon/reboot."
    )


def _ensure_loaded() -> None:
    _refresh()


def list_missions() -> list[dict[str, Any]]:
    _refresh()
    with _LOCK:
        return [dict(m) for m in sorted(_MISSIONS.values(), key=lambda x: x.get("created_at", 0), reverse=True)]


def status(mission_id: str) -> dict[str, Any]:
    _refresh()
    with _LOCK:
        mission = _MISSIONS.get(mission_id)
        if not mission:
            return {"success": False, "message": f"Mission '{mission_id}' was not found."}
        result = dict(mission)
        result["worker_alive"] = _worker_alive(mission)
        return result


def cancel(mission_id: str) -> str:
    mission_id = str(mission_id or "").strip()

    def request_cancel(missions: dict[str, dict[str, Any]]):
        mission = missions.get(mission_id)
        if not mission:
            return None
        if mission.get("status") in _TERMINAL_STATES:
            return {"kind": "terminal", "status": mission.get("status")}
        mission["cancel_requested"] = True
        mission["status"] = "cancelling"
        mission["cancel_requested_at"] = time.time()
        return {"kind": "cancel"}

    result = _mutate_state(request_cancel)
    if not result:
        return f"Mission '{mission_id}' was not found."
    if result["kind"] == "terminal":
        return f"Mission {mission_id} is already {result['status']}."

    # If the worker disappeared, finalize cancellation immediately; otherwise it will
    # observe the durable flag and exit cleanly.
    current = status(mission_id)
    if not current.get("worker_alive"):
        _finish(mission_id, "cancelled", "Mission cancellation requested while no worker was active.")
        return f"Mission {mission_id} cancelled."

    return f"Cancellation requested for mission {mission_id}."


def recover_active_missions() -> dict[str, Any]:
    _refresh()
    launched: list[str] = []
    timed_out: list[str] = []
    cancelled: list[str] = []

    for mission in list(_MISSIONS.values()):
        mission_id = str(mission.get("mission_id") or "")
        state = str(mission.get("status") or "")
        if state not in _ACTIVE_STATES or not mission_id:
            continue

        deadline = mission.get("deadline")
        if deadline is not None:
            try:
                if time.time() >= float(deadline):
                    _finish(mission_id, "timed_out", "Mission time limit expired while Brahma was offline.")
                    timed_out.append(mission_id)
                    continue
            except (TypeError, ValueError):
                pass

        if mission.get("cancel_requested"):
            current = status(mission_id)
            if not current.get("worker_alive"):
                _finish(mission_id, "cancelled", "Mission cancellation completed during recovery.")
                cancelled.append(mission_id)
                continue

        if not _worker_alive(mission):
            launch_worker(mission_id)
            launched.append(mission_id)

    return {
        "launched": launched,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "checked_at": time.time(),
    }


def execute(**kwargs: Any) -> dict[str, Any]:
    action = str(kwargs.get("action", "start")).strip().lower()
    if action == "start":
        mission_id = start_mission(
            goal=str(kwargs.get("goal") or kwargs.get("query") or ""),
            duration=kwargs.get("duration"),
            until=kwargs.get("until"),
            completion_type=kwargs.get("completion_type"),
            completion_target=kwargs.get("completion_target"),
            interval_seconds=int(kwargs.get("interval_seconds", 30)),
            keep_working=kwargs.get("keep_working"),
        )
        return {"summary": mission_id}

    if action == "list":
        return {"summary": "Autonomous mission list.", "missions": list_missions()}

    if action == "recover":
        return {"summary": "Autonomous mission recovery pass complete.", "recovery": recover_active_missions()}

    mission_id = str(kwargs.get("mission_id") or "").strip()
    if action == "status":
        return {"summary": f"Mission status for {mission_id}.", "status": status(mission_id)}
    if action == "cancel":
        return {"summary": cancel(mission_id)}
    return {"summary": f"Unknown autonomous mission action: {action}"}
