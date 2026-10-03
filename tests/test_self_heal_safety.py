from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions.auto_heal_engine import TracebackAnalyzer, SafetySandbox, auto_heal


class SelfHealingSafetySmokeTests(unittest.TestCase):
    def test_traceback_analysis_and_sandbox_are_bounded(self):
        traceback_text = (
            "Traceback (most recent call last):\n"
            "  File \"actions/test_action.py\", line 7, in test_action\n"
            "ZeroDivisionError: demo"
        )
        parsed = TracebackAnalyzer.parse(traceback_text)
        self.assertTrue(parsed["success"])
        self.assertEqual(Path(parsed["target_file"]).name, "test_action.py")
        valid, error = SafetySandbox.validate_code("def ok():\n    return 1")
        self.assertTrue(valid, error)

    def test_history_action_does_not_require_a_model(self):
        result = auto_heal({"action": "history"})
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())


if __name__ == "__main__":
    unittest.main()
