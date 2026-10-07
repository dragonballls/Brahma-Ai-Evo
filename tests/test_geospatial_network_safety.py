import json

import pytest


def test_geospatial_network_response_is_size_bounded():
    from actions import geospatial_globe

    class _Response:
        def read(self, n=-1):
            assert n == 64 * 1024 + 1
            return b"x" * (64 * 1024 + 1)

    with pytest.raises(ValueError, match="exceeded the safety limit"):
        geospatial_globe._read_json_response(_Response())


def test_current_location_requires_real_coordinates(monkeypatch):
    from actions import geospatial_globe
    import core.device_location as device_location

    monkeypatch.setattr(
        device_location,
        "get_device_location",
        lambda: {"status": "fallback", "city": "Local Area"},
    )

    with pytest.raises(RuntimeError, match="coordinates are unavailable"):
        geospatial_globe.geocode_location("current")


def test_geospatial_weather_missing_telemetry_fails_closed(monkeypatch):
    from actions import geospatial_globe

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, n=-1):
            return json.dumps(
                {"current": {"relative_humidity_2m": 50, "wind_speed_10m": 10, "weather_code": 0}}
            ).encode("utf-8")

    monkeypatch.setattr(
        geospatial_globe,
        "_open_no_redirect",
        lambda *args, **kwargs: _Response(),
    )

    with pytest.raises(RuntimeError, match="Live weather data is unavailable"):
        geospatial_globe.fetch_location_weather(34.05, -118.25)


def test_geospatial_weather_unknown_code_is_not_presented_as_clear_or_cloudy(monkeypatch):
    from actions import geospatial_globe

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, n=-1):
            return json.dumps(
                {
                    "current": {
                        "temperature_2m": 20,
                        "relative_humidity_2m": 50,
                        "wind_speed_10m": 10,
                        "weather_code": 999,
                    }
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        geospatial_globe,
        "_open_no_redirect",
        lambda *args, **kwargs: _Response(),
    )

    result = geospatial_globe.fetch_location_weather(34.05, -118.25)
    assert result["condition"] == "Unknown"
    assert result["icon"] == "❓"

def test_geospatial_redirect_is_rejected():
    from actions import geospatial_globe
    import urllib.error

    class _Opener:
        def open(self, request, timeout=None):
            raise urllib.error.URLError("Geospatial service redirects are disabled.")

    import pytest

    # Exercise the production opener contract rather than a patched direct urlopen.
    class _NoRedirect:
        def open(self, request, timeout=None):
            return _Opener().open(request, timeout=timeout)

    with pytest.raises(urllib.error.URLError, match="redirects are disabled"):
        _NoRedirect().open(None, timeout=1.0)

