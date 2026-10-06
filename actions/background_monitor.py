# actions/background_monitor.py
"""
Background Monitor Action for Brahma AI.

Allows the AI to schedule background polling for system health, crypto prices, or website uptime.
"""

import threading
import time
import requests
import json
import uuid
from datetime import datetime
from actions.system_manager import get_system_health

_monitors = {}
_monitor_lock = threading.Lock()
_monitor_wakeup = threading.Event()
_monitor_thread = None
_monitor_running = False
_speech_sink = None

def set_monitor_speech_sink(sink_fn):
    global _speech_sink
    _speech_sink = sink_fn

def _monitor_loop():
    global _monitor_running
    while _monitor_running:
        with _monitor_lock:
            if not _monitors:
                _monitor_running = False
                break

            current_time = time.time()
            due = []
            next_wait = 30.0
            for m_id, m in list(_monitors.items()):
                remaining = max(0.0, float(m["interval"]) - (current_time - m["last_check"]))
                next_wait = min(next_wait, remaining)
                if remaining <= 0.0:
                    m["last_check"] = current_time
                    due.append((m_id, dict(m)))

        # Never hold the shared lock during network/system work.
        for m_id, monitor in due:
            _run_check(m_id, monitor)

        if not due:
            _monitor_wakeup.wait(timeout=max(0.25, min(next_wait, 30.0)))
            _monitor_wakeup.clear()

    _monitor_running = False

def _ensure_monitor_thread() -> None:
    global _monitor_thread, _monitor_running
    with _monitor_lock:
        if _monitor_running and _monitor_thread and _monitor_thread.is_alive():
            _monitor_wakeup.set()
            return
        _monitor_running = True
        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            daemon=True,
            name="background-monitor",
        )
        _monitor_thread.start()

def _run_check(m_id, m):
    try:
        alert_msg = None
        
        if m['type'] == 'system':
            health = get_system_health()
            if m['target'] == 'ram' and health['ram_usage_percent'] > m['threshold']:
                alert_msg = f"Alert: RAM usage has exceeded {m['threshold']}%. Currently at {health['ram_usage_percent']}%."
            elif m['target'] == 'cpu' and health['cpu_usage_percent'] > m['threshold']:
                alert_msg = f"Alert: CPU usage has exceeded {m['threshold']}%. Currently at {health['cpu_usage_percent']}%."
                
        elif m['type'] == 'crypto':
            # Target should be a coin id like 'bitcoin'
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={m['target']}&vs_currencies=usd"
            resp = requests.get(url, timeout=5).json()
            if m['target'] in resp:
                price = resp[m['target']]['usd']
                # Condition: "above" or "below"
                if m['condition'] == 'above' and price > m['threshold']:
                    alert_msg = f"Alert: {m['target'].capitalize()} has gone above ${m['threshold']}. Current price is ${price}."
                elif m['condition'] == 'below' and price < m['threshold']:
                    alert_msg = f"Alert: {m['target'].capitalize()} has dropped below ${m['threshold']}. Current price is ${price}."
                    
        elif m['type'] == 'website':
            try:
                resp = requests.get(m['target'], timeout=5)
                if resp.status_code >= 400:
                    alert_msg = f"Alert: Website {m['target']} is returning status code {resp.status_code}."
            except Exception:
                alert_msg = f"Alert: Website {m['target']} appears to be down or unreachable."

        if alert_msg:
            # Alert triggered! Remove monitor atomically, then speak without
            # holding the monitor lock during callback work.
            with _monitor_lock:
                _monitors.pop(m_id, None)
            if _speech_sink:
                _speech_sink(alert_msg)
            
    except Exception as e:
        print(f"[Monitor] Error checking {m_id}: {e}")

# The worker starts lazily from add_monitor(), so this feature has no
# permanent polling thread when unused.

def add_monitor(monitor_type: str, target: str, threshold: float, condition: str = "above", interval_sec: int = 60) -> str:
    monitor_type = str(monitor_type or "").strip().lower()
    target = str(target or "").strip()
    condition = str(condition or "above").strip().lower()
    if monitor_type not in {"system", "crypto", "website"}:
        raise ValueError("Unsupported monitor type.")
    if not target:
        raise ValueError("Monitor target is required.")
    if condition not in {"above", "below"}:
        raise ValueError("Monitor condition must be 'above' or 'below'.")
    interval_sec = max(1, int(interval_sec))
    m_id = f"{monitor_type}_{target}_{time.time_ns()}_{uuid.uuid4().hex[:6]}"
    with _monitor_lock:
        _monitors[m_id] = {
            "type": monitor_type,
            "target": target.lower(),
            "threshold": threshold,
            "condition": condition,
            "interval": interval_sec,
            "last_check": time.time()
        }
    _ensure_monitor_thread()
    _monitor_wakeup.set()
    return f"Started monitoring {monitor_type} ({target}) every {interval_sec} seconds."

def get_monitors() -> str:
    with _monitor_lock:
        if not _monitors:
            return "No active background monitors."
        return json.dumps(_monitors, indent=2)

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "add")
    if action == "list":
        return get_monitors()
    
    m_type = parameters.get("type")
    target = parameters.get("target")
    threshold = parameters.get("threshold", 0.0)
    condition = parameters.get("condition", "above")
    interval = parameters.get("interval", 60)
    
    if not m_type or not target:
        return "You must provide a 'type' (system/crypto/website) and a 'target' (ram/cpu/bitcoin/url)."
        
    res = add_monitor(m_type, target, float(threshold), condition, int(interval))
    if player:
        player.write_log(f"SYS: {res}")
    return res
