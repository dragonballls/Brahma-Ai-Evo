from __future__ import annotations

import hashlib
import os
import platform
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import xml.etree.ElementTree as ET
import ctypes
from ctypes import wintypes
import subprocess


import pyautogui

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

try:
    from pywinauto import Desktop
except Exception:  # pragma: no cover
    Desktop = None


_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "discord": ("discord",),
    "whatsapp": ("whatsapp",),
    "telegram": ("telegram",),
    "signal": ("signal",),
    "skype": ("skype",),
    "zoom": ("zoom",),
    "teams": ("teams", "microsoft teams"),
    "phone link": ("phone link", "your phone"),
    "messenger": ("messenger", "facebook messenger"),
    "instagram": ("instagram",),
    "facebook": ("facebook",),
    "slack": ("slack",),
    "gmail": ("gmail", "google mail"),
    "mail": ("mail", "outlook"),
}

_MESSAGE_HINTS = (
    "new message",
    "message",
    "chat",
    "dm",
    "direct message",
    "unread",
    "notification",
    "mention",
    "reply",
    "preview",
    "received",
)

_CALL_HINTS = (
    "incoming call",
    "call from",
    "voice call",
    "video call",
    "ringing",
    "incoming video",
    "incoming voice",
    "accept",
    "answer",
    "decline",
    "reject",
    "hang up",
    "end call",
)

_ACCEPT_HINTS = ("accept", "answer", "pick up", "join", "allow")
_DECLINE_HINTS = ("decline", "reject", "ignore", "hang up", "end", "cut", "dismiss")
_WINDOW_CALL_HINTS = (
    "call",
    "incoming",
    "ringing",
    "voice",
    "video",
    "answer",
    "accept",
    "decline",
    "reject",
    "hang up",
    "end call",
    "meeting",
    "joined",
    "conference",
)


def _db_path() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    return Path(local) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


def _normalize_app_from_primary(primary: str | None, fallback: str = "") -> str | None:
    hay = f"{primary or ''} {fallback or ''}".lower()
    mapping = [
        ("whatsapp", "WhatsApp"),
        ("discord", "Discord"),
        ("telegram", "Telegram"),
        ("signal", "Signal"),
        ("skype", "Skype"),
        ("zoom", "Zoom"),
        ("teams", "Teams"),
        ("phone link", "Phone Link"),
        ("yourphone", "Phone Link"),
        ("phonelink", "Phone Link"),
        ("messenger", "Messenger"),
        ("instagram", "Instagram"),
        ("facebook", "Facebook"),
        ("slack", "Slack"),
        ("outlook", "Mail"),
        ("mail", "Mail"),
        ("gmail", "Gmail"),
        ("sms", "Messages"),
        ("messages", "Messages"),
    ]
    for token, name in mapping:
        if token in hay:
            return name
    return None


def _parse_toast_payload(payload: bytes | str | None) -> tuple[str, list[str], list[str], str]:
    if payload is None:
        return "", [], [], ""
    if isinstance(payload, (bytes, bytearray)):
        raw = payload.decode("utf-8", "ignore")
    else:
        raw = str(payload)

    texts: list[str] = []
    actions: list[str] = []
    try:
        root = ET.fromstring(raw)
        for node in root.findall(".//text"):
            txt = (node.text or "").strip()
            if txt:
                texts.append(txt)
        for node in root.findall(".//action"):
            for attr in ("content", "arguments", "hint-inputId"):
                val = (node.get(attr) or "").strip()
                if val:
                    actions.append(val)
    except Exception:
        pass

    flat = " ".join(texts + actions + [raw]).lower()
    kind = "call" if any(k in flat for k in _CALL_HINTS) else "message"
    if "call" in flat and not any(m in flat for m in _MESSAGE_HINTS):
        kind = "call"
    return raw, texts, actions, kind


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


_PROC_NAME_CACHE: dict[int, tuple[float, str]] = {}
_PROC_NAME_CACHE_TTL = 15.0


def _proc_name(pid: int | None) -> str:
    if not pid or psutil is None:
        return ""
    now = time.monotonic()
    cached = _PROC_NAME_CACHE.get(int(pid))
    if cached and (now - cached[0]) < _PROC_NAME_CACHE_TTL:
        return cached[1]
    try:
        name = psutil.Process(int(pid)).name().lower()
    except Exception:
        name = ""
    # Short TTL bounds PID reuse while avoiding repeated process-handle/name lookups
    # across the attention monitor's 5-second polling cycle.
    _PROC_NAME_CACHE[int(pid)] = (now, name)
    if len(_PROC_NAME_CACHE) > 512:
        cutoff = now - _PROC_NAME_CACHE_TTL
        for cached_pid, (stamp, _) in list(_PROC_NAME_CACHE.items()):
            if stamp < cutoff:
                _PROC_NAME_CACHE.pop(cached_pid, None)
    return name


