from __future__ import annotations

import unittest
from pathlib import Path

from core.capability_catalog import CAPABILITY_GROUPS, capability_terms, prompt_block


ROOT = Path(__file__).resolve().parents[1]


class CapabilityUsabilityTests(unittest.TestCase):
    def test_capability_catalog_is_central_and_nonempty(self):
        self.assertGreaterEqual(len(CAPABILITY_GROUPS), 6)
        self.assertIn("spotify", capability_terms())
        self.assertIn("smart home", capability_terms())
        self.assertIn("spreadsheet", capability_terms())

    def test_prompt_explicitly_removes_internal_command_dependency(self):
        prompt = prompt_block()
        self.assertIn("Users never need to know internal tool names", prompt)
        self.assertIn("ordinary natural-language requests", prompt)
        self.assertIn("do not ask the user to restate a request using a special syntax", prompt)

    def test_main_uses_catalog_for_natural_feature_requests(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("from core.capability_catalog import prompt_block as capability_prompt_block", source)
        self.assertIn("from core.capability_catalog import capability_terms", source)

    def test_action_router_avoids_common_word_false_positives(self):
        import main

        self.assertFalse(main._looks_like_action_request("Tell me about phone cases."))
        self.assertFalse(main._looks_like_action_request("What do you think about my room?"))
        self.assertFalse(main._looks_like_action_request("Can you explain what an app is?"))
        self.assertTrue(main._looks_like_action_request("Show my connected devices."))
        self.assertTrue(main._looks_like_action_request("Play something on Spotify."))

    def test_chat_exposes_one_consistent_quick_action_surface(self):
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        for label in ("Computer", "Web", "Create", "Media", "Devices", "More"):
            self.assertIn(f'("{label}"', source)
        self.assertIn("self.command_submitted.emit(command)", source)


if __name__ == "__main__":
    unittest.main()
