import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def test_weather_action_never_describes_unavailable_fallback_as_current_weather():
    from actions import weather_report

    with patch.object(weather_report, "get_live_weather", return_value={
        "status": "unavailable",
        "city": "Testville",
        "temp_c": 26,
        "condition": "Clear",
        "summary": "Weather telemetry temporarily offline.",
    }):
        result = weather_report.weather_action({"city": "Testville"})

    assert "currently 26 degrees Celsius" not in result
    assert "currently unavailable" in result


def test_weather_action_reports_failed_browser_launch_instead_of_swallowing_it():
    from actions import weather_report

    with patch.object(weather_report, "get_live_weather", return_value={
        "status": "success",
        "city": "Testville",
        "temp_c": 20,
        "condition": "Clear",
    }), patch.object(weather_report.webbrowser, "open", return_value=False):
        result = weather_report.weather_action(
            {"city": "Testville", "open_browser": True}
        )

    assert "currently 20 degrees Celsius" in result
    assert "couldn't open the browser automatically" in result


def test_upload_video_rejects_missing_source_before_claiming_preparation_success():
    from actions import upload_video

    with patch.object(upload_video, "_find_candidate_video", return_value=None),          patch.object(upload_video, "webbrowser") as browser:
        result = upload_video.run({"platform": "youtube", "description": "demo"})

    assert result.startswith("Video upload preparation failed:")
    browser.open.assert_not_called()


def test_upload_video_rejects_browser_launch_failure():
    from actions import upload_video
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        video = Path(tmp) / "demo.mp4"
        video.write_bytes(b"video")

        with patch.object(upload_video, "_find_candidate_video", return_value=video),              patch.object(upload_video, "_generate_video_copy", return_value={
                 "hook_title": "Demo",
                 "caption": "Demo",
                 "hashtags": [],
                 "formatted_post": "Demo",
             }),              patch.object(upload_video.webbrowser, "open", return_value=False):
            result = upload_video.run({
                "platform": "youtube",
                "description": "demo",
                "video_path": str(video),
            })

    assert "No upload was performed" in result


def test_upload_video_success_language_does_not_claim_an_automatic_upload():
    from actions import upload_video
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        video = Path(tmp) / "demo.mp4"
        video.write_bytes(b"video")

        with patch.object(upload_video, "_find_candidate_video", return_value=video),              patch.object(upload_video, "_generate_video_copy", return_value={
                 "hook_title": "Demo",
                 "caption": "Demo",
                 "hashtags": [],
                 "formatted_post": "Demo",
             }),              patch.object(upload_video.webbrowser, "open", return_value=True):
            result = upload_video.run({
                "platform": "youtube",
                "description": "demo",
                "video_path": str(video),
            })

    assert "has not been uploaded automatically" in result


def test_upload_video_does_not_claim_browser_open_is_verified():
    from actions import upload_video
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        video = Path(tmp) / "demo.mp4"
        video.write_bytes(b"video")

        with patch.object(upload_video, "_find_candidate_video", return_value=video), \
             patch.object(upload_video, "_generate_video_copy", return_value={
                 "hook_title": "Demo",
                 "caption": "Demo",
                 "hashtags": [],
                 "formatted_post": "Demo",
             }), \
             patch.object(upload_video.webbrowser, "open", return_value=True):
            result = upload_video.run({
                "platform": "youtube",
                "description": "demo",
                "video_path": str(video),
            })

    assert "page load was not independently verified" in result
    assert "creator studio is open" not in result.lower()
