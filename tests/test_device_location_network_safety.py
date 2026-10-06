from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_device_location_uses_https_only_for_external_geolocation():
    source = (ROOT / "core" / "device_location.py").read_text(encoding="utf-8")
    assert "http://ip-api.com/json" not in source
    assert '"https://ipwho.is/"' in source
    assert '"https://freeipapi.com/api/json"' in source
    assert "resp.read(64 * 1024 + 1)" in source
    assert "Location service response exceeded the safety limit." in source
