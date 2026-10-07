"""
memory/config_manager.py - Centralized configuration access for Brahma AI.
Handles persistent app settings, audio device selection, push-to-talk,
and AI options. Backed by the user-scoped %LOCALAPPDATA%/BrahmaAI/config/app_settings.json.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Dict
import threading
import uuid
import stat

from core.runtime_paths import CONFIG_DIR, APP_SETTINGS_PATH

BSE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_SETTINGS_PATH
_SETTINGS_LOCK = threading.RLock()
_SETTINGS_CACHE: tuple[tuple[int, int, int], Dict[str, Any]] | None = None



def _ensure_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _settings_signature() -> tuple[int, int, int]:
    try:
        entry = SETTINGS_FILE.lstat()
        return entry.st_mtime_ns, entry.st_size, int(entry.st_ino)
    except OSError:
        return (-1, -1, -1)


def _read_settings_text() -> str:
    """Read the settings file through an opened file descriptor without following path swaps."""
    if os.name == "nt":
        from core.windows_file_safety import read_text
        text, _size = read_text(SETTINGS_FILE, max_chars=4 * 1024 * 1024)
        return text

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(SETTINGS_FILE, flags)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(4 * 1024 * 1024 + 1)
    finally:
        if fd >= 0:
            os.close(fd)




def load_settings() -> Dict[str, Any]:
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        _ensure_config()
        try:
            mode = SETTINGS_FILE.lstat().st_mode
        except FileNotFoundError:
            mode = 0
        if mode and stat.S_ISLNK(mode):
            raise RuntimeError("Settings file must not be a symlink.")
        signature = _settings_signature()
        if _SETTINGS_CACHE is not None and _SETTINGS_CACHE[0] == signature:
            return dict(_SETTINGS_CACHE[1])
        if signature == (-1, -1, -1):
            _SETTINGS_CACHE = (signature, {})
            return {}

        try:
            raw = _read_settings_text()
            if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
                raise ValueError("Settings file exceeds the 4 MiB safety limit.")
            data = json.loads(raw)
        except OSError as exc:
            raise RuntimeError("Settings file could not be read safely.") from exc
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                "Settings file is unreadable or corrupted; refusing to use empty defaults."
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                "Settings file has an invalid root schema; refusing to use empty defaults."
            )
        normalized = dict(data)
        _SETTINGS_CACHE = (signature, normalized)
        return dict(normalized)


def save_settings(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TypeError("settings update must be a dictionary")
    with _SETTINGS_LOCK:
        _ensure_config()
        if SETTINGS_FILE.is_symlink():
            raise RuntimeError("Settings file must not be a symlink.")
        if SETTINGS_FILE.exists() and not SETTINGS_FILE.is_file():
            raise RuntimeError("Settings path is not a regular file.")
        current: Dict[str, Any] = {}
        if SETTINGS_FILE.is_file():
            try:
                loaded = json.loads(_read_settings_text())
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(
                    "Settings file is unreadable or corrupted; refusing to overwrite it."
                ) from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(
                    "Settings file has an invalid root schema; refusing to overwrite it."
                )
            current.update(loaded)
        current.update(data)
        temp_path = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.{uuid.uuid4().hex}.tmp")
        try:
            payload = json.dumps(current, indent=4, ensure_ascii=False)
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
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


def get_boolean_setting(key: str, default: bool = False) -> bool:
    """Read a persisted boolean without coercing malformed values into truthy state."""
    if not isinstance(default, bool):
        raise TypeError("boolean setting default must be a bool")
    value = get_setting(key, default)
    if not isinstance(value, bool):
        raise RuntimeError(f"Setting '{key}' must be a boolean value.")
    return value


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


# --- Voice transcription privacy

def get_cloud_transcription_enabled() -> bool:
    """Return whether cloud speech recognition is explicitly permitted."""
    value = get_setting("allow_cloud_transcription", False)
    # Permission is opt-in and must be represented by the literal boolean True.
    # Invalid persisted values fail closed instead of enabling network transcription.
    return value is True


def set_cloud_transcription_enabled(enabled: bool) -> None:
    set_setting("allow_cloud_transcription", bool(enabled))


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
