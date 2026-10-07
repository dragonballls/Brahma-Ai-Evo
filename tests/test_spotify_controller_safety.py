import json
from pathlib import Path


def test_spotify_config_corruption_is_not_converted_to_empty_state(tmp_path, monkeypatch):
    from actions import spotify_controller

    config = tmp_path / "spotify-config.json"
    config.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(spotify_controller, "SPOTIFY_CONFIG_PATH", config)

    import pytest

    with pytest.raises(RuntimeError, match="corrupted"):
        spotify_controller.get_spotify_config()


def test_spotify_credential_write_is_atomic_on_replace_failure(tmp_path, monkeypatch):
    from actions import spotify_controller

    config = tmp_path / "spotify-config.json"
    original = {
        "clientId": "old-client",
        "clientSecret": "old-secret",
        "redirectUri": "http://127.0.0.1:8888/callback",
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(spotify_controller, "SPOTIFY_CONFIG_PATH", config)

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(spotify_controller.os, "replace", fail_replace)

    assert spotify_controller.save_spotify_credentials("new-client", "new-secret") is False
    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_spotify_open_url_reports_immediate_chrome_exit(tmp_path, monkeypatch):
    from actions import spotify_controller

    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    monkeypatch.setattr(spotify_controller, "_get_chrome_path", lambda: str(chrome))
    monkeypatch.setattr(spotify_controller.webbrowser if hasattr(spotify_controller, "webbrowser") else __import__("webbrowser"), "open", lambda *_args, **_kwargs: True)

    class _Process:
        def poll(self):
            return 1

    monkeypatch.setattr(
        spotify_controller.subprocess,
        "Popen",
        lambda *args, **kwargs: _Process(),
    )

    assert spotify_controller._open_url_in_chrome("https://example.com") is True
