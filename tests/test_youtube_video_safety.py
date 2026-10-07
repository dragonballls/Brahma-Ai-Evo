from unittest.mock import patch


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
