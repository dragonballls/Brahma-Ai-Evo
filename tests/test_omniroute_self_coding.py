from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

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
from core.github_research import GitHubResearchClient, _result_score


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

    def test_omniroute_startup_is_headless(self):
        source = Path(ROOT / "core" / "omniroute_setup.py").read_text(encoding="utf-8")
        self.assertIn("command = self.command_argv(for_start=True)", source)
        self.assertIn('if for_start and "--no-open" not in command:', source)

    def test_embedded_dashboard_is_first_class_page(self):
        source = Path(ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertIn("class OmniRouteEmbeddedPage(QWidget):", source)
        self.assertIn("self._omniroute_page = OmniRouteEmbeddedPage()", source)
        self.assertIn("self._center_stack.addWidget(self._omniroute_page)", source)
        self.assertIn("stack.setCurrentWidget(page)", source)
        self.assertIn("def _start_gateway(self):", source)
        self.assertIn("self._retry_timer", source)
        self.assertIn('"OmniRoute connected"', source)
        self.assertNotIn("QDialog(self.window())", source)

    def test_omniroute_is_local_and_lazy(self):
        from core.omniroute import OmniRouteGateway

        gateway = OmniRouteGateway()
        self.assertEqual(gateway.base_url, "http://127.0.0.1:20128/v1")
        self.assertFalse(gateway._ready)


    def test_github_research_ranks_and_synthesizes_multiple_sources(self):
        client = GitHubResearchClient()
        repos = [
            {
                "full_name": "example/permissive",
                "description": "websocket reference implementation",
                "stargazers_count": 100,
                "forks_count": 20,
                "updated_at": "2026-09-20T00:00:00Z",
                "license": {"spdx_id": "MIT", "name": "MIT License"},
                "html_url": "https://github.com/example/permissive",
            },
            {
                "full_name": "example/copyleft",
                "description": "websocket alternative",
                "stargazers_count": 500,
                "forks_count": 100,
                "updated_at": "2026-09-20T00:00:00Z",
                "license": {"spdx_id": "AGPL-3.0", "name": "GNU AGPL"},
                "html_url": "https://github.com/example/copyleft",
            },
            {
                "full_name": "example/third",
                "description": "another websocket implementation",
                "stargazers_count": 50,
                "forks_count": 10,
                "updated_at": "2026-09-20T00:00:00Z",
                "license": {"spdx_id": "Apache-2.0", "name": "Apache License 2.0"},
                "html_url": "https://github.com/example/third",
            },
        ]
        code = [
            {
                "repository": repos[0],
                "path": "src/websocket.py",
                "score": 100.0,
                "html_url": "https://github.com/example/permissive/blob/main/src/websocket.py",
            },
            {
                "repository": repos[1],
                "path": "server/websocket.py",
                "score": 99.0,
                "html_url": "https://github.com/example/copyleft/blob/main/server/websocket.py",
            },
            {
                "repository": repos[2],
                "path": "lib/websocket.py",
                "score": 98.0,
                "html_url": "https://github.com/example/third/blob/main/lib/websocket.py",
            },
        ]
        with (
            patch.object(client, "search_repositories", side_effect=[repos, []]),
            patch.object(client, "search_code", return_value=code),
        ):
            result = client.research_goal("add websocket reconnect handling", repo_limit=8, code_limit=12)

        self.assertTrue(result["available"])
        self.assertGreaterEqual(len(result["repositories"]), 3)
        self.assertEqual(result["code_matches"][0]["repository"], "example/permissive")
        self.assertIn("GITHUB-FIRST RESEARCH PREFLIGHT", client.format_dossier(result))
        self.assertGreaterEqual(_result_score(repos[0], 100.0, 4), _result_score(repos[1], 99.0, 3))

    def test_github_first_edit_guard_requires_multiple_sources(self):
        from actions.brahma_dev_agent import BrahmaDevAgent

        with tempfile.TemporaryDirectory() as tmp:
            agent = BrahmaDevAgent(tmp)
            blocked_before_research = agent._execute_tool(
                "FileWrite",
                {"file_path": "demo.txt", "content": "hello"},
            )
            self.assertIn("GitHub-first guard", blocked_before_research)

            agent.github_research_done = True
            agent.github_required_sources = 2
            blocked_with_one = agent._execute_tool(
                "FileWrite",
                {"file_path": "demo.txt", "content": "hello"},
            )
            self.assertIn("2 viable repositories", blocked_with_one)

            agent.github_inspected_repos.update({"a/repo", "b/repo"})
            written = agent._execute_tool(
                "FileWrite",
                {"file_path": "demo.txt", "content": "hello"},
            )
            self.assertIn("Successfully wrote", written)
            self.assertEqual(Path(tmp, "demo.txt").read_text(encoding="utf-8"), "hello")

    def test_github_tools_are_exposed_to_brahma_dev(self):
        source = Path(ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
        self.assertIn("GitHubSearch", source)
        self.assertIn("GitHubRead", source)
        self.assertIn("GitHubRepo", source)
        self.assertIn("_github_research_preflight", source)
        self.assertIn("self.github_required_sources = min(3", source)


if __name__ == "__main__":
    unittest.main()
