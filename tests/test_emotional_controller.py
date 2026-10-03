"""Regression tests for emotional delivery and tough-love boundaries."""
from __future__ import annotations

import unittest

from core.emotional_controller import EmotionalController


class EmotionalControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = EmotionalController()

    def test_tough_love_requests_stern_tone(self):
        state = self.controller.assess("Be blunt and call me out if I'm making a bad decision.")
        self.assertEqual(state.name, "stern")
        self.assertGreaterEqual(state.intensity, 0.7)

    def test_task_failure_can_sound_frustrated(self):
        state = self.controller.assess("The build failed again and the bug is still there.", task_failed=True)
        self.assertEqual(state.name, "frustrated")

    def test_urgent_request_has_higher_intensity(self):
        state = self.controller.assess("Do this immediately; it is critical.")
        self.assertEqual(state.name, "urgent")
        self.assertGreater(state.intensity, 0.8)

    def test_emotional_prompt_allows_forceful_criticism_without_degradation(self):
        prompt = self.controller.prompt_block("Call me out.", state=EmotionalController().assess(
            "Call me out."
        ))
        self.assertIn("be specific about the user's decision", prompt)
        self.assertIn("Never humiliate, degrade", prompt)
        self.assertIn("Never invent accusations", prompt)

    def test_slander_is_converted_to_fact_based_critique(self):
        prompt = self.controller.prompt_block("Slander me if necessary.")
        self.assertIn("verified facts", prompt)
        self.assertIn("fabricated claims", prompt)

    def test_distress_beats_requested_harshness(self):
        state = self.controller.assess(
            "Be harsh with me, but I'm scared and I really messed up.",
            user_requested_toughness=True,
        )
        self.assertEqual(state.name, "concerned")


if __name__ == "__main__":
    unittest.main()
