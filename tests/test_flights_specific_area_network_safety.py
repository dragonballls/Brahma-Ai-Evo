from __future__ import annotations

import json

import features.flights_specific_area as radar


def test_local_flight_radar_fails_closed_without_device_location(monkeypatch):
    monkeypatch.setattr(radar, "_geocode_location", lambda _area: (_ for _ in ()).throw(RuntimeError("Current device location is unavailable.")))

    result = radar.execute(area="local")

    assert result == {"error": "Current device location is unavailable."}


def test_flight_radar_network_reader_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(radar, "_validate_remote_host", lambda _url: None)

    def fake_fetch(*args, **kwargs):
        raise ValueError("Flight-radar response exceeds the 4 MiB safety limit.")

    monkeypatch.setattr(radar, "fetch_public_bytes", fake_fetch)
    try:
        radar._fetch_json("https://opensky-network.org/api/states/all", timeout=1)
    except ValueError as exc:
        assert "4 MiB safety limit" in str(exc)
    else:
        raise AssertionError("oversized response must be rejected")


def test_flight_radar_network_reader_uses_pinned_transport_and_rejects_redirect(monkeypatch):
    captured = {}
    monkeypatch.setattr(radar, "_validate_remote_host", lambda _url: None)

    def fake_fetch(url, *, timeout, max_response_bytes, headers):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["max_response_bytes"] = max_response_bytes
        captured["headers"] = headers
        return 302, b"redirect"

    monkeypatch.setattr(radar, "fetch_public_bytes", fake_fetch)

    try:
        radar._fetch_json("https://opensky-network.org/api/states/all", timeout=8)
    except RuntimeError as exc:
        assert "HTTP 302" in str(exc)
    else:
        raise AssertionError("redirect response must not be accepted")
    assert captured["timeout"] == 8
    assert captured["max_response_bytes"] == radar.MAX_NETWORK_RESPONSE_BYTES
    assert captured["headers"]["User-Agent"].startswith("BrahmaAI-FlightRadar")


def test_flight_radar_fetch_rejects_non_global_dns_before_request(monkeypatch):
    from core import network_safety
    import pytest

    monkeypatch.setattr(
        network_safety.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 443))],
    )
    with pytest.raises(ValueError, match="non-global"):
        radar._fetch_json(
            "https://opensky-network.org/api/states/all",
            timeout=8,
        )


def test_flight_date_parser_rejects_unparseable_date_instead_of_using_today():
    from actions.flight_finder import _parse_date
    assert _parse_date("not-a-real-date") is None


def test_flight_search_rejects_malformed_passenger_count():
    from actions.flight_finder import flight_finder
    result = flight_finder({"origin": "LAX", "destination": "JFK", "date": "tomorrow", "passengers": "many"})
    assert "Passenger count must be a whole number" in result


def test_flight_report_filename_parts_cannot_escape_desktop():
    from actions.flight_finder import _safe_filename_part
    assert "/" not in _safe_filename_part("../../escape", "origin")
    assert "\\" not in _safe_filename_part(r"..\..\escape", "origin")
    assert _safe_filename_part("   ", "origin") == "origin"
