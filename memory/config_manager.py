"""
memory/config_manager.py - Centralized configuration access for Brahma AI.
Handles persistent app settings, audio device selection, push-to-talk,
and AI options. Backed by %LOCALAPPDATA%/BrahmaAI/config/app_settings.json.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

from core.user_paths import get_user_data_dir

CONFIG_DIR = get_user_data_dir() / "config"
SETTINGS_FILE = CONFIG_DIR / "app_settings.json"

_lock = threading.RLock()
_settings_cache: Dict[str, Any] | None = None
_settings_mtime_ns: int | None = None


def _ensure_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _read_settings_from_disk() -> Dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings() -> Dict[str, Any]:
    """Load settings with a process-local cache invalidated by file mtime.

    The UI changes settings frequently, so avoiding an unnecessary JSON parse on
    every read materially reduces startup/background I/O while still noticing
    edits made by another process.
    """
    global _settings_cache, _settings_mtime_ns
    _ensure_config()
    with _lock:
        try:
            mtime_ns = SETTINGS_FILE.stat().st_mtime_ns if SETTINGS_FILE.exists() else None
        except OSError:
            mtime_ns = None

        if _settings_cache is not None and mtime_ns == _settings_mtime_ns:
            return dict(_settings_cache)

        data = _read_settings_from_disk()
        _settings_cache = dict(data)
        _settings_mtime_ns = mtime_ns
        return dict(data)


def _atomic_write(data: Dict[str, Any]) -> None:
    """Persist settings without exposing a partially-written JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.tmp")
    payload = json.dumps(data, indent=4, ensure_ascii=False)
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, SETTINGS_FILE)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save_settings(data: Dict[str, Any]) -> None:
    """Merge and atomically persist settings; update the local cache immediately."""
    global _settings_cache, _settings_mtime_ns
    if not isinstance(data, dict) or not data:
        return

    with _lock:
        current = load_settings()
        current.update(data)
        try:
            _atomic_write(current)
            _settings_cache = dict(current)
            try:
                _settings_mtime_ns = SETTINGS_FILE.stat().st_mtime_ns
            except OSError:
                _settings_mtime_ns = None
        except (OSError, TypeError, ValueError) as e:
            print(f"[CONFIG] Error saving settings: {e}")


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