def _match_app(text: str, proc_name: str) -> str | None:
    hay = f"{text} {proc_name}".lower()
    for canonical, aliases in _APP_ALIASES.items():
        if any(alias in hay for alias in aliases):
            return canonical
    return None


def _collect_text_snapshot(win, limit: int = 18) -> list[str]:
    out: list[str] = []
    try:
        title = _norm(win.window_text())
        if title:
            out.append(title)
    except Exception:
        pass

    try:
        desc = win.descendants()
    except Exception:
        desc = []

    for child in desc:
        text = ""
        for attr in ("window_text",):
            try:
                text = getattr(child, attr)() or ""
            except Exception:
                text = ""
            if text:
                break
        if not text:
            try:
                text = getattr(getattr(child, "element_info", None), "name", "") or ""
            except Exception:
                text = ""
        text = _norm(text)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break

    return out


def _is_notification_shape(win) -> bool:
    try:
        rect = win.rectangle()
        width = abs(rect.right - rect.left)
        height = abs(rect.bottom - rect.top)
        return width <= 720 and height <= 320
    except Exception:
        return False


def _contains_any(hay: str, needles: tuple[str, ...]) -> bool:
    return any(needle in hay for needle in needles)


def _window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    try:
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, len(buf))
    except Exception:
        return ""
    return _norm(buf.value)


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    try:
        ctypes.windll.user32.GetClassNameW(hwnd, buf, len(buf))
    except Exception:
        return ""
    return _norm(buf.value)


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def _enum_visible_windows() -> list[dict]:
    results: list[dict] = []
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def callback(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if not title:
                return True
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "class": _window_class(hwnd),
                "pid": _window_pid(hwnd),
            })
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception:
        pass
    return results


def _extract_preview(lines: list[str], app: str) -> str:
    cleaned: list[str] = []
    app_l = app.lower()
    for line in lines:
        if not line:
            continue
        if line == app_l:
            continue
        if line in {"message", "notification", "new message"}:
            continue
        cleaned.append(line)
    if not cleaned:
        return ""
    if len(cleaned) >= 2 and cleaned[0] in _APP_ALIASES:
        cleaned = cleaned[1:]
    return " ".join(cleaned[:3]).strip()


_current_player_alias = None
_current_audio_path = None
_speech_sink: Callable[[str], None] | None = None


_current_speech_proc: subprocess.Popen | None = None
_speech_generation = 0
_speech_generation_lock = threading.Lock()


def _next_speech_generation() -> int:
    global _speech_generation
    with _speech_generation_lock:
        _speech_generation += 1
        return _speech_generation


def _current_speech_generation() -> int:
    with _speech_generation_lock:
        return _speech_generation


def _cleanup_current_audio() -> None:
    global _current_player_alias, _current_audio_path, _current_speech_proc
    proc = _current_speech_proc
    _current_speech_proc = None
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.5)
            except Exception:
                pass

    if _current_player_alias is not None:
        try:
            ctypes.windll.winmm.mciSendStringW(f"stop {_current_player_alias}", None, 0, None)
        except Exception:
            pass
        try:
            ctypes.windll.winmm.mciSendStringW(f"close {_current_player_alias}", None, 0, None)
        except Exception:
            pass
        _current_player_alias = None

    if _current_audio_path is not None:
        try:
            if os.path.exists(_current_audio_path):
                os.remove(_current_audio_path)
        except Exception:
            pass
        _current_audio_path = None


