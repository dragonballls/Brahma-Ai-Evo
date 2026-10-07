from __future__ import annotations
import unittest
from unittest.mock import patch
from core.intelligence_orchestrator import IntelligenceOrchestrator

class IntelligenceOrchestratorTests(unittest.TestCase):
    def test_profile_matching_uses_word_boundaries(self):
        from core.intelligence_orchestrator import profile_for

        cfg = {"default_profile": "smart", "simple_profile": "fast", "simple_max_chars": 220, "simple_keywords": ("hello",)}
        self.assertEqual(profile_for("What does capitalization mean?", None, cfg), "smart")
        self.assertEqual(profile_for("Please debug this Python function.", None, cfg), "coding")
        self.assertEqual(profile_for("Look at this image.", None, cfg), "vision")
        self.assertEqual(profile_for("Please decode this value.", None, cfg), "smart")


    def test_simple_request_uses_fast_single_call(self):
        calls=[]
        def fake_chat(**kwargs):
            calls.append(kwargs)
            return "fast answer"
        with patch("core.intelligence_orchestrator.cloud_client.chat", side_effect=fake_chat):
            result=IntelligenceOrchestrator().respond("hello")
        self.assertEqual(result,"fast answer")
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]["model"],"auto/fast")

    def test_complex_request_uses_two_specialists_and_synthesis(self):
        calls=[]
        def fake_chat(**kwargs):
            calls.append(kwargs)
            return f"answer {len(calls)}"
        with patch("core.intelligence_orchestrator.cloud_client.chat", side_effect=fake_chat):
            result=IntelligenceOrchestrator().respond("Compare two complex software architectures and explain their tradeoffs.")
        self.assertEqual(result,"answer 3")
        self.assertEqual(len(calls),3)
        self.assertEqual([x["model"] for x in calls],["auto/smart","auto/smart","auto/smart"])

    def test_ui_orchestration_toggle_can_disable_multi_model_consensus(self):
        from pathlib import Path

        source = Path("core/intelligence_orchestrator.py").read_text(encoding="utf-8")
        self.assertIn('intelligence_orchestration_enabled", True', source)
        self.assertIn('bool(d.get("intelligence_orchestration_enabled", True))', source)
    def test_disabled_mode_falls_back_to_one_call(self):
        with patch("core.intelligence_orchestrator.load_config", return_value={
            "enabled":False,"default_profile":"smart","simple_profile":"fast",
            "parallel_workers":4,"max_specialists":2,"simple_max_chars":220,
            "simple_keywords":("hello",),"profiles":{
                "smart":{"model":"auto/smart","temperature":0.3,"max_tokens":4096,"specialists":2}
            }}), patch("core.intelligence_orchestrator.cloud_client.chat", return_value="direct") as call:
            result=IntelligenceOrchestrator().respond("complex request")
        self.assertEqual(result,"direct")
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["model"],"auto")

    def test_cross_provider_ensemble_fans_out_distinct_models_then_synthesizes(self):
        calls = []

        def fake_chat(**kwargs):
            calls.append(kwargs)
            if kwargs["model"] == "auto/smart":
                return "CONSENSUS"
            return "independent expert answer"

        with (
            patch("core.intelligence_orchestrator._configured_providers", return_value=("openai", "anthropic", "gemini")),
            patch("core.intelligence_orchestrator._catalog_models", return_value=(
                "openai/gpt-test-pro",
                "anthropic/claude-test-opus",
                "google/gemini-test-pro",
            )),
            patch("core.intelligence_orchestrator.cloud_client.chat", side_effect=fake_chat),
        ):
            result = IntelligenceOrchestrator().respond("Compare three complex architectures.", profile="smart")

        self.assertEqual(result, "CONSENSUS")
        panel_models = [item["model"] for item in calls if item["model"] != "auto/smart"]
        self.assertEqual(set(panel_models), {"openai/gpt-test-pro", "anthropic/claude-test-opus", "google/gemini-test-pro"})
        self.assertEqual(len(panel_models), 5)  # 3 experts + 2 cross-examiners
        synth = [item for item in calls if item["model"] == "auto/smart"]
        self.assertEqual(len(synth), 1)
        critiques = [item for item in calls if "adversarial cross-examination" in item["system"]]
        self.assertEqual(len(critiques), 2)
        self.assertIn("=== Source 1 ===", synth[0]["prompt"])
        self.assertIn("Cross-examination:", synth[0]["prompt"])
        self.assertNotIn("openai/gpt-test-pro", synth[0]["prompt"])

    def test_ensemble_can_be_disabled_without_changing_standard_path(self):
        config = {
            "enabled": True,
            "ensemble_enabled": False,
            "default_profile": "smart",
            "simple_profile": "fast",
            "parallel_workers": 4,
            "max_specialists": 2,
            "simple_max_chars": 220,
            "simple_keywords": ("hello",),
            "profiles": {
                "smart": {"model": "auto/smart", "temperature": 0.3, "max_tokens": 4096, "specialists": 0, "ensemble": True}
            },
        }
        with patch(
            "core.intelligence_orchestrator.load_config", return_value=config
        ), patch(
            "core.intelligence_orchestrator.cloud_client.chat", return_value="direct"
        ) as call:
            result = IntelligenceOrchestrator().respond("complex request")
        self.assertEqual(result, "direct")
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs["model"], "auto/smart")

if __name__=="__main__":
    unittest.main()


def test_structured_orchestrator_rejects_non_object_json_and_uses_fail_safe_fallback(monkeypatch):
    from core import intelligence_orchestrator

    cfg = {
        "enabled": True,
        "profiles": {
            "smart": {
                "model": "auto/smart",
                "specialists": 0,
                "max_tokens": 100,
                "temperature": 0.1,
            }
        },
        "parallel_workers": 1,
        "max_specialists": 1,
    }
    monkeypatch.setattr(intelligence_orchestrator, "load_config", lambda: cfg)
    monkeypatch.setattr(intelligence_orchestrator, "allowed", lambda: True)
    monkeypatch.setattr(
        intelligence_orchestrator.cloud_client,
        "chat",
        lambda *a, **k: "[]",
    )
    monkeypatch.setattr(
        intelligence_orchestrator.cloud_client,
        "chat_json",
        lambda *a, **k: {"success": True},
    )

    result = intelligence_orchestrator.IntelligenceOrchestrator().respond_json("return an object")
    assert result == {"success": True}


def test_structured_ensemble_judge_rejects_non_object_json(monkeypatch):
    from core import intelligence_orchestrator as orch

    c = dict(orch.DEFAULTS)
    c["ensemble_enabled"] = True
    monkeypatch.setattr(orch, "load_config", lambda: c)
    monkeypatch.setattr(orch, "allowed", lambda: True)
    monkeypatch.setattr(orch, "_ensemble_models", lambda *args, **kwargs: [("openai", "openai/test")])
    monkeypatch.setattr(orch, "_ensemble_roles", lambda *args, **kwargs: ("primary",))
    monkeypatch.setattr(orch.cloud_client, "chat", lambda *args, **kwargs: "[]")
    monkeypatch.setattr(
        orch.cloud_client,
        "chat_json",
        lambda *args, **kwargs: {"success": True, "source": "safe-fallback"},
    )

    result = orch.IntelligenceOrchestrator().respond_json("test")
    assert result == {"success": True, "source": "safe-fallback"}
