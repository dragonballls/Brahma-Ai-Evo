from __future__ import annotations

import time
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.voice_guard import (
    VoiceCommandGate,
    VoiceToolExecutionGate,
    is_wake_phrase_only,
)


class VoiceGuardTests(unittest.TestCase):
    def test_exact_duplicate_transcript_is_rejected(self):
        gate = VoiceCommandGate(duplicate_window_s=2)
        self.assertTrue(gate.accept("open chrome"))
        self.assertFalse(gate.accept("open chrome"))

    def test_near_duplicate_transcript_is_rejected(self):
        gate = VoiceCommandGate(duplicate_window_s=2)
        self.assertTrue(gate.accept("open the chrome browser"))
        self.assertFalse(gate.accept("open the chrome browser."))

    def test_different_command_is_accepted(self):
        gate = VoiceCommandGate(duplicate_window_s=2)
        self.assertTrue(gate.accept("open chrome"))
        self.assertTrue(gate.accept("open notepad"))

    def test_wake_phrase_requires_a_command(self):
        self.assertTrue(is_wake_phrase_only("Hey Brahma Evo"))
        gate = VoiceToolExecutionGate()
        gate.add_input_fragment("Hey Brahma Evo")
        allowed, reason = gate.allow("computer_control", {"action": "click"})
        self.assertFalse(allowed)
        self.assertIn("wake phrase", reason)

    def test_tool_requires_fresh_voice_transcript(self):
        gate = VoiceToolExecutionGate()
        allowed, reason = gate.allow("computer_control", {"action": "click"})
        self.assertFalse(allowed)
        self.assertIn("fresh voice transcript", reason)

    def test_duplicate_tool_call_in_same_turn_is_blocked(self):
        gate = VoiceToolExecutionGate()
        gate.add_input_fragment("open chrome")
        args = {"action": "open", "target": "chrome"}
        self.assertTrue(gate.allow("computer_control", args)[0])
        self.assertFalse(gate.allow("computer_control", args)[0])

    def test_rapid_duplicate_tool_call_across_turns_is_blocked(self):
        gate = VoiceToolExecutionGate(rapid_repeat_window_s=2)
        args = {"action": "open", "target": "chrome"}

        gate.add_input_fragment("open chrome")
        self.assertTrue(gate.allow("computer_control", args)[0])
        gate.finish_turn()

        gate.add_input_fragment("open chrome")
        allowed, reason = gate.allow("computer_control", args)
        self.assertFalse(allowed)
        self.assertIn("rapid duplicate", reason)

    def test_cumulative_transcript_fragments_do_not_duplicate(self):
        gate = VoiceToolExecutionGate()
        gate.add_input_fragment("open")
        gate.add_input_fragment("open chrome")
        self.assertEqual(gate.current_text, "open chrome")

    def test_repeat_can_be_allowed_after_window(self):
        gate = VoiceCommandGate(duplicate_window_s=0.5)
        self.assertTrue(gate.accept("open chrome"))
        time.sleep(0.6)
        self.assertTrue(gate.accept("open chrome"))


if __name__ == "__main__":
    unittest.main()
