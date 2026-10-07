from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import settings_agent
from memory import config_manager


class ConversationalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.settings_file = self.config_dir / "app_settings.json"
        self.patcher_dir = patch.object(config_manager, "CONFIG_DIR", self.config_dir)
        self.patcher_file = patch.object(config_manager, "SETTINGS_FILE", self.settings_file)
        self.patcher_dir.start()
        self.patcher_file.start()

    def tearDown(self):
        self.patcher_file.stop()
        self.patcher_dir.stop()
        self.tmp.cleanup()

    def test_simple_natural_language_toggle(self):
        self.assertEqual(
            settings_agent.deterministic_plan("turn off startup animation"),
            [{"key": "startup_animation_enabled", "value": False}],
        )

    def test_outcome_language_maps_to_multiple_settings(self):
        plan = settings_agent.deterministic_plan("make Brahma lighter on my PC")
        self.assertEqual(
            {item["key"]: item["value"] for item in plan},
            {
                "desktop_performance_profile": "efficiency",
                "show_desktop_performance_overlay": False,
                "startup_animation_enabled": False,
            },
        )

    def test_provider_phrasing_maps_without_exact_setting_name(self):
        plan = settings_agent.deterministic_plan("use OpenRouter by default")
        self.assertEqual(
            {item["key"]: item["value"] for item in plan},
            {"default_ai_provider": "OpenRouter"},
        )

    def test_validation_rejects_unknown_setting(self):
        with self.assertRaises(ValueError):
            settings_agent.validate("not_a_real_setting", True)

    def test_validation_rejects_bad_numeric_range(self):
        with self.assertRaises(ValueError):
            settings_agent.validate("sound_effects_volume", 140)

    def test_validation_rejects_nonfinite_numeric_values(self):
        with self.assertRaises(ValueError):
            settings_agent.validate("sound_effects_volume", float("nan"))
        with self.assertRaises(ValueError):
            settings_agent.validate("sound_effects_volume", float("inf"))

    def test_apply_persists_and_reports_changes(self):
        result = settings_agent.apply("set sound effects volume to 42")
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"][0]["new"], 42)
        self.assertEqual(settings_agent.load_settings()["sound_effects_volume"], 42)

    @patch("llm_client.client.intelligent_json")
    def test_llm_interpreter_builds_json_prompt(self, intelligent_json):
        intelligent_json.return_value = {
            "patches": [{"key": "push_to_talk_enabled", "value": True}],
        }
        patches = settings_agent._llm_plan("configure voice control the way I described")
        self.assertEqual(patches, [{"key": "push_to_talk_enabled", "value": True}])
        prompt = intelligent_json.call_args.args[0]
        self.assertIn('"patches"', prompt)
        self.assertIn('"push_to_talk_enabled"', prompt)

    @patch("core.settings_agent._llm_plan")
    def test_unknown_request_falls_back_to_interpreter(self, llm_plan):
        llm_plan.return_value = [{"key": "push_to_talk_enabled", "value": True}]
        result = settings_agent.apply("configure voice control the way I described")
        self.assertTrue(result["ok"])
        self.assertTrue(settings_agent.load_settings()["push_to_talk_enabled"])
        llm_plan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
