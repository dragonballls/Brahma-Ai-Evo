from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            with patch.object(type(engine), "offline_mode", new_callable=__import__("unittest").mock.PropertyMock, return_value=True):
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
                        result = engine._rank_opportunities([])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["goal"], "add useful feature")

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
