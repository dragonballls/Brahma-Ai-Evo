from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

try:
    from core.user_paths import get_user_data_dir
except Exception:
    get_user_data_dir = lambda: Path.home() / ".brahma-evo"

logger = logging.getLogger("BrahmaPCHealth")

_STATE_DIR = Path(get_user_data_dir()) / "pc_health"
_STATE_FILE = _STATE_DIR / "guardian_state.json"

_PROTECTED = {
    "system",
    "system idle process",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "fontdrvhost.exe",
    "winlogon.exe",
    "dwm.exe",
    "explorer.exe",
    "spoolsv.exe",
    "taskhostw.exe",
    "brahmaevo.exe",
}

_WATCH_LOCK = threading.Lock()
_WATCH_STOP = threading.Event()
_WATCH_THREAD: threading.Thread | None = None
_WATCH_STATUS: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "deadline": None,
    "last_scan": None,
    "last_alert": None,
    "last_repair": None,
}


def _hidden_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _run(cmd: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **_hidden_kwargs(),
        )
        return {
            "returncode": p.returncode,
            "stdout": (p.stdout or "").strip(),
            "stderr": (p.stderr or "").strip(),
        }
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _powershell(command: str, timeout: int = 30) -> dict[str, Any]:
    return _run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        timeout=timeout,
    )


def _is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _state_save() -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_WATCH_STATUS, indent=2, default=str), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as exc:
        logger.debug("guardian state save failed: %s", exc)


def _snapshot_processes(limit: int = 12) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "create_time"]):
        try:
            with proc.oneshot():
                info = proc.info
                rss = info.get("memory_info").rss if info.get("memory_info") else 0
                items.append(
                    {
                        "pid": int(info.get("pid") or 0),
                        "name": (info.get("name") or "Unknown").strip(),
                        "rss_mb": round(rss / (1024 * 1024), 1),
                        "cpu_percent": round(float(info.get("cpu_percent") or 0.0), 1),
                        "create_time": info.get("create_time"),
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    items.sort(key=lambda x: x["rss_mb"], reverse=True)
    return items[:limit]


def _memory_summary() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "percent": round(vm.percent, 1),
        "used_gb": round(vm.used / 1024**3, 2),
        "available_gb": round(vm.available / 1024**3, 2),
        "total_gb": round(vm.total / 1024**3, 2),
        "swap_percent": round(swap.percent, 1),
        "swap_used_gb": round(swap.used / 1024**3, 2),
    }


def _storage_summary() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            rows.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "free_gb": round(usage.free / 1024**3, 2),
                    "total_gb": round(usage.total / 1024**3, 2),
                    "used_percent": round(usage.percent, 1),
                    "low_space": usage.percent >= 90,
                }
            )
        except (PermissionError, OSError):
            continue
    return rows


def _event_errors(limit: int = 20) -> list[str]:
    if os.name != "nt":
        return []
    errors: list[str] = []
    query = "*[System[(Level=1 or Level=2)]]"
    for channel in ("System", "Application"):
        result = _run(
            ["wevtutil.exe", "qe", channel, "/q:" + query, "/c:" + str(limit), "/f:text", "/rd:true"],
            timeout=15,
        )
        if result["returncode"] == 0 and result["stdout"]:
            chunks = [x.strip() for x in re.split(r"\n(?=Event\[)", result["stdout"]) if x.strip()]
            errors.extend(chunks[:limit])
    return errors[:limit]


def _windows_integrity_check() -> dict[str, Any]:
    if os.name != "nt":
        return {"supported": False, "reason": "Windows integrity checks are Windows-only."}
    dism = _run(["DISM.exe", "/Online", "/Cleanup-Image", "/CheckHealth"], timeout=120)
    sfc = _run(["sfc.exe", "/verifyonly"], timeout=900)
    return {
        "supported": True,
        "admin": _is_admin(),
        "dism": dism,
        "sfc": sfc,
    }