def _speak_sapi_male(text: str, *, rate: int = 0, generation: int | None = None) -> None:
    """Speak offline through a cancellable hidden Windows process."""
    global _current_speech_proc
    text = str(text or "").strip()
    if not text:
        return
    if generation is None:
        generation = _current_speech_generation()
    if generation != _current_speech_generation():
        return

    # System.Speech is part of the Windows PowerShell/.NET Framework runtime.
    # Base64 keeps user text out of the command-line parser.
    import base64
    encoded_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
    bounded_rate = max(-10, min(10, int(rate)))
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate={bounded_rate}; "
        "$voices=$s.GetInstalledVoices(); "
        "foreach($v in $voices){$n=$v.VoiceInfo.Name.ToLowerInvariant();"
        "if($n.Contains('george') -or $n.Contains('david') -or $n.Contains('mark')){"
        "$s.SelectVoice($v.VoiceInfo.Name);break}}; "
        f"$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_text}')); "
        "$s.Speak($t); $s.Dispose()"
    )
    encoded_script = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded_script,
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    proc = None
    try:
        proc = subprocess.Popen(cmd, creationflags=flags)
        _current_speech_proc = proc
        return_code = proc.wait()
        if _current_speech_proc is proc:
            _current_speech_proc = None
        if generation != _current_speech_generation():
            return
        if return_code != 0:
            raise RuntimeError(f"Offline speech exited with code {return_code}.")
    except Exception as exc:
        if generation != _current_speech_generation():
            return
        print(f"[AttentionMonitor] Offline male speech failed: {exc}")
    finally:
        if _current_speech_proc is proc:
            _current_speech_proc = None




_speak_lock = threading.Lock()


def _speak_edge_native(
    text: str,
    force_edge: bool = False,
    *,
    rate: str = "+0%",
    pitch: str = "+0Hz",
    sapi_rate: int = 0,
    generation: int | None = None,
) -> None:
    """Speak with lightweight, caller-selected fallback prosody."""
    global _current_player_alias, _current_audio_path
    text = (text or "").strip()
    if not text:
        return
    if generation is None:
        generation = _current_speech_generation()
    if generation != _current_speech_generation():
        return

    rate = str(rate or "+0%")
    pitch = str(pitch or "+0Hz")
    try:
        sapi_rate = max(-10, min(10, int(sapi_rate)))
    except Exception:
        sapi_rate = 0

    # When entered fully local mode from settings, use the offline native male voice unless force_edge requested.
    if not force_edge:
        try:
            from memory import config_manager
            cfg = config_manager.load_settings()
            if cfg.get("offline_mode_enabled", False):
                _speak_sapi_male(text, rate=sapi_rate, generation=generation)
                return
        except Exception:
            pass

    with _speak_lock:
        try:
            import edge_tts
        except Exception as exc:
            print(f"[AttentionMonitor] Edge TTS import failed: {exc}. Falling back to offline male voice.")
            _speak_sapi_male(text, rate=sapi_rate)
            return

        try:
            _cleanup_current_audio()
        except Exception:
            pass

        audio_path = os.path.join(tempfile.gettempdir(), f"brahma_edge_tts_{uuid.uuid4().hex}.mp3")
        try:
            communicator = edge_tts.Communicate(
                text,
                voice="en-US-GuyNeural",
                rate=rate,
                pitch=pitch,
            )
            communicator.save_sync(audio_path)
        except Exception as exc:
            if generation != _current_speech_generation():
                return
            print(f"[AttentionMonitor] Edge TTS generation failed: {exc}. Falling back to offline male voice.")
            _cleanup_current_audio()
            _speak_sapi_male(text, rate=sapi_rate)
            return

        # Play Edge TTS audio via Windows PresentationCore MediaPlayer (native across Windows 10 & 11).
        _current_audio_path = audio_path
        try:
            ps_script = (
                "Add-Type -AssemblyName presentationCore; "
                "$p = New-Object System.Windows.Media.MediaPlayer; "
                "$p.Volume = 1.0; "
                f"$p.Open([System.Uri]'{audio_path}'); "
                "$deadline = (Get-Date).AddSeconds(8); "
                "while(-not $p.NaturalDuration.HasTimeSpan -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }; "
                "if(-not $p.NaturalDuration.HasTimeSpan) { throw 'Media duration metadata did not load.' }; "
                "$duration = $p.NaturalDuration.TimeSpan; "
                "$p.Play(); "
                "$deadline = (Get-Date).AddSeconds([Math]::Max(5, [Math]::Ceiling($duration.TotalSeconds) + 3)); "
                "while($p.Position -lt $duration -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }; "
                "if($p.Position -lt $duration) { throw 'Media playback did not complete.' }; "
                "$p.Stop(); $p.Close()"
            )
            cmd = [
                "powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script
            ]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            _current_speech_proc = subprocess.Popen(cmd, creationflags=flags)
            return_code = _current_speech_proc.wait()
            _current_speech_proc = None
            if generation != _current_speech_generation():
                return
            if return_code != 0:
                raise RuntimeError(f"MediaPlayer exited with code {return_code}.")
        except Exception as exc:
            if generation != _current_speech_generation():
                return
            print(f"[AttentionMonitor] MediaPlayer playback failed: {exc}. Falling back to offline male voice.")
            _speak_sapi_male(text, rate=sapi_rate)
        finally:
            _cleanup_current_audio()




