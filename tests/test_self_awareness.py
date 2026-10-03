"""Regression tests for functional self-awareness and entity grounding."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.self_model import SelfAwareness

class SelfAwarenessTests(unittest.TestCase):
    def test_user_and_assistant_pronouns_are_distinct(self):
        awareness = SelfAwareness()
        self.assertEqual(awareness.classify_reference("I"), "user")
        self.assertEqual(awareness.classify_reference("me"), "user")
        self.assertEqual(awareness.classify_reference("my"), "user")
        self.assertEqual(awareness.classify_reference("you"), "self")
        self.assertEqual(awareness.classify_reference("your"), "self")

    def test_first_person_flips_for_assistant_speaker(self):
        awareness = SelfAwareness()
        self.assertEqual(awareness.classify_reference("I", speaker="assistant"), "self")
        self.assertEqual(awareness.classify_reference("my", speaker="assistant"), "self")
        self.assertEqual(awareness.classify_reference("you", speaker="assistant"), "user")

    def test_assistant_aliases_and_devices_are_separate(self):
        awareness = SelfAwareness()
        self.assertEqual(awareness.classify_reference("Jarvis"), "self")
        self.assertEqual(awareness.classify_reference("Brahma Evo"), "self")
        self.assertEqual(awareness.classify_reference("my phone"), "device")
        self.assertEqual(awareness.classify_reference("PC"), "device")

    def test_turn_resolution_captures_entity_mentions(self):
        awareness = SelfAwareness()
        result = awareness.resolve_turn("Can you open my phone and tell me what you see?")
        by_surface = {(x["surface"].lower(), x["entity"]) for x in result["mentions"]}
        self.assertIn(("you", "self"), by_surface)
        self.assertIn(("my", "user"), by_surface)
        self.assertIn(("phone", "device"), by_surface)

    def test_prompt_block_contains_identity_boundaries(self):
        with patch.object(SelfAwareness, "_owner_name", return_value="Alex"), patch.object(SelfAwareness, "_assistant_name", return_value="Brahma"):
            block = SelfAwareness().prompt_block("Can you open my PC?")
        self.assertIn("SELF = Brahma", block)
        self.assertIn("USER = Alex", block)
        self.assertIn("I/me/my/mine/myself refer to USER", block)
        self.assertIn("you/your/yourself refer to SELF", block)
        self.assertIn("CURRENT TURN ENTITY MAP", block)
        self.assertIn('"my" -> user', block)
        self.assertIn('"PC" -> device', block)

    def test_identity_answer_never_guesses_missing_user_name(self):
        with patch.object(SelfAwareness, "_owner_name", return_value=""):
            ans = SelfAwareness().identity_answer("who am i")
        self.assertIn("won’t guess", ans)

    def test_identity_answer_uses_stored_user_name(self):
        with patch.object(SelfAwareness, "_owner_name", return_value="Alex"):
            ans = SelfAwareness().identity_answer("who am i")
        self.assertIn("Alex", ans)

    def test_runtime_action_state_is_persistent(self):
        with tempfile.TemporaryDirectory() as td, patch("core.self_model.PATH", Path(td) / "self_awareness.json"):
            awareness = SelfAwareness()
            awareness.record_action("open_app", "success", task="Open Chrome")
            saved = json.loads((Path(td) / "self_awareness.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["last_action"], "open_app")
            self.assertEqual(saved["last_action_status"], "success")
            self.assertEqual(saved["current_task"], "Open Chrome")

    def test_module_has_no_background_worker_dependency(self):
        import core.self_model as module
        self.assertFalse(hasattr(module, "threading"))


    def test_feature_wrapper_exposes_identity_actions(self):
        from features.self_awareness import execute
        with patch.object(SelfAwareness, "_owner_name", return_value="Alex"), patch.object(SelfAwareness, "_assistant_name", return_value="Brahma"):
            self.assertTrue(execute(action="snapshot")["success"])
            self.assertEqual(execute(action="classify", text="you")["entity"], "self")
            self.assertEqual(execute(action="classify", text="me")["entity"], "user")
            self.assertIn("Alex", execute(action="answer", text="who am i")["answer"])

if __name__ == "__main__":
    unittest.main()
