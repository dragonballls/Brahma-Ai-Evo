"""Runtime configuration access for Brahma Evo.

Secrets and mutable settings always live in the user data directory. The
repository's example configuration is documentation only and is never treated
as the live API-key store.
"""
from __future__ import annotations

import base64
import json
import platform
import threading
from typing import Any

from core.runtime_paths import API_CONFIG_PATH
from core.provider_policy import GEMINI, OPENROUTER, LOCAL, normalize_provider

_SECRET_SUFFIXES = ("_api_key",)
_PROTECTED_PREFIX = "dpapi:"


def _protect_secret(value: object) -> str:
    text = str(value or "")
    if not text or platform.system().lower() != "windows":
        return text
    try:
        import win32crypt
        protected = win32crypt.CryptProtectData(
            text.encode("utf-8"),
            "Brahma Evo runtime secret",
            None, None, None, 0,
        )[1]
        return _PROTECTED_PREFIX + base64.b64encode(protected).decode("ascii")
    except Exception as exc:
        raise RuntimeError("Windows secret protection is unavailable; refusing to store API keys in plaintext.") from exc


def _unprotect_secret(value: object) -> str:
    raw = str(value or "")
    if not raw.startswith(_PROTECTED_PREFIX):
        return raw
    try:
        import win32crypt
        blob = base64.b64decode(raw[len(_PROTECTED_PREFIX):], validate=True)
        return win32crypt.CryptUnprotectData(blob, None)[1].decode("utf-8")
    except Exception:
        return ""


def _encode_config_for_storage(config: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(config)
    for key, value in list(encoded.items()):
        if key.endswith(_SECRET_SUFFIXES) and value:
            encoded[key] = _protect_secret(value)
    return encoded


def _decode_config_from_storage(config: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(config)
    for key, value in list(decoded.items()):
        if key.endswith(_SECRET_SUFFIXES) and value:
            decoded[key] = _unprotect_secret(value)
    return decoded

_CONFIG_LOCK = threading.RLock()


def get_config() -> dict[str, Any]:
    """Return the persisted runtime API configuration, or safe defaults."""
    with _CONFIG_LOCK:
        defaults: dict[str, Any] = {"os_system": platform.system().lower() or "windows"}
        try:
            if API_CONFIG_PATH.is_file():
                data = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    defaults.update(_decode_config_from_storage(data))
        except (OSError, json.JSONDecodeError):
            pass
        return defaults


def save_config(updates: dict[str, Any]) -> None:
    if not isinstance(updates, dict):
        raise TypeError("configuration updates must be a dictionary")
    with _CONFIG_LOCK:
        API_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        current: dict[str, Any] = {"os_system": platform.system().lower() or "windows"}
        if API_CONFIG_PATH.is_file():
            try:
                data = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "API configuration is unreadable or corrupted; refusing to overwrite it."
                ) from exc
            if not isinstance(data, dict):
                raise RuntimeError(
                    "API configuration has an invalid root schema; refusing to overwrite it."
                )
            current.update(data)
        current.update(updates)
        temp = API_CONFIG_PATH.with_suffix(".json.tmp")
        try:
            temp.write_text(
                json.dumps(_encode_config_for_storage(current), indent=4, ensure_ascii=False),
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
    raw = str(provider or "").strip()
    if not raw:
        return ""
    normalized = normalize_provider(raw, default=raw)
    storage_names = {
        GEMINI: "gemini",
        OPENROUTER: "openrouter",
        LOCAL: "local",
        "TypeSafe": "typesafe",
        "Jev": "typesafe",
    }
    key_name = storage_names.get(normalized, normalized.casefold().replace(" ", "_"))
    return str(get_config().get(f"{key_name}_api_key", "") or "").strip()


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
