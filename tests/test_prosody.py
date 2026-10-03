"""Regression tests for emotion-linked speech rate and prosody."""
from __future__ import annotations

import unittest

from core.prosody import profile_for_state, profile_for_text, profile_prompt_block


class ProsodyTests(unittest.TestCase):
    def test_emotions_map_to_distinct_speaking_rates(self):
        concerned = profile_for_state("concerned", 0.9)
        neutral = profile_for_state("neutral", 0.9)
        urgent = profile_for_state("urgent", 0.9)
        self.assertLess(concerned.rate_percent, neutral.rate_percent)
        self.assertGreater(urgent.rate_percent, neutral.rate_percent)

    def test_intensity_scales_the_emotional_rate_change(self):
        soft = profile_for_state("urgent", 0.35)
        strong = profile_for_state("urgent", 1.0)
        self.assertLess(soft.rate_percent, strong.rate_percent)

    def test_pitch_and_pause_profile_track_emotional_delivery(self):
        warm = profile_for_state("warm", 0.8)
        concerned = profile_for_state("concerned", 0.8)
        self.assertGreater(warm.pitch_hz, 0)
        self.assertLess(concerned.pitch_hz, 0)
        self.assertGreater(concerned.pause_scale, warm.pause_scale)

    def test_fallback_values_are_clamped_and_format_correctly(self):
        profile = profile_for_state("urgent", 1.0)
        self.assertTrue(profile.edge_rate.endswith("%"))
        self.assertTrue(profile.edge_pitch.endswith("Hz"))
        self.assertLessEqual(profile.sapi_rate, 10)
        self.assertGreaterEqual(profile.sapi_rate, -10)

    def test_text_shape_adds_small_natural_variation(self):
        short = profile_for_text("Understood.", state="neutral", intensity=0.5)
        long = profile_for_text("x" * 260, state="neutral", intensity=0.5)
        self.assertGreater(short.rate_percent, long.rate_percent)
        self.assertEqual(short.pitch_hz, long.pitch_hz)

    def test_live_prompt_contains_dynamic_prosody_policy(self):
        prompt = profile_prompt_block()
        self.assertIn("do not use one fixed cadence", prompt)
        self.assertIn("slow slightly before important points", prompt)
        self.assertIn("urgent:", prompt)
        self.assertIn("concerned:", prompt)


if __name__ == "__main__":
    unittest.main()
