from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from actions.video_understanding import _build_prompt, _result_text


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


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

    def test_video_understanding_source_contract(self):
        source = _source("actions/video_understanding.py")
        ast.parse(source)
        self.assertIn('"type": "video", "uri": url', source)
        self.assertIn("client.files.upload", source)
        self.assertIn("Focus on the interval", source)


class YouTubeMediaControlContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _source("actions/youtube_video.py")
        ast.parse(cls.source)

    def test_youtube_control_surface_exists(self):
        for marker in (
            '"playlist":  _handle_playlist',
            '"control":   _handle_control',
            '"watch":     _handle_watch',
            '"analyze_section": _handle_watch',
            "playbackRate=",
            "currentTime=",
            "document.querySelector('video')",
            'browser_control({"action": "go_to"',
        ):
            self.assertIn(marker, self.source)

    def test_youtube_playlist_and_analysis_routes_exist(self):
        self.assertIn("def _scrape_first_playlist_url", self.source)
        self.assertIn("def _handle_playlist", self.source)
        self.assertIn("def _handle_watch", self.source)
        self.assertIn("analyze_youtube(", self.source)

    def test_timecode_parser_is_defined(self):
        self.assertIn("def _parse_timecode", self.source)
        self.assertIn('if ":" not in raw:', self.source)


class SpotifyMediaControlContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _source("actions/spotify_controller.py")
        ast.parse(cls.source)

    def test_playlist_action_exists(self):
        self.assertIn('"play_playlist"', self.source)
        self.assertIn('action in ("play_playlist", "playlist")', self.source)
        self.assertIn('_spotify_mcp_call("searchSpotify"', self.source)
        self.assertIn('_spotify_mcp_call("playMusic"', self.source)

    def test_playlist_context_is_built(self):
        self.assertIn('spotify:playlist:', self.source)


if __name__ == "__main__":
    unittest.main()
