"""Runtime configuration access for Brahma Evo.

Secrets and mutable settings always live in the user data directory. The
repository's example configuration is documentation only and is never treated
as the live API-key store.
"""
from __future__ import annotations

import json
import platform
import threading
from typing import Any

from core.runtime_paths import API_CONFIG_PATH


_CONFIG_LOCK = threading.RLock()


def get_config() -> dict[str, Any]:
    """Return the persisted runtime API configuration, or safe defaults."""
    with _CONFIG_LOCK:
        defaults: dict[str, Any] = {"os_system": platform.system().lower() or "windows"}
        try:
            if API_CONFIG_PATH.is_file():
                data = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    defaults.update(data)
        except (OSError, json.JSONDecodeError):
            pass
        return defaults


def save_config(updates: dict[str, Any]) -> None:
    if not isinstance(updates, dict):
        raise TypeError("configuration updates must be a dictionary")
    with _CONFIG_LOCK:
        API_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        current = get_config()
        current.update(updates)
        temp = API_CONFIG_PATH.with_suffix(".json.tmp")
        try:
            temp.write_text(
                json.dumps(current, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(API_CONFIG_PATH)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


def get_api_key(provider: str) -> str:
    key = f"{str(provider or '').strip().lower()}_api_key"
    return str(get_config().get(key, "") or "").strip()


def get_os() -> str:
    """Return ``windows``, ``mac``, or ``linux``."""
    value = str(get_config().get("os_system", platform.system()) or "").strip().lower()
    if value == "darwin":
        return "mac"
    return value or "windows"


def is_windows() -> bool:
    return get_os() == "windows"


def is_mac() -> bool:
    return get_os() == "mac"


def is_linux() -> bool:
    return get_os() == "linux"
