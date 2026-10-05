"""
memory/config_manager.py - Centralized configuration access for Brahma AI.
Handles persistent app settings, audio device selection, push-to-talk,
and AI options. Backed by the user-scoped %LOCALAPPDATA%/BrahmaAI/config/app_settings.json.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict
import threading

from core.runtime_paths import CONFIG_DIR, APP_SETTINGS_PATH

BSE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_SETTINGS_PATH
_SETTINGS_LOCK = threading.RLock()
_SETTINGS_CACHE: tuple[tuple[int, int], Dict[str, Any]] | None = None



def _ensure_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _settings_signature() -> tuple[int, int]:
    try:
        stat = SETTINGS_FILE.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return (-1, -1)


def load_settings() -> Dict[str, Any]:
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        _ensure_config()
        signature = _settings_signature()
        if _SETTINGS_CACHE is not None and _SETTINGS_CACHE[0] == signature:
            return dict(_SETTINGS_CACHE[1])
        if signature == (-1, -1):
            _SETTINGS_CACHE = (signature, {})
            return {}

        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            normalized = dict(data) if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            normalized = {}
        _SETTINGS_CACHE = (signature, normalized)
        return dict(normalized)


def save_settings(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TypeError("settings update must be a dictionary")
    with _SETTINGS_LOCK:
        _ensure_config()
        current = load_settings()
        current.update(data)
        temp_path = SETTINGS_FILE.with_suffix(".json.tmp")
        try:
            temp_path.write_text(
                json.dumps(current, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
            temp_path.replace(SETTINGS_FILE)
            global _SETTINGS_CACHE
            _SETTINGS_CACHE = (_settings_signature(), dict(current))
        except Exception as e:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"[CONFIG] Error saving settings: {e}")
            raise


def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)


def set_setting(key: str, value: Any) -> None:
    save_settings({key: value})


# --- Audio Devices
 

def get_input_device() -> str:
    return str(get_setting("input_device", "") or "")


def set_input_device(name: str) -> None:
    set_setting("input_device", name)


def get_output_device() -> str:
    return str(get_setting("output_device", "") or "")


def set_output_device(name: str) -> None:
    set_setting("output_device", name)


# --- Push-to-Talk

def get_push_to_talk_enabled() -> bool:
    return bool(get_setting("push_to_talk_enabled", False))


def set_push_to_talk_enabled(enabled: bool) -> None:
    set_setting("push_to_talk_enabled", bool(enabled))


# --- Wake Word & Briefing

def get_wake_word_enabled() -> bool:
    return bool(get_setting("wake_word_enabled", True))


def save_wake_word_enabled(enabled: bool) -> None:
    set_setting("wake_word_enabled", bool(enabled))


def get_brief_enabled() -> bool:
    return bool(get_setting("brief_enabled", True))
