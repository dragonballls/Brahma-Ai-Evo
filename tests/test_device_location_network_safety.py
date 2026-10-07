from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_device_location_uses_https_only_for_external_geolocation():
    source = (ROOT / "core" / "device_location.py").read_text(encoding="utf-8")
    assert "http://ip-api.com/json" not in source
    assert '"https://ipwho.is/"' in source
    assert '"https://freeipapi.com/api/json"' in source
    assert "resp.read(64 * 1024 + 1)" in source
    assert "Location service response exceeded the safety limit." in source

def test_device_location_settings_corruption_propagates_from_canonical_loader(tmp_path, monkeypatch):
    import core.device_location as device_location
    import memory.config_manager as config_manager
    import pytest

    settings = tmp_path / "app_settings.json"
    settings.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config_manager, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    config_manager._SETTINGS_CACHE = None

    with pytest.raises(RuntimeError, match="corrupted"):
        device_location._read_manual_override()


def test_device_location_cache_corruption_is_not_treated_as_a_cache_miss(tmp_path, monkeypatch):
    import core.device_location as device_location
    import pytest

    cache = tmp_path / "device_location_cache.json"
    cache.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(device_location, "_CACHE_FILE", cache)

    with pytest.raises(RuntimeError, match="cache is corrupted"):
        device_location._read_disk_cache()


def test_windows_location_uses_pinned_system_powershell_without_console(tmp_path, monkeypatch):
    import core.device_location as device_location

    powershell = (
        tmp_path
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    powershell.parent.mkdir(parents=True)
    powershell.write_text("", encoding="utf-8")

    monkeypatch.setattr(device_location.platform, "system", lambda: "Windows")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(
        device_location,
        "_reverse_geocode_osm",
        lambda lat, lon: "Test City",
    )

    captured = {}

    class _Proc:
        stdout = "34.05,-118.25,Ready"

    def _run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(device_location.subprocess, "run", _run)
    result = device_location._detect_via_windows_api()

    assert result["city"] == "Test City"
    assert captured["argv"][0] == str(powershell)
    assert captured["kwargs"]["creationflags"] == getattr(
        device_location.subprocess, "CREATE_NO_WINDOW", 0
    )
    assert captured["kwargs"]["stdin"] is device_location.subprocess.DEVNULL

def test_device_location_accepts_valid_cached_coordinates_after_math_validation(tmp_path, monkeypatch):
    import core.device_location as device_location
    import json
    import time

    cache = tmp_path / "device_location_cache.json"
    cache.write_text(
        json.dumps(
            {
                "status": "success",
                "city": "Test City",
                "latitude": 34.05,
                "longitude": -118.25,
                "timestamp": time.time(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(device_location, "_CACHE_FILE", cache)

    result = device_location._read_disk_cache()
    assert result["city"] == "Test City"


def test_external_location_redirect_is_rejected(monkeypatch):
    import core.device_location as device_location
    import pytest
    import urllib.error

    class _Opener:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("Location service redirects are disabled.")

    monkeypatch.setattr(
        device_location.urllib.request,
        "build_opener",
        lambda *_handlers: _Opener(),
    )

    with pytest.raises(urllib.error.URLError, match="redirects are disabled"):
        device_location._open_no_redirect(
            device_location.urllib.request.Request("https://nominatim.openstreetmap.org/"),
            timeout=1.0,
        )

