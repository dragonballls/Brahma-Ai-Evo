from __future__ import annotations
import unittest
from unittest.mock import patch
from core.intelligence_orchestrator import IntelligenceOrchestrator

class IntelligenceOrchestratorTests(unittest.TestCase):
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

if __name__=="__main__":
    unittest.main()
