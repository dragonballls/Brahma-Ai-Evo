from core.user_paths import get_user_data_dir
from core.network_safety import fetch_public_bytes
# core/device_location.py

import json
import os
import platform
import math
import subprocess
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_DIR = get_user_data_dir() / "config"
_CACHE_FILE = _CONFIG_DIR / "device_location_cache.json"
_SETTINGS_FILE = _CONFIG_DIR / "app_settings.json"

_MEMORY_CACHE: Optional[Dict[str, Any]] = None
_MEMORY_CACHE_TIME: float = 0
_CACHE_TTL: float = 1800.0  # 30 minutes


def _read_manual_override() -> Optional[str]:
    """Read location override through the canonical fail-closed settings loader."""
    from memory.config_manager import load_settings

    data = load_settings()
    override = data.get("location_override") or data.get("city")
    if override and isinstance(override, str) and override.strip():
        return override.strip()
    return None


def _read_disk_cache() -> Optional[Dict[str, Any]]:
    """Read cached location from disk without treating corruption as a cache miss."""
    if not _CACHE_FILE.exists():
        return None
    if _CACHE_FILE.is_symlink():
        raise RuntimeError("Device location cache must not be a symlink.")
    if not _CACHE_FILE.is_file():
        raise RuntimeError("Device location cache path is not a regular file.")

    try:
        cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("Device location cache could not be read safely.") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Device location cache is corrupted; refusing to ignore it.") from exc

    if not isinstance(cached, dict):
        raise RuntimeError("Device location cache has an invalid root schema.")
    if cached.get("status") != "success":
        raise RuntimeError("Device location cache has an invalid status.")
    cached_time = cached.get("timestamp")
    if not isinstance(cached_time, (int, float)) or not math.isfinite(float(cached_time)):
        raise RuntimeError("Device location cache has an invalid timestamp.")
    city = cached.get("city")
    if not isinstance(city, str) or not city.strip():
        raise RuntimeError("Device location cache has an invalid city.")
    for key, lower, upper in (
        ("latitude", -90.0, 90.0),
        ("longitude", -180.0, 180.0),
    ):
        value = cached.get(key)
        if value is None:
            raise RuntimeError(f"Device location cache is missing {key}.")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Device location cache has an invalid {key}.") from exc
        if not math.isfinite(numeric) or not lower <= numeric <= upper:
            raise RuntimeError(f"Device location cache has an invalid {key}.")

    age = time.time() - float(cached_time)
    if 0 <= age < _CACHE_TTL:
        return cached
    return None


