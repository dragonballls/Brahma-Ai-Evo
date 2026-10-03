"""Regression tests for the emotional-state dynamic feature."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EmotionalFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "features" / "emotional_state.py"
        spec = importlib.util.spec_from_file_location("test_emotional_feature", path)
        cls.module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(cls.module)

    def test_current_state_shape(self):
        result = self.module.execute(action="current")
        self.assertTrue(result["success"])
        self.assertIn(result["state"], {"neutral", "warm", "curious", "amused", "concerned", "stern", "frustrated", "urgent"})

    def test_assess_returns_prompt_grounding(self):
        result = self.module.execute(action="assess", text="Call me out if I'm making a bad decision.")
        self.assertTrue(result["success"])
        self.assertEqual(result["state"], "stern")
        self.assertIn("[EMOTIONAL DELIVERY]", result["prompt"])


if __name__ == "__main__":
    unittest.main()