def set_speech_sink(sink_fn: Callable[[str], None] | None) -> None:
    global _speech_sink
    _speech_sink = sink_fn


def speak_native(text: str, force_edge: bool = False) -> None:
    text = (text or "").strip()
    if not text:
        return
    stop_native_speech()
    generation = _current_speech_generation()
    if _speech_sink is not None:
        try:
            _speech_sink(text)
        except Exception:
            _speak_edge_native(text, force_edge=force_edge, generation=generation)
    else:
        _speak_edge_native(text, force_edge=force_edge, generation=generation)


def stop_native_speech() -> None:
    _next_speech_generation()
    _cleanup_current_audio()


def _focus_window_by_app(app: str) -> bool:
    if Desktop is None:
        return False
    try:
        desktop = Desktop(backend="uia")
        for win in desktop.windows():
            title = _norm(win.window_text())
            proc = _proc_name(getattr(win, "process_id", lambda: None)())
            if _match_app(title, proc) == app:
                try:
                    win.set_focus()
                except Exception:
                    pass
                try:
                    win.restore()
                except Exception:
                    pass
                return True
    except Exception:
        pass
    return False


def _click_best_button(app: str, action: str) -> bool:
    if Desktop is None:
        return False
    hints = _ACCEPT_HINTS if action == "accept" else _DECLINE_HINTS
    try:
        desktop = Desktop(backend="uia")
        for win in desktop.windows():
            title = _norm(win.window_text())
            proc = _proc_name(getattr(win, "process_id", lambda: None)())
            if _match_app(title, proc) != app and app not in title:
                continue
            try:
                for ctrl in win.descendants():
                    try:
                        text = _norm(ctrl.window_text() or getattr(getattr(ctrl, "element_info", None), "name", ""))
                    except Exception:
                        text = ""
                    if text and _contains_any(text, hints):
                        try:
                            ctrl.click_input()
                            return True
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass
    return False


def handle_call_action(event: dict, action: str) -> str:
    app = _norm(event.get("app") or "")
    if not app:
        return "No app was detected for that call."

    if action in {"pick_up", "answer", "accept"}:
        if _click_best_button(app, "accept"):
            return f"Picked up the call on {event.get('app', 'the app')}."
        if _focus_window_by_app(app):
            try:
                pyautogui.press("enter")
                return f"Tried to pick up the call on {event.get('app', 'the app')}."
            except Exception:
                pass
        return f"I found the call on {event.get('app', 'the app')}, but could not confirm the answer button."

    if action in {"ignore", "decline", "reject", "cut"}:
        if _click_best_button(app, "decline"):
            return f"Declined the call on {event.get('app', 'the app')}."
        if _focus_window_by_app(app):
            try:
                pyautogui.press("esc")
                return f"Tried to decline the call on {event.get('app', 'the app')}."
            except Exception:
                pass
        return f"I found the call on {event.get('app', 'the app')}, but could not confirm the decline button."

    return "Unknown call action."


def read_event_preview(event: dict) -> str:
    preview = (event.get("preview") or "").strip()
    app = (event.get("app") or "the app").strip()
    if preview:
        return f"You received a message on {app}. {preview}"
    return f"You received a message on {app}."


@dataclass
class _WindowState:
    signature: str = ""
    last_seen: float = 0.0