def _network_check() -> dict[str, Any]:
    result: dict[str, Any] = {}
    if os.name == "nt":
        result["ipconfig"] = _run(["ipconfig.exe", "/all"], timeout=20)
        result["dns"] = _run(["nslookup.exe", "www.microsoft.com"], timeout=15)
    else:
        result["dns"] = _run(["getent", "hosts", "www.microsoft.com"], timeout=10)
    return {
        "healthy": all(v["returncode"] == 0 for v in result.values()),
        "checks": result,
    }


def diagnose(deep: bool = False) -> dict[str, Any]:
    """Collects evidence locally without changing system state. Deep mode adds expensive Windows integrity checks."""
    vm = _memory_summary()
    processes = _snapshot_processes()
    storage = _storage_summary()
    boot = psutil.boot_time()
    uptime_hours = round((time.time() - boot) / 3600, 1)

    cpu_percent = round(psutil.cpu_percent(interval=0.2), 1)
    process_count = len(psutil.pids())

    findings: list[dict[str, Any]] = []
    if vm["percent"] >= 90:
        findings.append({
            "severity": "high",
            "category": "memory_pressure",
            "summary": f"Physical RAM is under heavy pressure at {vm['percent']}%.",
            "evidence": vm,
        })
    elif vm["percent"] >= 80:
        findings.append({
            "severity": "medium",
            "category": "memory_pressure",
            "summary": f"RAM utilization is elevated at {vm['percent']}%.",
            "evidence": vm,
        })

    for proc in processes[:8]:
        if proc["rss_mb"] >= 1500:
            findings.append({
                "severity": "high",
                "category": "memory_consumer",
                "summary": f"{proc['name']} (PID {proc['pid']}) is using {proc['rss_mb']} MB.",
                "evidence": proc,
            })

    for drive in storage:
        if drive["low_space"]:
            findings.append({
                "severity": "high",
                "category": "storage_pressure",
                "summary": f"{drive['device']} is {drive['used_percent']}% full.",
                "evidence": drive,
            })

    if cpu_percent >= 90:
        findings.append({
            "severity": "medium",
            "category": "cpu_pressure",
            "summary": f"CPU utilization is {cpu_percent}%.",
            "evidence": {"cpu_percent": cpu_percent},
        })

    integrity = {"supported": False, "skipped": True, "reason": "Deep integrity verification was not requested."}
    if deep:
        integrity = _windows_integrity_check()
        if integrity.get("supported") and (
            integrity["dism"]["returncode"] not in (0,) or integrity["sfc"]["returncode"] not in (0,)
        ):
            findings.append({
                "severity": "high",
                "category": "system_integrity",
                "summary": "Windows DISM/SFC verification reported a problem or could not complete successfully.",
                "evidence": {
                    "dism_returncode": integrity["dism"]["returncode"],
                    "sfc_returncode": integrity["sfc"]["returncode"],
                },
            })

    network = _network_check()
    if not network["healthy"]:
        findings.append({
            "severity": "medium",
            "category": "network",
            "summary": "At least one local network/DNS diagnostic failed.",
            "evidence": {k: v["returncode"] for k, v in network["checks"].items()},
        })

    events = _event_errors()
    if events:
        findings.append({
            "severity": "medium",
            "category": "event_log",
            "summary": f"Recent critical/error events were found in Windows logs ({len(events)} captured).",
            "evidence": {"events": events},
        })

    return {
        "timestamp": time.time(),
        "platform": platform.platform(),
        "uptime_hours": uptime_hours,
        "cpu_percent": cpu_percent,
        "process_count": process_count,
        "memory": vm,
        "top_processes": processes,
        "storage": storage,
        "integrity": integrity,
        "network": network,
        "event_errors": events,
        "findings": findings,
    }


