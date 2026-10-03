from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

import psutil
from typing import Any

try:
    from core.user_paths import get_user_data_dir
except Exception:
    get_user_data_dir = lambda: Path.home() / ".brahma-evo"

_STATE_DIR = Path(get_user_data_dir()) / "missions"
_STATE_FILE = _STATE_DIR / "missions.json"

_LOCK = threading.RLock()
_MISSIONS: dict[str, dict[str, Any]] = {}
_EVENTS: dict[str, threading.Event] = {}
_THREADS: dict[str, threading.Thread] = {}


def _load() -> None:
    global _MISSIONS
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8")) if _STATE_FILE.exists() else {}
        saved = data.get("missions", {})
        if isinstance(saved, dict):
            _MISSIONS = saved
            # Daemon mission workers do not survive an application restart.
            for mission in _MISSIONS.values():
                if mission.get("status") in {"running", "pending", "cancelling"}:
                    mission["status"] = "interrupted"
                    mission["error"] = "Brahma Evo restarted before this mission finished."
            _save()
    except Exception:
        _MISSIONS = {}


def _save() -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"missions": _MISSIONS}, indent=2, default=str), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:
        pass


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


def _check_completion(
    mission: dict[str, Any],
    result: Any,
) -> bool:
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
        return bool(mission.get("completion_target")) and Path(str(mission["completion_target"])).expanduser().exists()

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
    import psutil
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            yield proc.info
        except Exception:
            continue


def _mission_loop(mission_id: str) -> None:
    with _LOCK:
        mission = _MISSIONS[mission_id]
    event = _EVENTS[mission_id]

    from agent.executor import AgentExecutor

    interval = max(5.0, min(float(mission.get("interval_seconds") or 30), 3600.0))
    duration = mission.get("duration_seconds")
    deadline = (time.time() + float(duration)) if duration is not None else None
    keep_working = bool(mission.get("keep_working", duration is not None and not mission.get("until")))

    with _LOCK:
        mission["status"] = "running"
        mission["started_at"] = time.time()
        mission["deadline"] = deadline
        _save()

    executor = AgentExecutor()
    deadline_reached = threading.Event()
    deadline_timer: threading.Timer | None = None
    if deadline is not None:
        remaining = max(0.0, deadline - time.time())
        deadline_timer = threading.Timer(remaining, lambda: (deadline_reached.set(), event.set()))
        deadline_timer.daemon = True
        deadline_timer.start()

    try:
        while not event.is_set():
            now = time.time()
            if deadline is not None and now >= deadline:
                with _LOCK:
                    mission["status"] = "timed_out"
                    mission["finished_at"] = now
                    _save()
                return

            iteration = int(mission.get("iterations") or 0) + 1
            with _LOCK:
                mission["iterations"] = iteration
                mission["last_started_at"] = now
                _save()

            context = (
                f"This is autonomous mission iteration {iteration}. "
                "Continue making concrete progress on the original goal. "
                "Review the previous result, avoid repeating completed work, verify each claimed change, "
                "and stop only when the completion condition is actually satisfied."
            )
            previous = mission.get("last_result")
            if previous:
                context += f"\nPrevious iteration result:\n{str(previous)[-3000:]}"

            goal = str(mission["goal"])
            task_goal = f"{goal}\n\nAUTONOMOUS EXECUTION DIRECTIVE:\n{context}"

            try:
                result = executor.execute(goal=task_goal, cancel_flag=event, player=None)
                finished_now = time.time()
                cancellation_requested = event.is_set() and not deadline_reached.is_set()
                time_limit_reached = deadline_reached.is_set() or (deadline is not None and finished_now >= deadline)
                with _LOCK:
                    mission["last_result"] = result
                    mission["last_finished_at"] = finished_now
                    mission["error"] = ""
                    if time_limit_reached:
                        mission["status"] = "timed_out"
                        mission["finished_at"] = finished_now
                        _save()
                        return
                    if cancellation_requested:
                        mission["status"] = "cancelled"
                        mission["finished_at"] = finished_now
                        _save()
                        return
                    complete = _check_completion(mission, result)
                    if complete and not keep_working:
                        mission["status"] = "completed"
                        mission["finished_at"] = finished_now
                        _save()
                        return
                    _save()
            except Exception as exc:
                failure_now = time.time()
                cancellation_requested = event.is_set() and not deadline_reached.is_set()
                time_limit_reached = deadline_reached.is_set() or (deadline is not None and failure_now >= deadline)
                with _LOCK:
                    mission["error"] = str(exc)
                    mission["last_finished_at"] = failure_now
                    if time_limit_reached:
                        mission["status"] = "timed_out"
                        mission["finished_at"] = failure_now
                    elif cancellation_requested:
                        mission["status"] = "cancelled"
                        mission["finished_at"] = failure_now
                    else:
                        mission["status"] = "running"
                    _save()
                    if mission["status"] in {"timed_out", "cancelled"}:
                        return

            if not keep_working and mission.get("until"):
                # A completion condition was supplied: keep trying until it matches
                keep_working = True

            if deadline is not None:
                remaining = max(0.0, deadline - time.time())
                if remaining <= 0:
                    continue
                event.wait(timeout=min(interval, remaining))
            else:
                event.wait(timeout=interval)

        with _LOCK:
            if deadline_reached.is_set():
                mission["status"] = "timed_out"
            else:
                mission["status"] = "cancelled"
            mission["finished_at"] = time.time()
            _save()
    except Exception as exc:
        with _LOCK:
            mission["status"] = "failed"
            mission["error"] = str(exc)
            mission["finished_at"] = time.time()
            _save()
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()


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

    mission_id = "mission-" + uuid.uuid4().hex[:8]
    mission = {
        "mission_id": mission_id,
        "goal": goal,
        "status": "pending",
        "created_at": time.time(),
        "duration_seconds": duration_seconds,
        "until": until,
        "completion_type": completion_type or ("phrase" if until else "agent_success"),
        "completion_target": completion_target,
        "interval_seconds": max(5, min(int(interval_seconds), 3600)),
        "keep_working": bool(keep_working) if keep_working is not None else (duration_seconds is not None and not until),
        "iterations": 0,
        "last_result": "",
        "error": "",
    }

    with _LOCK:
        _MISSIONS[mission_id] = mission
        _EVENTS[mission_id] = threading.Event()
        _save()

        thread = threading.Thread(
            target=_mission_loop,
            args=(mission_id,),
            daemon=True,
            name=f"BrahmaMission-{mission_id}",
        )
        _THREADS[mission_id] = thread
        thread.start()

    return f"Started autonomous mission {mission_id} for {duration or 'until completed'}. Use action=status with mission_id={mission_id} to inspect it."


