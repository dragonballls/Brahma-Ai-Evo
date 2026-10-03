"""Regression tests for the Live-style duplex voice contract."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from memory import config_manager
from core.duplex_voice import BargeInGate, PlaybackGeneration


ROOT = Path(__file__).resolve().parents[1]


class LiveVoiceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.echo_text = (ROOT / "core" / "echo.py").read_text(encoding="utf-8")

    def test_input_and_output_chunk_is_within_live_latency_range(self):
        tree = ast.parse(self.main_text)
        found = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "CHUNK_SIZE":
                        found = ast.literal_eval(node.value)
        self.assertEqual(found, 640)
        # 640 samples = 40 ms at 16 kHz input, and 26.7 ms at 24 kHz output.

    def test_live_affective_dialog_is_enabled(self):
        self.assertIn("enable_affective_dialog=True", self.main_text)

    def test_server_vad_and_start_of_activity_interrupts_are_configured(self):
        self.assertIn("realtime_input_config={", self.main_text)
        self.assertIn('"automatic_activity_detection"', self.main_text)
        self.assertIn('"disabled": False', self.main_text)
        self.assertIn('"prefix_padding_ms": 100', self.main_text)
        self.assertIn('"silence_duration_ms": 650', self.main_text)
        self.assertIn('"activity_handling": "START_OF_ACTIVITY_INTERRUPTS"', self.main_text)

    def test_server_interrupted_event_clears_client_playback(self):
        self.assertIn('getattr(sc, "interrupted", False)', self.main_text)
        self.assertIn("self.trigger_barge_in()", self.main_text)
        self.assertIn("self._playback_generation.bump()", self.main_text)
        self.assertIn("generation != self._playback_generation.current()", self.main_text)

    def test_fast_barge_in_path_is_present(self):
        self.assertIn("fast=True", self.main_text)
        self.assertIn("minimum_level=40.0", self.main_text)
        self.assertIn("required_blocks=2", self.main_text)
        self.assertIn("fast: bool = False", self.echo_text)
        self.assertIn("if warming and fast", self.echo_text)

    def test_voice_style_directives_cover_natural_delivery(self):
        expected = (
            "VOICE INTERACTION MODE:",
            "continuous hands-free, full-duplex",
            "natural turn-taking",
            "natural pauses, pacing",
            "calm, intelligent, polished, articulate, confident",
            "mm-hmm",
            "uh-huh",
            "yield immediately",
        )
        for phrase in expected:
            self.assertIn(phrase, self.main_text)

    def test_hands_free_is_the_default_mode(self):
        self.assertFalse(config_manager.get_setting("push_to_talk_enabled", False))

    def test_emotion_linked_prosody_is_wired_into_live_and_fallback_speech(self):
        self.assertIn("from core.prosody import profile_for_text, profile_prompt_block", self.main_text)
        self.assertIn("profile.prompt_directive()", self.main_text)
        self.assertIn("rate=profile.edge_rate", self.main_text)
        self.assertIn("pitch=profile.edge_pitch", self.main_text)
        self.assertIn("sapi_rate=profile.sapi_rate", self.main_text)
        self.assertIn("Vary cadence naturally within the utterance", self.main_text)

    def test_gate_and_generation_primitives_are_functional(self):
        gate = BargeInGate(required_blocks=2, minimum_level=10)
        self.assertFalse(gate.observe(is_user_speech=True, level=20))
        self.assertTrue(gate.observe(is_user_speech=True, level=20))
        generation = PlaybackGeneration()
        first = generation.current()
        self.assertEqual(generation.bump(), first + 1)


if __name__ == "__main__":
    unittest.main()
