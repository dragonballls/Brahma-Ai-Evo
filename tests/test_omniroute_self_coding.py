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

    def test_self_coding_verification_compiles_connect_and_top_level_runtime(self):
        import inspect
        source = inspect.getsource(SelfCodingAgent._verify)
        self.assertIn('"brahma_connect"', source)
        self.assertIn('"updater.py"', source)
        self.assertIn('"or_client.py"', source)
        self.assertIn('"llm_client.py"', source)

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

    def test_omniroute_floating_dashboard_is_first_class_window(self):
        source = Path(ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertIn("class OmniRouteFloatingWindow(QDialog):", source)
        self.assertIn("self.gateway_ready.connect(", source)
        self.assertIn("self.gateway_ready.emit(ok)", source)
        self.assertIn("def _start_gateway(self):", source)
        self.assertIn("def _retry_gateway(self):", source)
        self.assertIn("omniroute_window_width", source)
        self.assertIn("omniroute_window_height", source)
        self.assertIn('self._status.setText("Connected")', source)
        self.assertIn("Qt.WindowType.Window", source)
        self.assertNotIn("class OmniRouteEmbeddedPage(QWidget):", source)
        self.assertNotIn("self._omniroute_page = OmniRouteEmbeddedPage()", source)
        self.assertNotIn("QDialog(self.window())", source)

    def test_ui_provider_testing_uses_canonical_gateway_lifecycle(self):
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertIn("gateway().configure_provider(provider, key)", source)
        self.assertNotIn("gateway().provisioner.configure_provider(provider, key)", source)

    def test_omniroute_lifecycle_lock_keeps_slow_startup_off_state_lock(self):
        source = (ROOT / "core" / "omniroute.py").read_text(encoding="utf-8")
        self.assertIn("self._lifecycle_lock = threading.Lock()", source)
        self.assertIn("with self._lifecycle_lock:", source)
        self.assertIn("self.provisioner.probe_only()", source)
        self.assertIn("self.provisioner.ensure_running(wait_seconds=15.0)", source)
        self.assertIn("with self._lock:", source)

    def test_ensemble_concurrency_honors_parallel_worker_setting(self):
        source = (ROOT / "core" / "intelligence_orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn("_ENSEMBLE_EXECUTOR", source)
        self.assertIn("int(c.get(\"parallel_workers\",4))", source)
        self.assertIn('ThreadPoolExecutor(', source)
        self.assertIn('thread_name_prefix="BrahmaEnsemble"', source)
        self.assertIn('thread_name_prefix="BrahmaEnsembleCritic"', source)

    def test_auto_heal_backup_names_are_collision_resistant(self):
        source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
        self.assertIn("time.time_ns()", source)
        self.assertIn("uuid.uuid4().hex", source)
        self.assertIn("backup_name = f\"{file_path.stem}.bak_{stamp}{file_path.suffix}\"", source)

    def test_self_coding_subprocesses_follow_hidden_console_policy(self):
        source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
        self.assertIn("def _hidden_creationflags", source)
        self.assertIn("creationflags=self._hidden_creationflags()", source)
        self.assertIn("stdin=subprocess.DEVNULL", source)

    def test_omniroute_is_local_and_lazy(self):
        from core.omniroute import OmniRouteGateway

        gateway = OmniRouteGateway()
        self.assertEqual(gateway.base_url, "http://127.0.0.1:20128/v1")
        self.assertFalse(gateway._ready)

    def test_unified_cloud_paths_have_no_direct_gemini_bypass(self):
        import re

        files = (
            ROOT / "llm_client.py",
            ROOT / "core" / "skill_forge.py",
            ROOT / "actions" / "brahma_dev_agent.py",
        )
        for path in files:
            source = path.read_text(encoding="utf-8")
            self.assertNotRegex(
                source,
                re.compile(r"genai\.Client\(|client\.models\.generate_content\("),
                msg=f"Direct Gemini generation bypass remains in {path}",
            )

        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("unified_cloud_client.chat_with_tools", main)
        self.assertIn("unified_cloud_client.chat_json", main)
        # Native Gemini Live remains a deliberate specialized transport.
        self.assertEqual(main.count("genai.Client("), 1)
        live_pos = main.find("client = genai.Client(")
        self.assertGreater(live_pos, main.find("class BrahmaLive"))

        llm = (ROOT / "llm_client.py").read_text(encoding="utf-8")
        self.assertIn("openrouter_client.chat", llm)
        self.assertIn("openrouter_client.chat_with_tools", llm)
        self.assertIn("openrouter_client.chat_json", llm)
        self.assertIn("openrouter_client.vision", llm)

    def test_call_assistant_text_paths_use_unified_gateway(self):
        source = (ROOT / "actions" / "call_assistant.py").read_text(encoding="utf-8")
        self.assertIn("unified_cloud_client.chat(", source)
        self.assertIn("unified_cloud_client.chat_json(", source)
        self.assertNotIn("genai.Client(", source.split("def _generate_ai_response", 1)[1].split("def _transcribe_audio", 1)[0] if "def _transcribe_audio" in source else source)
        self.assertNotIn("client.models.generate_content(", source.split("def _generate_ai_response", 1)[1].split("def _transcribe_audio", 1)[0] if "def _transcribe_audio" in source else source)
    def test_skill_and_coding_paths_fail_closed_without_gateway_bypass(self):
        skill = (ROOT / "core" / "skill_forge.py").read_text(encoding="utf-8")
        dev = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
        self.assertIn("LLM synthesis unavailable after OmniRoute routing.", skill)
        self.assertIn("Repair unavailable after OmniRoute routing.", skill)
        self.assertIn("Please check OmniRoute provider connectivity.", dev)
        self.assertNotIn("Direct Gemini", dev)
        self.assertNotIn("Direct Gemini", skill)

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

    def test_skill_forge_uses_local_llm_for_offline_synthesis_and_repair(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "skill_forge.py").read_text(encoding="utf-8")
        self.assertIn("from core.local_brain import local_brain", source)
        self.assertIn("Offline local LLM synthesis is unavailable.", source)
        self.assertIn("Offline local LLM repair is unavailable.", source)
    def test_skill_forge_skips_github_research_in_offline_mode(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "skill_forge.py").read_text(encoding="utf-8")
        self.assertIn('offline_mode = bool(config_manager.get_setting("offline_mode_enabled", False))', source)
        self.assertIn("if not offline_mode:", source)
        self.assertIn('Offline Mode enabled; skipping GitHub research.', source)
    def test_skill_forge_uses_github_first_research_before_synthesis(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "skill_forge.py").read_text(encoding="utf-8")
        self.assertIn("from core.github_research import GitHubResearchClient", source)
        self.assertIn("researcher.research_goal(goal, repo_limit=6, code_limit=10)", source)
        self.assertIn("researcher.format_dossier(research, max_chars=9000)", source)
        self.assertIn("_call_llm_synthesizer(goal, name_hint, combined_context)", source)
    def test_self_coding_model_ladder_uses_canonical_omniroute_client(self):
        source = Path(ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
        self.assertIn("from or_client import client as cloud_client", source)
        self.assertIn("cloud_client.multi_turn(", source)
        self.assertIn('model="auto/coding"', source)
        self.assertIn("max_tokens=8192", source)
        self.assertIn("Please check OmniRoute provider connectivity.", source)
        self.assertNotIn("genai.Client(", source)
        self.assertNotIn('get_api_key("Gemini")', source)

    def test_github_tools_are_exposed_to_brahma_dev(self):
        source = Path(ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
        self.assertIn("GitHubSearch", source)
        self.assertIn("GitHubRead", source)
        self.assertIn("GitHubRepo", source)
        self.assertIn("_github_research_preflight", source)
        self.assertIn("self.github_required_sources = min(3", source)


    def test_centralized_efficiency_policy_is_wired_into_coding_and_repair(self):
        policy = (ROOT / "core" / "efficiency_policy.py").read_text(encoding="utf-8")
        self_coding = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
        dev = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
        heal = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")

        self.assertIn("EFFICIENCY_DIRECTIVE", policy)
        for source in (self_coding, dev, heal):
            self.assertIn("core.efficiency_policy", source)
            self.assertIn("EFFICIENCY-FIRST ENGINEERING POLICY", source)

        self.assertIn("exact content/configuration hashes", policy)
        self.assertIn("Do not repeat the same scan", policy)
        self.assertIn("Never cache API keys", policy)
        self.assertIn("Never trade away correctness", policy)


    def test_omniroute_provider_operations_are_lifecycle_safe(self):
        source = Path(ROOT / "core" / "omniroute.py").read_text(encoding="utf-8")
        self.assertIn("with self._lifecycle_lock:", source)
        self.assertIn("current_base_url = self.provisioner.base_url.rstrip", source)
        self.assertIn("if not self.ensure_ready():", source)
        self.assertIn('"message": "OmniRoute is not ready"', source)

    def test_openrouter_failed_model_backoff_contract(self):
        source = Path(ROOT / "or_client.py").read_text(encoding="utf-8")
        self.assertIn("FAILED_MODEL_COOLDOWN = 30", source)
        self.assertIn("_failed_until: dict[str, float] = {}", source)
        self.assertIn("def _is_temporarily_failed", source)
        self.assertIn("def _mark_temporarily_failed", source)
        self.assertIn("if self._is_rate_limited(model) or self._is_temporarily_failed(model):", source)
        self.assertIn("if self._is_rate_limited(candidate) or self._is_temporarily_failed(candidate):", source)

    def test_openrouter_forbidden_model_skips_instead_of_aborting(self):
        from unittest.mock import patch
        import or_client

        client = or_client.OpenRouterClient()
        client.api_key = "test-key"
        with patch.object(client, "_refresh_credentials"), patch.object(
            or_client.requests, "post", return_value=type("Resp", (), {"status_code": 403})()
        ):
            result = client._call(
                "example/forbidden",
                [{"role": "user", "content": "ping"}],
                max_tokens=32,
                temperature=0.1,
            )
        self.assertIsNone(result)
        self.assertTrue(client._is_temporarily_failed("example/forbidden"))

    def test_structured_ensemble_has_no_stale_global_executor(self):
        source = Path(ROOT / "core" / "intelligence_orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn("_ENSEMBLE_EXECUTOR", source)
        self.assertIn("BrahmaStructuredEnsemble", source)
        self.assertIn('int(c.get("parallel_workers",4))', source)


    def test_device_and_smart_home_routing_is_off_ui_thread(self):
        source = Path(ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("target=_run_brahma_connect", source)
        self.assertIn('name="brahma-connect-routing"', source)
        self.assertIn("target=_run_smart_home", source)
        self.assertIn('name="smart-home-routing"', source)
        self.assertIn('normalized_route = re.sub(r"\\s+", " "', source)


    def test_omniroute_probe_rejects_http_error_false_positives(self):
        from core.omniroute_setup import OmniRouteProvisioner
        import urllib.error

        provisioner = OmniRouteProvisioner("http://127.0.0.1:20128/v1")
        for status in (401, 403, 405):
            def raise_http_error(status=status):
                raise urllib.error.HTTPError(
                    provisioner.base_url + "/models", status, "not OmniRoute", None, None
                )
            with patch("core.omniroute_setup.urllib.request.urlopen", side_effect=raise_http_error):
                self.assertFalse(provisioner.probe_only())

    def test_android_websocket_callbacks_ignore_stale_sockets(self):
        source = (ROOT / "brahma-connect-android" / "app" / "src" / "main" / "java" / "com" / "brahma" / "connect" / "network" / "BrahmaWebSocketClient.kt").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("if (socket !== webSocket)"), 3)
        self.assertIn('webSocket.close(1000, "Superseded connection")', source)

    def test_self_coding_rollback_never_git_cleans_untracked_files(self):
        source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
        rollback = source.split("def _rollback(", 1)[1].split("def preview(", 1)[0]
        self.assertNotIn('"clean", "-fd"', rollback)
        self.assertIn("preserved untracked or working-tree changes", rollback)


if __name__ == "__main__":
    unittest.main()
