from __future__ import annotations

import json

import features.flights_specific_area as radar


def test_local_flight_radar_fails_closed_without_device_location(monkeypatch):
    monkeypatch.setattr(radar, "_geocode_location", lambda _area: (_ for _ in ()).throw(RuntimeError("Current device location is unavailable.")))

    result = radar.execute(area="local")

    assert result == {"error": "Current device location is unavailable."}


def test_flight_radar_network_reader_rejects_oversized_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return b"x" * limit

    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(radar, "_validate_remote_host", lambda _url: None)
    monkeypatch.setattr(radar.urllib.request, "build_opener", lambda *_args: FakeOpener())

    try:
        radar._fetch_json("https://opensky-network.org/api/states/all", timeout=1)
    except ValueError as exc:
        assert "4 MiB safety limit" in str(exc)
    else:
        raise AssertionError("oversized response must be rejected")


def test_flight_radar_rejects_non_global_resolved_address(monkeypatch):
    monkeypatch.setattr(radar.socket, "getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("10.0.0.5", 443))
    ])

    try:
        radar._validate_remote_host("https://opensky-network.org/api/states/all")
    except ValueError as exc:
        assert "non-global" in str(exc)
    else:
        raise AssertionError("private DNS resolution must be rejected")
