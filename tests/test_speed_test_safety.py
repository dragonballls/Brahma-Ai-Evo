from __future__ import annotations

import speedtest

import features.internet_speed_test as speed_feature
import features.tests_my_internet_speed as alias_feature


def test_primary_speed_test_fails_closed_when_measurement_fails(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("measurement unavailable")

    monkeypatch.setattr(speedtest, "Speedtest", fail)

    result = speed_feature.execute()

    assert result["success"] is False
    assert result["error"] == "measurement unavailable"
    assert "45.20" not in result["summary"]
    assert "22.80" not in result["summary"]


def test_alias_speed_test_fails_closed_when_measurement_fails(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("measurement unavailable")

    monkeypatch.setattr(alias_feature.speedtest, "Speedtest", fail)

    result = alias_feature.execute()

    assert result["success"] is False
    assert result["error"] == "measurement unavailable"
    assert "45.20" not in result["summary"]
    assert "22.80" not in result["summary"]
