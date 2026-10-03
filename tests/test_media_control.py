from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from actions.video_understanding import _build_prompt, _result_text
from actions.youtube_video import (
    _parse_timecode,
    _scrape_first_playlist_url,
    _control_video,
)
from actions.spotify_controller import _spotify_play_playlist_by_name


class VideoUnderstandingTests(unittest.TestCase):
    def test_prompt_preserves_timestamp_focus_and_visual_audio_scope(self):
        prompt = _build_prompt("What did the Morse code say?", "4:20", "4:45")
        self.assertIn("4:20", prompt)
        self.assertIn("4:45", prompt)
        self.assertIn("visuals and audio", prompt)
        self.assertIn("Morse code", prompt)

    def test_result_text_extracts_interactions_text_steps(self):
        payload = {
            "steps": [
                {"content": [{"type": "text", "text": "First finding."}]},
                {"content": [{"type": "text", "text": "Second finding."}]},
            ]
        }
        result = _result_text(payload)
        self.assertIn("First finding.", result)
        self.assertIn("Second finding.", result)


class YouTubeMediaControlTests(unittest.TestCase):
    def test_parse_timecode_supports_seconds_and_clock_formats(self):
        self.assertEqual(_parse_timecode("90"), 90.0)
        self.assertEqual(_parse_timecode("1:30"), 90.0)
        self.assertEqual(_parse_timecode("1:02:03"), 3723.0)

    def test_playlist_search_extracts_first_playlist(self):
        fake_html = (
            '{"playlistId":"PL_TEST_123"}'
            '{"playlistId":"PL_TEST_123"}'
            '{"playlistId":"PL_TEST_456"}'
        )
        with patch("actions.youtube_video.requests.get") as get:
            get.return_value.text = fake_html
            url = _scrape_first_playlist_url("bomba")
        self.assertEqual(url, "https://www.youtube.com/playlist?list=PL_TEST_123")

    def test_control_speed_uses_html5_video(self):
        browser_result = json.dumps({"ok": True, "rate": 1.7, "currentTime": 12.5, "playing": True})
        with patch("actions.youtube_video.browser_control", return_value=browser_result) as control:
            result = _control_video({"command": "speed", "speed": 1.7})
        self.assertIn("1.7x", result)
        args = control.call_args.args[0]
        self.assertEqual(args["action"], "evaluate")
        self.assertIn("playbackRate=1.7", args["expression"])


class SpotifyPlaylistTests(unittest.TestCase):
    def test_play_playlist_searches_playlist_then_starts_context(self):
        with patch(
            "actions.spotify_controller._spotify_mcp_call",
            side_effect=[
                {"success": True, "output": '1. "bomba (10 tracks)" by User - ID: PL123'},
                {"success": True, "output": "Now playing: spotify:playlist:PL123"},
            ],
        ) as call:
            result = _spotify_play_playlist_by_name("bomba")
        self.assertIn('Playing the Spotify playlist "bomba', result)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0].args[0], "searchSpotify")
        self.assertEqual(call.call_args_list[1].args[0], "playMusic")
        self.assertEqual(
            call.call_args_list[1].args[1]["uri"],
            "spotify:playlist:PL123",
        )


if __name__ == "__main__":
    unittest.main()
