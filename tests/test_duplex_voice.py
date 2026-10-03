"""Regression tests for duplex voice coordination."""
from __future__ import annotations

import unittest

from core.duplex_voice import BargeInGate, PlaybackGeneration


class DuplexVoiceTests(unittest.TestCase):
    def test_barge_in_requires_two_confirming_blocks(self):
        gate = BargeInGate(required_blocks=2, minimum_level=20)
        self.assertFalse(gate.observe(is_user_speech=True, level=100))
        self.assertTrue(gate.observe(is_user_speech=True, level=100))
        self.assertEqual(gate.positive_blocks, 2)

    def test_barge_in_resets_on_non_speech_or_low_level(self):
        gate = BargeInGate(required_blocks=2, minimum_level=20)
        gate.observe(is_user_speech=True, level=100)
        self.assertFalse(gate.observe(is_user_speech=False, level=100))
        self.assertEqual(gate.positive_blocks, 0)
        self.assertFalse(gate.observe(is_user_speech=True, level=10))
        self.assertEqual(gate.positive_blocks, 0)

    def test_playback_generation_changes_after_interrupt(self):
        marker = PlaybackGeneration()
        first = marker.current()
        second = marker.bump()
        self.assertEqual(second, first + 1)
        self.assertEqual(marker.current(), second)
