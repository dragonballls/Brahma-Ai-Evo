import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import creator_engine
from core.creator_render import _segment_filters
from core.creator_audio import record_microphone
from core.creator_voice import generate_voiceover
from core.creator_ingest import prepare_sources
from actions.creator_studio import creator_control


class CreatorStudioTests(unittest.TestCase):
    def test_safe_name_blocks_path_separators(self):
        self.assertEqual(creator_engine._safe_name("../My Video"), "My-Video")

    def test_safe_segment_bounds_and_speed(self):
        result = creator_engine._safe_segments(
            [{"start": -5, "end": 15, "speed": 9, "reason": "x"}],
            10,
        )
        self.assertEqual(result[0]["start"], 0)
        self.assertEqual(result[0]["end"], 10)
        self.assertEqual(result[0]["speed"], 4.0)

    def test_safe_segments_discards_invalid_ranges(self):
        self.assertEqual(
            creator_engine._safe_segments([{"start": 9, "end": 9.01}], 10),
            [],
        )

    def test_metadata_normalizes_hashtags(self):
        with patch.object(
            creator_engine,
            "_ai_json",
            return_value={
                "title": "Test Video",
                "hashtags": ["#one", "two"],
                "tags": ["one", "two"],
                "alternative_titles": ["Alt"],
            },
        ):
            result = creator_engine.metadata("goal", {"summary": "facts"}, {}, "youtube")
        self.assertEqual(result["hashtags"], ["#one", "#two"])
        self.assertEqual(result["title"], "Test Video")

    def test_metadata_fallback_is_complete(self):
        with patch.object(
            creator_engine,
            "_ai_json",
            side_effect=RuntimeError("provider unavailable"),
        ):
            result = creator_engine.metadata(
                "My Gaming Challenge",
                {"summary": "A gaming challenge"},
                {},
                "youtube",
            )
        for key in ("title", "description", "hashtags", "tags", "thumbnail_text", "hook"):
            self.assertIn(key, result)

    def test_render_filters_preserve_source_when_no_segments(self):
        vf, af = _segment_filters(
            {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
            [],
        )
        self.assertEqual(vf, "[0:v]setpts=PTS-STARTPTS[vout]")
        self.assertEqual(af, "[0:a]asetpts=PTS-STARTPTS[aout]")

    def test_render_filters_concat_segments(self):
        vf, af = _segment_filters(
            {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
            [
                {"start": 0, "end": 2, "speed": 1},
                {"start": 4, "end": 6, "speed": 1.5},
            ],
        )
        self.assertIn("concat=n=2", vf)
        self.assertIn("concat=n=2", af)

    def test_rights_review_is_conservative(self):
        result = creator_engine.rights_review({"rights_flags": []}, None)
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(result["flags"])

    def test_recording_rejects_invalid_duration_without_hardware(self):
        with self.assertRaises(ValueError):
            record_microphone("test.wav", 0)

    def test_voiceover_rejects_empty_text_without_process(self):
        with self.assertRaises(ValueError):
            generate_voiceover("", "test.mp3")

    def test_multi_source_single_input_is_non_destructive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mp4"
            source.write_bytes(b"placeholder")
            self.assertEqual(prepare_sources([str(source)], str(Path(temp_dir) / "joined.mp4")), str(source))

    def test_creator_tool_reports_capabilities_without_media(self):
        result = json.loads(creator_control({"action": "tools"}))
        self.assertTrue(result["video_understanding"])
        self.assertTrue(result["script_generation"])
        self.assertTrue(result["youtube_api"])

    def test_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = creator_engine.write_manifest(directory, {"project_id": "abc", "title": "x"})
            loaded_dir, loaded = creator_engine.read_manifest(str(path))
            self.assertEqual(loaded_dir, directory)
            self.assertEqual(loaded["project_id"], "abc")


if __name__ == "__main__":
    unittest.main()
