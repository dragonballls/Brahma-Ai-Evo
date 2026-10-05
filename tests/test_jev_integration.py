from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class JevIntegrationTests(unittest.TestCase):
    def test_system_one_is_disabled_without_key(self):
        from core.jev_system_one import JevSystemOne
        client = JevSystemOne()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}, clear=False):
            self.assertIsNone(client.evaluate(
                state={"x": 1},
                questions={"q": {"type": "noul", "instructions": "Is x useful?"}},
            ))

    def test_memory_gate_falls_back_when_jev_unavailable(self):
        from core.jev_memory import should_store
        with patch("core.jev_memory.jev.evaluate", side_effect=Exception("offline")):
            # Caller remains responsible for fallback when the optional controller fails.
            with self.assertRaises(Exception):
                should_store("temporary status", "test")

    def test_browser_adapter_is_nonfatal_without_key(self):
        from core.jev_browser import JevBrowserUnavailable, run_goal
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}, clear=False):
            with self.assertRaises(JevBrowserUnavailable):
                run_goal("https://example.com", "Open the page")

    def test_existing_browser_stack_has_jev_goal_path(self):
        from pathlib import Path
        source = Path("actions/browser_control.py").read_text(encoding="utf-8")
        self.assertIn('action in {"goal", "agent", "ultrafast"}', source)
        self.assertIn("run_goal", source)

    def test_memory_manager_calls_jev_admission_gate(self):
        from pathlib import Path
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("jev_memory.should_store", source)


if __name__ == "__main__":
    unittest.main()
