from unittest.mock import patch
import pytest


def test_youtube_open_url_reports_browser_failure():
    from actions import youtube_video

    with patch.object(youtube_video.webbrowser, "open", return_value=False):
        assert youtube_video._open_url("https://www.youtube.com/") is False


def test_youtube_play_does_not_claim_valid_url_is_playing_when_navigation_fails():
    from actions import youtube_video

    with patch.object(
        youtube_video,
        "browser_control",
        return_value="Navigation error: browser unavailable",
    ):
        result = youtube_video._handle_play(
            {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            None,
        )

    assert result.startswith("I couldn't open that YouTube video")


def test_youtube_play_does_not_claim_scraped_video_is_playing_when_browser_launch_fails():
    from actions import youtube_video

    with patch.object(youtube_video, "_scrape_first_video_url", return_value="https://www.youtube.com/watch?v=abcdefghijk"),          patch.object(youtube_video, "_open_url", return_value=False):
        result = youtube_video._handle_play({"query": "demo"}, None)

    assert "browser could not be opened" in result


def test_youtube_play_reports_search_open_failure():
    from actions import youtube_video

    with patch.object(youtube_video, "_scrape_first_video_url", return_value=None),          patch.object(youtube_video, "_open_url", return_value=False):
        result = youtube_video._handle_play({"query": "demo"}, None)

    assert result == "I couldn't open the YouTube search page."

    
def test_youtube_play_does_not_claim_playback_after_browser_navigation():
    from actions import youtube_video

    with patch.object(
        youtube_video,
        "browser_control",
        return_value="Opened: https://www.youtube.com/watch?v=abcdefghijk",
    ):
        result = youtube_video._handle_play(
            {"url": "https://www.youtube.com/watch?v=abcdefghijk"},
            None,
        )

    assert "playing" not in result.lower()
    assert "opened" in result.lower()


def test_youtube_http_response_is_bounded(monkeypatch):
    from actions import youtube_video

    class _Response:
        status_code = 200
        encoding = "utf-8"

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            assert chunk_size == 8192
            yield b"x" * (4 * 1024 * 1024 + 1)

    captured = {}

    def _get(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Response()

    monkeypatch.setattr(youtube_video.requests, "get", _get)
    try:
        youtube_video._get_youtube_text("https://www.youtube.com/", timeout=5)
    except RuntimeError as exc:
        assert "exceeded the safety limit" in str(exc)
    else:
        raise AssertionError("Oversized YouTube response was accepted")

    assert captured["kwargs"]["allow_redirects"] is False
    assert captured["kwargs"]["stream"] is True


def test_youtube_http_redirect_is_rejected(monkeypatch):
    from actions import youtube_video

    class _Response:
        status_code = 302
        is_redirect = True
        encoding = "utf-8"
        headers = {"Location": "https://attacker.example/"}

        def raise_for_status(self):
            raise AssertionError("raise_for_status must not run for redirects")

        def iter_content(self, chunk_size=8192):
            raise AssertionError("redirect response body must not be read")

    monkeypatch.setattr(
        youtube_video.requests,
        "get",
        lambda *args, **kwargs: _Response(),
    )

    with pytest.raises(RuntimeError, match="alternate host"):
        youtube_video._get_youtube_text("https://www.youtube.com/", timeout=5)