def _ensure_loaded() -> None:
    with _LOCK:
        if not _MISSIONS:
            _load()


def list_missions() -> list[dict[str, Any]]:
    _ensure_loaded()
    with _LOCK:
        return [dict(m) for m in sorted(_MISSIONS.values(), key=lambda x: x.get("created_at", 0), reverse=True)]


def status(mission_id: str) -> dict[str, Any]:
    _ensure_loaded()
    with _LOCK:
        mission = _MISSIONS.get(mission_id)
        if not mission:
            return {"success": False, "message": f"Mission '{mission_id}' was not found."}
        return dict(mission)


def cancel(mission_id: str) -> str:
    with _LOCK:
        mission = _MISSIONS.get(mission_id)
        event = _EVENTS.get(mission_id)
        if not mission or not event:
            return f"Mission '{mission_id}' was not found."
        if mission.get("status") in {"completed", "cancelled", "timed_out", "failed"}:
            return f"Mission {mission_id} is already {mission.get('status')}."
        event.set()
        mission["status"] = "cancelling"
        _save()
    return f"Cancellation requested for mission {mission_id}."


def execute(**kwargs: Any) -> dict[str, Any]:
    global _MISSIONS
    with _LOCK:
        if not _MISSIONS:
            _load()

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

    mission_id = str(kwargs.get("mission_id") or "").strip()
    if action == "status":
        return {"summary": f"Mission status for {mission_id}.", "status": status(mission_id)}
    if action == "cancel":
        return {"summary": cancel(mission_id)}
    return {"summary": f"Unknown autonomous mission action: {action}"}