class AttentionMonitor:
    def __init__(
        self,
        on_event: Callable[[dict], None] | None = None,
        interval: float = 5.0,
    ):
        self._on_event = on_event
        self._interval = max(1.0, float(interval))
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._db = _db_path()
        self._last_id = 0
        self._seen_keys: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._active_window_keys: set[str] = set()
        self._seen_max = 80

    def start(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return

            self._last_id = self._current_max_id()
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="attention-monitor-thread",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._running = False
            self._stop_event.set()
            thread = self._thread

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

        with self._lifecycle_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _loop(self) -> None:
        try:
            if not self._db.exists():
                print(f"[AttentionMonitor] notification DB not found: {self._db}")
                return

            while self._running:
                try:
                    self._poll_once()
                except Exception as exc:
                    print(f"[AttentionMonitor] poll failed: {exc}")
                if self._stop_event.wait(timeout=self._interval):
                    break
        finally:
            with self._lifecycle_lock:
                self._running = False
                if self._thread is threading.current_thread():
                    self._thread = None

    def _current_max_id(self) -> int:
        try:
            with sqlite3.connect(self._db) as con:
                cur = con.cursor()
                cur.execute("SELECT COALESCE(MAX(Id), 0) FROM Notification WHERE Type='toast'")
                row = cur.fetchone()
                return int(row[0] or 0)
        except Exception:
            return 0

    def _remember_seen(self, key: str) -> None:
        if key in self._seen_keys:
            return
        self._seen_keys.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self._seen_max:
            oldest = self._seen_order.popleft()
            self._seen_keys.discard(oldest)

    def _poll_once(self) -> None:
        now = time.time()
        self._poll_toasts(now)
        self._poll_windows(now)

    def _poll_toasts(self, now: float) -> None:
        with sqlite3.connect(self._db, timeout=1.5) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(
                """
                SELECT N.Id, N.HandlerId, N.Type, N.Tag, N.ArrivalTime, N.Payload, H.PrimaryId
                FROM Notification AS N
                LEFT JOIN NotificationHandler AS H ON H.RecordId = N.HandlerId
                WHERE N.Type='toast' AND N.Id > ?
                ORDER BY N.Id ASC
                """,
                (self._last_id,),
            )
            rows = cur.fetchall()

        for row in rows:
            nid = int(row["Id"] or 0)
            self._last_id = max(self._last_id, nid)

            raw, texts, actions, kind = _parse_toast_payload(row["Payload"])
            primary = row["PrimaryId"] or ""
            app = _normalize_app_from_primary(primary, " ".join(texts[:2]) or raw)
            if not app:
                continue

            flat = " ".join(texts + actions + [raw]).lower()
            if any(word in flat for word in ("start app", "screenshot copied", "never lose access")):
                continue

            if kind == "call":
                if not any(k in flat for k in _CALL_HINTS):
                    if not any(x in flat for x in ("incoming", "ringing", "accept", "decline", "answer", "reject")):
                        continue
            elif not any(k in flat for k in _MESSAGE_HINTS) and not texts:
                continue

            preview = ""
            if texts:
                if len(texts) >= 2:
                    preview = " ".join(texts[1:3]).strip()
                else:
                    preview = texts[0].strip()
            if not preview and actions:
                preview = " ".join(actions[:2]).strip()

            dedupe = hashlib.sha1(
                f"{app}|{kind}|{preview}|{raw[:240]}".encode("utf-8", "ignore")
            ).hexdigest()
            if dedupe in self._seen_keys:
                continue
            self._remember_seen(dedupe)

            event = {
                "kind": kind,
                "app": app,
                "title": texts[0] if texts else app,
                "preview": preview,
                "source": "wpndb",
                "notification_id": nid,
                "arrival_time": row["ArrivalTime"],
                "timestamp": now,
                "raw": raw,
                "primary_id": primary,
                "actions": actions,
            }

            if self._on_event:
                self._on_event(event)

    def _poll_windows(self, now: float) -> None:
        current_window_keys: set[str] = set()
        scan_succeeded = False
        try:
            for win in _enum_visible_windows():
                title = win.get("title") or ""
                pid = int(win.get("pid") or 0)
                proc_name = _proc_name(pid)
                app = _match_app(title, proc_name)
                if not app:
                    continue

                hay = f"{title} {win.get('class') or ''} {proc_name}".lower()
                if "brahma" in hay:
                    continue

                if app in {"Zoom", "Teams", "WhatsApp"} and _contains_any(hay, ("meeting", "call", "incoming", "ringing", "conference", "joined")):
                    pass
                elif not _contains_any(hay, _WINDOW_CALL_HINTS):
                    continue

                dedupe = hashlib.sha1(
                    f"window|{app}|{title}|{pid}".encode("utf-8", "ignore")
                ).hexdigest()
                current_window_keys.add(dedupe)
                if dedupe in self._active_window_keys:
                    continue
                self._active_window_keys.add(dedupe)

                event = {
                    "kind": "call",
                    "app": app,
                    "title": title,
                    "preview": title,
                    "source": "window",
                    "notification_id": None,
                    "arrival_time": None,
                    "timestamp": now,
                    "raw": title,
                    "primary_id": proc_name,
                    "actions": [],
                }
                if self._on_event:
                    self._on_event(event)

            scan_succeeded = True
        finally:
            if scan_succeeded:
                self._active_window_keys.intersection_update(current_window_keys)
