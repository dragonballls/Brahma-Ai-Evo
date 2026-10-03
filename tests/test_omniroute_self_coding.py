from __future__ import annotations

import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.omniroute_setup import (
    DEFAULT_PORT,
    OMNIROUTE_PACKAGE,
    OMNIROUTE_VERSION,
    detect_provider_from_key,
)
from core.self_coding import Checkpoint, SelfCodingAgent


class OmniRouteSelfCodingTests(unittest.TestCase):
    def test_omniroute_release_contract(self):
        self.assertEqual(DEFAULT_PORT, 20128)
        self.assertEqual(OMNIROUTE_VERSION, "3.8.50")
        self.assertEqual(OMNIROUTE_PACKAGE, "omniroute@3.8.50")

    def test_provider_detection_contract(self):
        self.assertEqual(detect_provider_from_key("sk-ant-example"), "anthropic")
        self.assertEqual(detect_provider_from_key("sk-or-v1-example"), "openrouter")
        self.assertEqual(detect_provider_from_key("gsk_example"), "groq")
        self.assertEqual(detect_provider_from_key("xai-example"), "xai")
        self.assertEqual(detect_provider_from_key("AIzaExample"), "gemini")
        self.assertEqual(detect_provider_from_key("sk-proj-example"), "openai")
        self.assertIsNone(detect_provider_from_key("ambiguous-example"))

    def test_checkpoint_is_durable_shape(self):
        checkpoint = Checkpoint(
            checkpoint_id="demo",
            branch="agent/checkpoint/demo",
            baseline="a" * 40,
            base_branch="main",
            commits=("b" * 40,),
            created_at="2026-01-01T00:00:00+00:00",
            state="pending",
        )
        self.assertEqual(checkpoint.state, "pending")
        self.assertEqual(checkpoint.commits[-1], "b" * 40)
        self.assertEqual(checkpoint.promoted_sha, None)

    def test_self_coding_requires_real_checkout(self):
        self.assertTrue(hasattr(SelfCodingAgent, "preview"))
        self.assertTrue(hasattr(SelfCodingAgent, "approve"))
        self.assertTrue(hasattr(SelfCodingAgent, "undo"))
        self.assertTrue(hasattr(SelfCodingAgent, "list_checkpoints"))

    def test_self_coding_git_guard_contract(self):
        import inspect

        source = inspect.getsource(SelfCodingAgent)
        self.assertIn("Repository is not clean", source)
        self.assertIn("Self-coding requires an attached Git branch", source)
        self.assertIn("main changed since preview; refusing promotion", source)
        self.assertIn("main changed after approval; refusing to undo unrelated work", source)
        self.assertIn("BRAHMA_SELF_CODING_REPO", source)

    def test_runtime_wiring_contracts(self):
        from core.omniroute import OmniRouteGateway
        from core.self_coding import SelfCodingAgent

        or_source = Path(ROOT / "or_client.py").read_text(encoding="utf-8")
        main_source = Path(ROOT / "main.py").read_text(encoding="utf-8")
        dev_source = Path(ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")

        self.assertIn("def _call_omniroute", or_source)
        self.assertIn("auto/coding", dev_source)
        self.assertIn('"name": "omniroute"', main_source)
        self.assertIn('"name": "self_coding"', main_source)
        self.assertIn("checkpoint", main_source)
        self.assertIn("approve", main_source)
        self.assertIn("undo", main_source)
        spec_source = Path(ROOT / "installer" / "BrahmaEvo.spec").read_text(encoding="utf-8")
        self.assertIn("build_vendor", spec_source)
        self.assertIn("omniroute_runtime", spec_source)
        self.assertIn("prepare_omniroute_runtime.py", Path(ROOT / "build_all.ps1").read_text(encoding="utf-8"))
        self.assertTrue(hasattr(OmniRouteGateway, "ensure_ready"))
        self.assertTrue(hasattr(SelfCodingAgent, "preview"))

    def test_omniroute_is_local_and_lazy(self):
        from core.omniroute import OmniRouteGateway

        gateway = OmniRouteGateway()
        self.assertEqual(gateway.base_url, "http://127.0.0.1:20128/v1")
        self.assertFalse(gateway._ready)


if __name__ == "__main__":
    unittest.main()
