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


def _dpapi_protect(text: str) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    payload = text.encode("utf-8")
    buf = ctypes.create_string_buffer(payload)
    in_blob = DATA_BLOB(len(payload), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Brahma Evo runtime secret",
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(blob: bytes) -> str:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(blob)
    in_blob = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _protect_secret(value: object) -> str:
    text = str(value or "")
    if not text or platform.system().lower() != "windows":
        return text
    try:
        import win32crypt
        result = win32crypt.CryptProtectData(
            text.encode("utf-8"),
            "Brahma Evo runtime secret",
            None, None, None, 0,
        )
        protected = result[1] if isinstance(result, tuple) and len(result) > 1 else result
        if isinstance(protected, memoryview):
            protected = protected.tobytes()
        elif isinstance(protected, bytearray):
            protected = bytes(protected)
        if not isinstance(protected, bytes):
            raise TypeError("win32crypt returned non-bytes protected data")
    except Exception:
        protected = _dpapi_protect(text)
    return _PROTECTED_PREFIX + base64.b64encode(protected).decode("ascii")


def _unprotect_secret(value: object) -> str:
    raw = str(value or "")
    if not raw.startswith(_PROTECTED_PREFIX):
        return raw
    try:
        blob = base64.b64decode(raw[len(_PROTECTED_PREFIX):], validate=True)
        try:
            import win32crypt
            result = win32crypt.CryptUnprotectData(blob, None)
            clear = result[1] if isinstance(result, tuple) and len(result) > 1 else result
            if isinstance(clear, memoryview):
                clear = clear.tobytes()
            elif isinstance(clear, bytearray):
                clear = bytes(clear)
            if not isinstance(clear, bytes):
                raise TypeError("win32crypt returned non-bytes clear data")
            return clear.decode("utf-8")
        except Exception:
            return _dpapi_unprotect(blob)
    except Exception as exc:
        raise RuntimeError("Stored Windows API-key protection could not be decrypted.") from exc


def _encode_config_for_storage(config: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(config)
    for key, value in list(encoded.items()):
        if key.endswith(_SECRET_SUFFIXES) and value:
            if isinstance(value, str) and value.startswith(_PROTECTED_PREFIX):
                continue
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
