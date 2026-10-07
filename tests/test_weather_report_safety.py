import pytest


def test_weather_live_response_is_size_bounded(monkeypatch):
    from actions import weather_report

    class _Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, n=-1):
            return b"x" * (64 * 1024 + 1)

    monkeypatch.setattr(
        weather_report.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(),
    )

    result = weather_report.get_live_weather("Test")
    assert result["status"] == "unavailable"
    assert result["temp_c"] is None
    assert result["condition"] == "Unavailable"


def test_weather_browser_failure_is_not_reported_as_success(monkeypatch):
    from actions import weather_report

    monkeypatch.setattr(
        weather_report,
        "get_live_weather",
        lambda city=None: {
            "status": "success",
            "city": "Test",
            "temp_c": 20,
            "condition": "Clear",
        },
    )
    monkeypatch.setattr(weather_report.webbrowser, "open", lambda *_args, **_kwargs: False)

    result = weather_report.weather_action({"city": "Test", "open_browser": True})
    assert "couldn't open the browser" in result