def detect_memory_leaks(
    sample_seconds: int = 15,
    samples: int = 3,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Looks for sustained RSS growth rather than treating one large process as a leak."""
    sample_seconds = max(5, min(int(sample_seconds), 120))
    samples = max(3, min(int(samples), 8))

    history: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for index in range(samples):
        now = time.time()
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info
                name = (info.get("name") or "Unknown").strip()
                pid = int(info.get("pid") or 0)
                rss = info.get("memory_info").rss if info.get("memory_info") else 0
                history.setdefault((name.lower(), pid), []).append(
                    (now, rss / (1024 * 1024))
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        if index < samples - 1:
            time.sleep(sample_seconds)

    candidates: list[dict[str, Any]] = []
    for (name_key, pid), points in history.items():
        if len(points) < samples:
            continue
        first_t, first_mem = points[0]
        last_t, last_mem = points[-1]
        elapsed_min = max((last_t - first_t) / 60.0, 1e-6)
        slope = (last_mem - first_mem) / elapsed_min
        if slope < 5.0 or (last_mem - first_mem) < 15.0:
            continue
        # Require monotonic-ish growth: no more than one small downward sample.
        drops = sum(1 for a, b in zip(points, points[1:]) if b[1] + 5 < a[1])
        if drops > 1:
            continue
        candidates.append({
            "pid": pid,
            "name": name_key,
            "start_mb": round(first_mem, 1),
            "end_mb": round(last_mem, 1),
            "growth_mb": round(last_mem - first_mem, 1),
            "growth_mb_per_min": round(slope, 1),
            "samples": len(points),
            "confidence": "high" if slope >= 25 else "medium",
        })

    candidates.sort(key=lambda x: x["growth_mb_per_min"], reverse=True)
    return candidates[:limit]


def _terminate_process(pid: int, force: bool = False) -> dict[str, Any]:
    try:
        proc = psutil.Process(int(pid))
        name = (proc.name() or "").lower()
        if int(pid) == os.getpid() or name in _PROTECTED or name + ".exe" in _PROTECTED:
            return {"success": False, "message": f"Refused to terminate protected process '{name}' (PID {pid})."}
        if force:
            proc.kill()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=8)
        except psutil.TimeoutExpired:
            if force:
                proc.kill()
            else:
                return {
                    "success": False,
                    "message": f"{proc.name()} did not exit within the safe termination window.",
                }
        return {"success": True, "message": f"Terminated '{name}' (PID {pid}) to release memory."}
    except psutil.NoSuchProcess:
        return {"success": True, "message": f"PID {pid} already exited."}
    except psutil.AccessDenied:
        return {"success": False, "message": f"Access denied while terminating PID {pid}."}
    except Exception as exc:
        return {"success": False, "message": f"Termination failed for PID {pid}: {exc}"}


def _repair_system_files() -> list[dict[str, Any]]:
    if os.name != "nt":
        return [{"success": False, "message": "Windows system-file repair is unavailable on this OS."}]
    if not _is_admin():
        return [{
            "success": False,
            "message": "System-file repair requires an elevated Brahma session; no automatic UAC bypass is attempted.",
        }]

    dism = _run(["DISM.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"], timeout=2400)
    sfc = _run(["sfc.exe", "/scannow"], timeout=1800)
    return [
        {"success": dism["returncode"] == 0, "tool": "DISM RestoreHealth", "returncode": dism["returncode"], "output": dism["stdout"][-2000:]},
        {"success": sfc["returncode"] == 0, "tool": "SFC scannow", "returncode": sfc["returncode"], "output": sfc["stdout"][-2000:]},
    ]


def _repair_disk() -> dict[str, Any]:
    if os.name != "nt":
        return {"success": False, "message": "Online CHKDSK repair is Windows-only."}
    drive = os.environ.get("SystemDrive", "C:")
    result = _run(["chkdsk.exe", drive, "/scan"], timeout=1800)
    return {
        "success": result["returncode"] == 0,
        "returncode": result["returncode"],
        "output": result["stdout"][-2500:] or result["stderr"][-2500:],
        "drive": drive,
    }


def _repair_network() -> dict[str, Any]:
    if os.name != "nt":
        return {"success": False, "message": "Network repair playbook currently targets Windows."}
    result = _run(["ipconfig.exe", "/flushdns"], timeout=30)
    return {
        "success": result["returncode"] == 0,
        "message": "DNS cache flushed." if result["returncode"] == 0 else (result["stderr"] or "DNS flush failed."),
    }


def repair(target: str = "auto", allow_disruptive: bool = False) -> dict[str, Any]:
    """Runs only conservative, evidence-backed fixes. Destructive registry/driver changes are not attempted."""
    target = str(target or "auto").strip().lower()
    findings = diagnose(deep=(target == "system_files" or allow_disruptive)).get("findings", [])
    actions: list[Any] = []

    if target in ("auto", "memory"):
        leaks = detect_memory_leaks(sample_seconds=5, samples=3, limit=5)
        if leaks:
            if not allow_disruptive:
                actions.append({
                    "success": False,
                    "category": "memory",
                    "message": f"Detected persistent memory growth in {leaks[0]['name']} (PID {leaks[0]['pid']}); termination is held behind allow_disruptive=true.",
                    "leaks": leaks,
                })
            else:
                actions.append(_terminate_process(leaks[0]["pid"], force=False))
        elif target == "memory":
            actions.append({"success": True, "category": "memory", "message": "No sustained process memory-growth pattern was detected."})

    if target in ("auto", "system_files"):
        if any(f["category"] == "system_integrity" for f in findings) or target == "system_files":
            if not allow_disruptive:
                actions.append({
                    "success": False,
                    "category": "system_files",
                    "message": "Windows system-file repair is available but requires allow_disruptive=true and, when needed, elevation.",
                })
            else:
                actions.extend(_repair_system_files())

    if target in ("auto", "disk"):
        if any(f["category"] == "storage_pressure" for f in findings):
            actions.append({
                "success": False,
                "category": "disk",
                "message": "Low disk space was detected. Brahma will not delete user files automatically; run the disk cleanup workflow explicitly.",
            })
        elif target == "disk":
            actions.append(_repair_disk())

    if target in ("auto", "network"):
        if any(f["category"] == "network" for f in findings) or target == "network":
            actions.append(_repair_network())

    if target not in {"auto", "memory", "system_files", "disk", "network"}:
        try:
            pid = int(target)
            if allow_disruptive:
                actions.append(_terminate_process(pid, force=False))
            else:
                actions.append({"success": False, "message": f"PID {pid} was identified as a repair target; set allow_disruptive=true to terminate it."})
        except ValueError:
            actions.append({"success": False, "message": f"Unknown repair target '{target}'."})

    return {
        "success": any(bool(a.get("success")) for a in actions if isinstance(a, dict)) if actions else True,
        "findings": findings,
        "actions": actions,
        "timestamp": time.time(),
    }


def _watch_loop(duration_seconds: float | None = None, interval_seconds: int = 60, auto_repair: bool = False) -> None:
    global _WATCH_STATUS
    started = time.time()
    deadline = started + duration_seconds if duration_seconds is not None else None
    with _WATCH_LOCK:
        _WATCH_STATUS.update({
            "running": True,
            "started_at": started,
            "deadline": deadline,
            "last_scan": None,
            "last_alert": None,
            "last_repair": None,
            "interval_seconds": interval_seconds,
        })
    _state_save()

    while not _WATCH_STOP.is_set():
        now = time.time()
        if deadline and now >= deadline:
            break
        try:
            leaks = detect_memory_leaks(sample_seconds=5, samples=3, limit=3)
            with _WATCH_LOCK:
                _WATCH_STATUS["last_scan"] = time.time()
                if leaks:
                    _WATCH_STATUS["last_alert"] = leaks[0]
                    if auto_repair:
                        if leaks[0]["name"].lower() not in _PROTECTED and leaks[0]["name"].lower() + ".exe" not in _PROTECTED:
                            _WATCH_STATUS["last_repair"] = _terminate_process(leaks[0]["pid"], force=False)
                else:
                    _WATCH_STATUS["last_alert"] = None
            _state_save()
        except Exception as exc:
            logger.exception("memory watch failure: %s", exc)
            with _WATCH_LOCK:
                _WATCH_STATUS["last_alert"] = {"error": str(exc)}
        remaining = (deadline - time.time()) if deadline else None
        base_interval = max(15.0, min(float(interval_seconds), 3600.0))
        sleep_for = min(base_interval, remaining) if remaining is not None else base_interval
        if sleep_for > 0:
            _WATCH_STOP.wait(timeout=sleep_for)

    with _WATCH_LOCK:
        _WATCH_STATUS["running"] = False
        _WATCH_STATUS["deadline"] = deadline
    _state_save()


def start_monitor(duration: str | int | float = "24 hours", interval_seconds: int = 60, auto_repair: bool = False) -> str:
    global _WATCH_THREAD
    duration_seconds = _parse_duration(duration)
    with _WATCH_LOCK:
        if _WATCH_THREAD and _WATCH_THREAD.is_alive():
            return "PC health monitor is already running."
        _WATCH_STOP.clear()
        _WATCH_THREAD = threading.Thread(
            target=_watch_loop,
            kwargs={"duration_seconds": duration_seconds, "interval_seconds": int(interval_seconds), "auto_repair": auto_repair},
            daemon=True,
            name="Brahma-PC-Health",
        )
        _WATCH_THREAD.start()
    return f"PC Health Guardian started for {_format_duration(duration_seconds)}."


def stop_monitor() -> str:
    _WATCH_STOP.set()
    with _WATCH_LOCK:
        running = bool(_WATCH_THREAD and _WATCH_THREAD.is_alive())
    return "Stopping PC Health Guardian." if running else "PC Health Guardian is not running."


def get_status() -> dict[str, Any]:
    with _WATCH_LOCK:
        return dict(_WATCH_STATUS)


def _parse_duration(value: str | int | float | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return min(max(0.0, float(value)), 30 * 86400)
    text = str(value).strip().lower()
    compact = re.sub(r"[,;]", " ", text)
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)", compact)
    if matches:
        total = 0.0
        factors = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
                   "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                   "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                   "d": 86400, "day": 86400, "days": 86400}
        for amount, unit in matches:
            total += float(amount) * factors[unit]
        return min(total, 30 * 86400)
    number = re.search(r"\d+(?:\.\d+)?", compact)
    return float(number.group()) if number else None


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "until stopped"
    sec = int(round(seconds))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    bits = []
    if days: bits.append(f"{days}d")
    if hours: bits.append(f"{hours}h")
    if mins: bits.append(f"{mins}m")
    if secs or not bits: bits.append(f"{secs}s")
    return " ".join(bits)


def execute(**kwargs: Any) -> dict[str, Any]:
    action = str(kwargs.get("action", "diagnose")).strip().lower()
    if action in {"diagnose", "scan", "full"}:
        result = diagnose(deep=bool(kwargs.get("deep", False)))
        return {
            "summary": f"PC diagnosis complete: {len(result['findings'])} findings, RAM {result['memory']['percent']}%, CPU {result['cpu_percent']}%.",
            "output": result,
        }

    if action in {"memory_leak", "memory", "leak"}:
        sample_seconds = int(kwargs.get("sample_seconds", 15))
        samples = int(kwargs.get("samples", 3))
        return {
            "summary": "Memory-growth analysis complete.",
            "leaks": detect_memory_leaks(sample_seconds=sample_seconds, samples=samples),
        }

    if action == "repair":
        return {
            "summary": "PC repair pass complete.",
            "output": repair(
                target=str(kwargs.get("target", "auto")),
                allow_disruptive=bool(kwargs.get("allow_disruptive", False)),
            ),
        }

    if action == "monitor":
        return {
            "summary": start_monitor(
                duration=kwargs.get("duration", "24 hours"),
                interval_seconds=int(kwargs.get("interval_seconds", 60)),
                auto_repair=bool(kwargs.get("allow_disruptive", False)),
            ),
            "status": get_status(),
        }

    if action == "stop":
        return {"summary": stop_monitor(), "status": get_status()}

    if action == "status":
        return {"summary": "PC Health Guardian status.", "status": get_status()}

    return {"summary": f"Unknown PC Health Guardian action: {action}"}