def _write_disk_cache(data: Dict[str, Any]) -> None:
    """Persist detected location atomically so cache writes cannot corrupt the file."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if _CACHE_FILE.is_symlink():
        raise RuntimeError("Device location cache must not be a symlink.")
    if _CACHE_FILE.exists() and not _CACHE_FILE.is_file():
        raise RuntimeError("Device location cache path is not a regular file.")

    data_copy = dict(data)
    data_copy["timestamp"] = time.time()
    temp = _CACHE_FILE.with_name(f".{_CACHE_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data_copy, indent=2), encoding="utf-8")
        os.replace(temp, _CACHE_FILE)
    except Exception as exc:
        raise RuntimeError("Device location cache could not be persisted safely.") from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _detect_via_windows_api() -> Optional[Dict[str, Any]]:
    """Attempts hardware/Wi-Fi location detection via Windows System.Device.Location (High Accuracy)."""
    if platform.system() != "Windows":
        return None

    script = """
    Add-Type -AssemblyName System.Device
    $w = New-Object System.Device.Location.GeoCoordinateWatcher(1)
    $w.Start()
    Start-Sleep -Milliseconds 1800
    $pos = $w.Position.Location
    Write-Output "$($pos.Latitude),$($pos.Longitude),$($w.Status)"
    """
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        return None
    powershell_path = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if powershell_path.is_symlink() or not powershell_path.is_file():
        return None

    try:
        proc = subprocess.run(
            [
                str(powershell_path),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=4.5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = proc.stdout.strip()
        parts = out.split(",")
        if len(parts) >= 3 and parts[2].strip().lower() == "ready":
            lat = float(parts[0])
            lon = float(parts[1])
            if abs(lat) > 0.001 and abs(lon) > 0.001:
                city = _reverse_geocode_osm(lat, lon)
                if city:
                    return {
                        "status": "success",
                        "city": city,
                        "latitude": lat,
                        "longitude": lon,
                        "source": "windows_location",
                    }
    except Exception:
        pass
    return None


def _reverse_geocode_osm(lat: float, lon: float) -> Optional[str]:
    """Reverse geocodes coordinates using OpenStreetMap Nominatim."""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
        status, raw = fetch_public_bytes(
            url,
            timeout=3.0,
            max_response_bytes=64 * 1024,
            headers={"User-Agent": "BrahmaAI-LocationEngine/1.0"},
        )
        if status >= 400:
            raise RuntimeError(f"Location service returned HTTP {status}.")
        data = json.loads(raw.decode("utf-8"))
        addr = data.get("address", {}) if isinstance(data, dict) else {}
        return (
            addr.get("city")
            or addr.get("town")
            or addr.get("suburb")
            or addr.get("borough")
            or addr.get("county")
            or addr.get("state_district")
        )
    except Exception:
        return None


def _detect_via_ip_services() -> Optional[Dict[str, Any]]:
    """Fast, accurate IP geolocation using multi-provider cascade."""
    endpoints = [
        ("ipwho.is", "https://ipwho.is/", "city", "latitude", "longitude", "region"),
        ("freeipapi.com", "https://freeipapi.com/api/json", "cityName", "latitude", "longitude", "regionName"),
    ]

    for name, url, city_k, lat_k, lon_k, reg_k in endpoints:
        try:
            status, raw = fetch_public_bytes(
                url,
                timeout=2.5,
                max_response_bytes=64 * 1024,
                headers={"User-Agent": "BrahmaAI-LocationEngine/1.0"},
            )
            if status >= 400:
                raise RuntimeError(f"Location service returned HTTP {status}.")
            data = json.loads(raw.decode("utf-8"))
            city = data.get(city_k)
            if city and isinstance(city, str) and city.strip() and city.lower() != "none":
                return {
                    "status": "success",
                    "city": city.strip(),
                    "region": data.get(reg_k),
                    "country": data.get("country") or data.get("countryName"),
                    "latitude": data.get(lat_k),
                    "longitude": data.get(lon_k),
                    "source": name,
                }
        except Exception:
            continue

    return None


def get_device_location(force_refresh: bool = False) -> Dict[str, Any]:
    global _MEMORY_CACHE, _MEMORY_CACHE_TIME

    # 1. Manual user override check
    override = _read_manual_override()
    if override:
        return {
            "status": "success",
            "city": override,
            "source": "manual_override",
        }

    # 2. In-memory cache check
    if not force_refresh and _MEMORY_CACHE and (time.time() - _MEMORY_CACHE_TIME < _CACHE_TTL):
        return _MEMORY_CACHE

    # 3. Disk cache check
    if not force_refresh:
        disk_cache = _read_disk_cache()
        if disk_cache:
            _MEMORY_CACHE = disk_cache
            _MEMORY_CACHE_TIME = disk_cache.get("timestamp", time.time())
            return disk_cache

    # 4. Primary: High-Accuracy Windows Hardware / Wi-Fi Geolocation
    loc = _detect_via_windows_api()

    # 5. Secondary: Multi-provider IP Geolocation fallback
    if not loc:
        loc = _detect_via_ip_services()

    if loc:
        _MEMORY_CACHE = loc
        _MEMORY_CACHE_TIME = time.time()
        _write_disk_cache(loc)
        return loc

    # 6. Fallback if completely offline
    fallback_cache = _read_disk_cache()
    if fallback_cache:
        return fallback_cache

    return {
        "status": "fallback",
        "city": "Local Area",
        "source": "default_fallback",
    }


def get_device_city(default: str = "Local Area") -> str:
    loc = get_device_location()
    return loc.get("city") or default
