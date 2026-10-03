"""Regression tests for the explicit conversational language lock."""
from __future__ import annotations

import unittest

from core.language_policy import DEFAULT_LANGUAGE, is_language_switch_request, prompt_block


class LanguagePolicyTests(unittest.TestCase):
    def test_default_language_is_english(self):
        self.assertEqual(DEFAULT_LANGUAGE, "English")

    def test_language_switch_requires_explicit_request(self):
        self.assertTrue(is_language_switch_request("Please answer in Spanish."))
        self.assertTrue(is_language_switch_request("Switch to Japanese."))
        self.assertTrue(is_language_switch_request("From now on, speak French."))

    def test_quoted_or_pasted_language_is_not_a_switch_request(self):
        self.assertFalse(is_language_switch_request("Translate 'bonjour' for me."))
        self.assertFalse(is_language_switch_request("This page is written in Spanish."))
        self.assertFalse(is_language_switch_request("Summarize this Hindi message: नमस्ते."))

    def test_policy_forbids_automatic_switching_and_mixing(self):
        prompt = prompt_block()
        self.assertIn("NEVER switch languages merely because the user writes", prompt)
        self.assertIn("Change response language only when the user explicitly requests", prompt)
        self.assertIn("Never mix languages for emotion, fillers, emphasis, or style", prompt)

    def test_policy_keeps_selected_language_until_explicitly_changed(self):
        prompt = prompt_block()
        self.assertIn("remain in that language until the user explicitly requests another", prompt)


if __name__ == "__main__":
    unittest.main()
