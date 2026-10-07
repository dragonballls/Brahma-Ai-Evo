from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from core.evolution_engine import EvolutionEngine


class ContinuousEvolutionTests(unittest.TestCase):
    def test_defaults_are_sleeping_and_low_frequency(self):
        engine = EvolutionEngine(tempfile.gettempdir())
        self.assertGreaterEqual(engine.interval_seconds, 30 * 60)
        self.assertGreaterEqual(engine.initial_delay_seconds, 60)

    def test_state_persists_outside_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            custom = Path(tmp) / "not_state"
            engine._state_path = custom / "evolution" / "state.json"
            engine._set_state(last_error="test")
            second = EvolutionEngine(tmp)
            second._state_path = engine._state_path
            second._state = second._load_state()
            self.assertEqual(second._state["last_error"], "test")

    def test_offline_mode_never_researches_or_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            with patch.object(type(engine), "offline_mode", new_callable=PropertyMock, return_value=True):
                result = engine.run_cycle(force=True)
            self.assertEqual(result["status"], "offline")

    def test_paused_mode_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            engine._set_state(paused=True)
            result = engine.run_cycle()
            self.assertEqual(result["status"], "paused")

    def test_candidate_ranking_requires_two_sources_and_low_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            payload = {
                "candidates": [
                    {
                        "goal": "add useful feature",
                        "reason": "good",
                        "expected_benefit": "better",
                        "risk": 0.2,
                        "confidence": 0.9,
                        "source_repositories": ["a/one", "b/two"],
                    },
                    {
                        "goal": "unsafe feature",
                        "reason": "bad",
                        "expected_benefit": "none",
                        "risk": 0.8,
                        "confidence": 0.99,
                        "source_repositories": ["a/one", "b/two"],
                    },
                    {
                        "goal": "single source",
                        "reason": "weak",
                        "expected_benefit": "none",
                        "risk": 0.1,
                        "confidence": 0.99,
                        "source_repositories": ["a/one"],
                    },
                ]
            }
            with patch("core.evolution_engine.EvolutionEngine._repo_context", return_value="local"):
                with patch("core.evolution_engine.GitHubResearchClient.research_goal", return_value={
                    "available": True,
                    "repositories": [],
                    "code_matches": [],
                }):
                    with patch("llm_client.client.intelligent_json", return_value=payload):
                        # The helper is exercised directly without creating a Git branch.
                        research = [{
                            "domain": "agent orchestration",
                            "result": {
                                "repositories": [
                                    {"repository": "a/one", "license_class": "permissive"},
                                    {"repository": "b/two", "license_class": "permissive"},
                                ],
                                "code_matches": [],
                            },
                        }]
                        result = engine._rank_opportunities(research)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["goal"], "add useful feature")

    def test_main_exposes_evolution_controller_without_auto_promotion(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('"name": "evolution"', source)
        self.assertIn('scan_now | pause | resume', source)
        self.assertIn("evolution_engine.start()", source)
        self.assertIn("evolution_engine.stop()", source)
        self.assertIn("never auto-promotes changes", source.lower())

    def test_self_coding_can_restore_base_branch_after_background_preview(self):
        import inspect
        from core.self_coding import SelfCodingAgent
        source = inspect.getsource(SelfCodingAgent._preview_unlocked)
        self.assertIn("return_to_base", source)
        self.assertIn('result["returned_to_base"] = base_branch', source)

    def test_cycle_never_auto_promotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            with patch.object(engine, "_repo_is_ready", return_value=(True, "ready")):
                with patch.object(engine, "_research_domains", return_value=[]):
                    with patch.object(engine, "_rank_opportunities", return_value=[{
                        "goal": "candidate",
                        "reason": "test",
                        "expected_benefit": "test",
                        "risk": 0.1,
                        "confidence": 0.95,
                        "source_repositories": ["a/one", "b/two"],
                    }]):
                        with patch("core.self_coding.SelfCodingAgent.preview", return_value={
                            "success": True,
                            "state": "pending",
                            "checkpoint_id": "demo",
                        }) as preview:
                            result = engine.run_cycle(force=True)
            self.assertEqual(result["status"], "pending")
            preview.assert_called_once()
            self.assertEqual(engine._state["candidates"][-1]["state"], "pending")


if __name__ == "__main__":
    unittest.main()


    def test_corrupt_evolution_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            engine._state_path = Path(tmp) / "evolution" / "state.json"
            engine._state_path.parent.mkdir(parents=True, exist_ok=True)
            engine._state_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                engine._load_state()

    def test_state_save_failure_rolls_back_in_memory_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            before = dict(engine._state)
            with patch.object(engine, "_save_state", side_effect=RuntimeError("disk failure")):
                with self.assertRaises(RuntimeError):
                    engine._set_state(last_error="not persisted")
            self.assertEqual(engine._state, before)

    def test_offline_mode_fails_closed_when_settings_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = EvolutionEngine(tmp)
            with patch("memory.config_manager.get_setting", side_effect=RuntimeError("corrupt settings")):
                self.assertTrue(engine.offline_mode)
