from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RuntimeConsistencyTests(unittest.TestCase):
    def read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_runtime_paths_are_shared(self):
        from core.runtime_paths import (
            API_CONFIG_PATH,
            APP_SETTINGS_PATH,
            IDENTITY_PATH,
            FATAL_CRASH_LOG_PATH,
        )
        self.assertEqual(API_CONFIG_PATH.name, "api_keys.json")
        self.assertEqual(APP_SETTINGS_PATH.name, "app_settings.json")
        self.assertEqual(IDENTITY_PATH.name, "identity.json")
        self.assertEqual(FATAL_CRASH_LOG_PATH.name, "FATAL_CRASH.log")

        main = self.read("main.py")
        boot = self.read("core/boot_sentry.py")
        identity = self.read("core/identity.py")
        self.assertIn("STARTUP_LOG_PATH", main)
        self.assertIn("FATAL_CRASH_LOG_PATH", main)
        self.assertIn("FATAL_CRASH_LOG_PATH", boot)
        self.assertIn("IDENTITY_PATH", identity)

    def test_runtime_api_config_does_not_require_repo_secret_file(self):
        import config
        from core.runtime_paths import API_CONFIG_PATH

        cfg = config.get_config()
        self.assertIsInstance(cfg, dict)
        self.assertTrue(config.get_os())
        self.assertIn("API_CONFIG_PATH", self.read("config/__init__.py"))
        self.assertNotIn('Path(__file__).parent / "api_keys.json"', self.read("config/__init__.py"))
        self.assertEqual(API_CONFIG_PATH.name, "api_keys.json")
        self.assertEqual(API_CONFIG_PATH.parent.name, "config")

    def test_workspace_delete_cascades_messages(self):
        from workspace_store import WorkspaceStore
        import sqlite3
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            store = WorkspaceStore(Path(td) / "workspace.sqlite3")
            convo = store.create_conversation("Consistency Test")
            store.append_message(convo, "user", "hello")
            self.assertTrue(store.conversation_has_messages(convo))
            store.delete_conversation(convo)
            self.assertFalse(store.conversation_exists(convo))

            conn = sqlite3.connect(Path(td) / "workspace.sqlite3")
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                    (convo,),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(remaining, 0)

    def test_memory_persistence_uses_atomic_json_writer(self):
        source = (self.root / "memory" / "memory_manager.py").read_text(encoding="utf-8")
        self.assertIn("def _atomic_write_json(path: Path, value: object)", source)
        self.assertIn("_atomic_write_json(MEMORY_PATH, memory)", source)
        self.assertIn("_atomic_write_json(CHAT_HISTORY_PATH, history[-MAX_HISTORY_LENGTH:])", source)
    def test_identity_persistence_is_atomic(self):
        source = self.read("core/identity.py")
        self.assertIn("self._lock = threading.RLock()", source)
        self.assertIn("temp = self.config_file.with_suffix(\".json.tmp\")", source)
        self.assertIn("temp.replace(self.config_file)", source)

    def test_provider_policy_is_canonical(self):
        from core.provider_policy import (
            GEMINI,
            OPENROUTER,
            LOCAL,
            display_name,
            is_gemini,
            is_local,
            is_openrouter,
            normalize_provider,
        )
        self.assertEqual(normalize_provider("google gemini"), GEMINI)
        self.assertEqual(normalize_provider("Gemini"), GEMINI)
        self.assertEqual(normalize_provider("OPENROUTER"), OPENROUTER)
        self.assertEqual(normalize_provider("local ai"), LOCAL)
        self.assertTrue(is_gemini("Google-Gemini"))
        self.assertTrue(is_openrouter("open router"))
        self.assertTrue(is_local("OLLAMA"))
        self.assertEqual(display_name("gemini"), "Google Gemini")

    def test_local_provider_matching_is_case_insensitive(self):
        source = self.read("llm_client.py")
        self.assertIn("def _is_local_provider(self)", source)
        self.assertNotIn('if self._provider == "Local":', source)

    def test_openrouter_refreshes_credentials_for_runtime_changes(self):
        source = self.read("or_client.py")
        self.assertIn("def _refresh_credentials", source)
        self.assertIn("self._refresh_credentials()", source)

    def test_omniroute_port_and_url_share_one_source(self):
        source = self.read("core/runtime_paths.py")
        setup = self.read("core/omniroute_setup.py")
        self.assertIn("OMNIROUTE_DEFAULT_PORT = 20128", source)
        self.assertIn("OMNIROUTE_DEFAULT_BASE_URL = f", source)
        self.assertIn("DEFAULT_PORT = OMNIROUTE_DEFAULT_PORT", setup)
        self.assertIn("base_url: str = OMNIROUTE_DEFAULT_BASE_URL", setup)
        self.assertNotIn("DEFAULT_PORT = 20128", setup)

    def test_bootstrap_targets_repository_python_runtime(self):
        source = self.read("bootstrap.ps1")
        contract = self.read("core/runtime_contract.py")
        self.assertIn('PYTHON_MAJOR_MINOR = "3.12"', contract)
        self.assertIn('PYTHON_BOOTSTRAP_VERSION = "3.12.10"', contract)
        self.assertIn('NODE_VERSION = "24.21.0"', contract)
        self.assertIn('$PythonMajorMinor = Get-RuntimeContractValue "PYTHON_MAJOR_MINOR"', source)
        self.assertIn('$PythonBootstrapVersion = Get-RuntimeContractValue "PYTHON_BOOTSTRAP_VERSION"', source)
        self.assertIn('$NodeVersion = Get-RuntimeContractValue "NODE_VERSION"', source)
        self.assertNotIn("py -3.12", source)
        self.assertNotIn("python-3.12.10-amd64.exe", source)

    def test_brahma_connect_service_has_synchronized_lifecycle(self):
        source = self.read("brahma_connect/service.py")
        self.assertIn("_lock: threading.RLock", source)
        self.assertIn("with self._lock:", source)
        self.assertIn("self._thread = None", source)
        self.assertIn("requested = Path(base_dir).expanduser().resolve()", source)
        self.assertIn("elif _SERVICE.base_dir != requested:", source)

    def test_local_discovery_uses_configured_endpoint(self):
        source = self.read("core/local_brain.py")
        self.assertIn('f"{self.endpoint}/models"', source)
        self.assertIn("DEFAULT_ENDPOINT.rstrip", source)
        self.assertIn('"data"', source)

    def test_local_model_download_updates_stay_on_qt_thread(self):
        source = self.read("ui.py")
        self.assertIn("local_model_pull_update = pyqtSignal(object)", source)
        self.assertIn("self.local_model_pull_update.emit(chunk)", source)
        self.assertNotIn('pull_model_async("qwen2.5:3b", lambda chunk: _populate_models())', source)

    def test_local_model_default_is_shared_with_unified_client(self):
        brain = self.read("core/local_brain.py")
        client = self.read("llm_client.py")
        self.assertIn('DEFAULT_MODEL = "qwen2.5:3b"', brain)
        self.assertIn("from core.local_brain import DEFAULT_ENDPOINT as LOCAL_DEFAULT_ENDPOINT, DEFAULT_MODEL as LOCAL_DEFAULT_MODEL", client)
        self.assertNotIn('"llama3.2"', client)
        self.assertIn("or LOCAL_DEFAULT_MODEL", client)

    def test_omniroute_readiness_is_cached(self):
        from core.omniroute import OmniRouteGateway

        gateway = OmniRouteGateway()
        calls = {"probe": 0, "sync": 0}

        def probe():
            calls["probe"] += 1
            return True

        def sync(_):
            calls["sync"] += 1
            return {"configured": [], "skipped": []}

        gateway.provisioner.probe_only = probe
        gateway.provisioner.sync_existing_provider_keys = sync

        self.assertTrue(gateway.ensure_ready(force=True))
        self.assertTrue(gateway.ensure_ready())
        self.assertEqual(calls["probe"], 1)
        self.assertEqual(calls["sync"], 1)

    def test_startup_readiness_is_provider_aware(self):
        source = self.read("ui.py")
        self.assertIn("def _check_config(self) -> bool:", source)
        self.assertIn('if provider == "OpenRouter":', source)
        self.assertIn("if offline or is_local(provider):", source)
        self.assertIn("return bool(api.get(\"gemini_api_key\"))", source)

    def test_universal_fallback_is_reachable_from_main_reply_ladder(self):
        main = self.read("main.py")
        forge = self.read("core/skill_forge.py")
        runtime = self.read("core/runtime_paths.py")
        self.assertIn("from core.universal_agent import run as run_universal_task", main)
        self.assertIn("run_universal_task(", main)
        self.assertIn("if not reply and _looks_like_action_request(text):", main)
        self.assertIn("from core.runtime_paths import API_CONFIG_PATH", forge)
        self.assertNotIn("from core.runtime_paths import r,", forge)
        self.assertNotIn(" r,", forge.split("from core.runtime_paths import", 1)[-1].split("\n", 1)[0])
        self.assertNotIn("r =", runtime)
    def test_live_voice_degrades_to_text_tts_without_gemini_credential(self):
        source = self.read("main.py")
        self.assertIn("def _has_gemini_voice_credentials()", source)
        self.assertIn("async def run_text_voice_fallback(self):", source)
        self.assertIn("asyncio.run(brahma_evo.run_text_voice_fallback())", source)
        self.assertIn('text_voice_fallback = bool(getattr(self, "_text_voice_fallback", False))', source)
        self.assertNotIn("Continuous Live voice is unavailable without a Gemini voice credential; text/control features remain available.", source)

    def test_provider_auto_switch_setting_is_consumed(self):
        source = self.read("main.py")
        self.assertIn('auto_provider_switch = bool(app_settings.get("auto_provider_switch", True))', source)
        self.assertIn("if not reply and not is_offline_mode and auto_provider_switch:", source)
        self.assertIn('primary_provider = "OpenRouter" if (', source)

    def test_live_audio_queues_are_bounded(self):
        source = self.read("main.py")
        self.assertIn("asyncio.Queue(maxsize=48)", source)
        self.assertIn("self._enqueue_live_input", source)
        self.assertIn("self._enqueue_playback", source)

    def test_ui_metrics_worker_has_shutdown_path(self):
        ui = self.read("ui.py")
        self.assertIn("self._stop_event = threading.Event()", ui)
        self.assertIn("def stop_background_metrics(self)", ui)
        self.assertIn("ui.stop_background_metrics()", self.read("main.py"))

    def test_shutdown_cleans_background_services(self):
        source = self.read("main.py")
        self.assertIn("def stop_background_services(self)", source)
        self.assertIn("self._idle_stop_event.set()", source)
        self.assertIn("brahma_connect.stop()", source)
        self.assertIn("dashboard.stop()", source)
        self.assertIn("aboutToQuit.connect(_cleanup_runtime_services)", source)

    def test_sensorium_shutdown_is_interruptible(self):
        source = self.read("core/sensorium.py")
        self.assertIn("self._stop_event = threading.Event()", source)
        self.assertIn("self._stop_event.wait", source)
        self.assertNotIn("time.sleep(sleep_for)", source)

    def test_omniroute_setup_imports_central_constants(self):
        setup = self.read("core/omniroute_setup.py")
        runtime = self.read("core/runtime_paths.py")
        contract = self.read("core/runtime_contract.py")
        self.assertIn("from core.runtime_contract import OMNIROUTE_VERSION", setup)
        self.assertIn("from core.runtime_paths import API_CONFIG_PATH, OMNIROUTE_DEFAULT_BASE_URL, OMNIROUTE_DEFAULT_PORT", setup)
        self.assertIn("OMNIROUTE_DEFAULT_PORT = 20128", runtime)
        self.assertIn('OMNIROUTE_VERSION = "3.8.50"', contract)
        self.assertIn('NODE_VERSION = "24.21.0"', contract)

    def test_boot_sentry_runs_after_singleton_import_boundary(self):
        main = self.read("main.py")
        self.assertNotIn("import core.boot_sentry", main)
        self.assertIn("from core.boot_sentry import check_and_recover_on_boot", main)
        guard_pos = main.index("if not guard.acquire()")
        boot_pos = main.index("from core.boot_sentry import check_and_recover_on_boot")
        main_fn_pos = main.index("def main():")
        self.assertGreater(boot_pos, main_fn_pos)
        self.assertGreater(boot_pos, guard_pos)

    def test_boot_sentry_uses_patch_age_guard(self):
        source = self.read("core/boot_sentry.py")
        self.assertIn('float(entry.get("timestamp"))', source)
        self.assertIn("age <= 300.0", source)

    def test_unsafe_core_updater_reset_is_gone(self):
        source = self.read("core/updater.py")
        self.assertNotIn('["git", "reset", "--hard"', source)
        self.assertIn("from updater import restart_application, update_from_github", source)

    def test_ui_omniroute_uses_central_page_and_gateway_endpoint(self):
        source = self.read("ui.py")
        self.assertIn('"omniroute": 5', source)
        self.assertIn("gateway().base_url", source)
        self.assertIn("gateway().ensure_ready(force=True)", source)

    def test_discord_explicit_off_state_is_not_overridden_by_token(self):
        source = self.read("ui.py")
        self.assertIn('if "enabled" not in data and str(data.get("bot_token") or "").strip():', source)
        self.assertIn('enabled = bool(settings.get("enabled", False))', source)
        self.assertNotIn('enabled = bool(settings.get("enabled", False) or (settings.get("bot_token") or "").strip())', source)

    def test_mobile_command_queue_is_bounded(self):
        source = self.read("dashboard/server.py")
        self.assertIn("asyncio.Queue(maxsize=64)", source)
        self.assertIn("def _enqueue_command(self, text: str) -> bool:", source)
        self.assertIn("Command queue is busy; retry shortly.", source)

    def test_screen_vision_session_is_bounded_and_stoppable(self):
        source = self.read("actions/screen_processor.py")
        self.assertIn("asyncio.Queue(maxsize=30)", source)
        self.assertIn("asyncio.Queue(maxsize=48)", source)
        self.assertIn("def stop(self)", source)
        self.assertIn("while not self._stop_event.is_set()", source)
        self.assertIn("def stop_screen_processor()", source)
        self.assertIn("stop_screen_processor()", self.read("main.py"))

    def test_email_daemon_has_interruptible_shutdown(self):
        source = self.read("actions/google_workspace_mcp.py")
        self.assertIn("_email_daemon_stop_event = threading.Event()", source)
        self.assertIn("_email_daemon_stop_event.wait", source)
        self.assertIn("_email_daemon_stop_event.set()", source)

    def test_instagram_daemon_has_interruptible_shutdown(self):
        source = self.read("actions/instagram_mcp.py")
        self.assertIn("self._stop_event = threading.Event()", source)
        self.assertIn("self._stop_event.wait(timeout=POLL_INTERVAL)", source)
        self.assertIn("self._stop_event.set()", source)

    def test_hidden_device_polling_stops_when_hidden(self):
        source = self.read("ui.py")
        self.assertIn("self._poll_tmr.start(2500)", source)
        self.assertIn("self._poll_tmr.stop()", source)
        self.assertIn("self._reconnect_timer.start()", source)
        self.assertIn("self._reconnect_timer.stop()", source)

    def test_runtime_api_config_is_lock_protected_and_atomic(self):
        source = self.read("config/__init__.py")
        self.assertIn("threading.RLock()", source)
        self.assertIn("with _CONFIG_LOCK:", source)
        self.assertIn('temp.replace(API_CONFIG_PATH)', source)

    def test_settings_hub_uses_canonical_page_navigation(self):
        source = self.read("ui.py")
        self.assertIn('SettingsHubPage(lambda page: self._set_page(page))', source)
        self.assertIn('"home")', source)
        self.assertIn('"devices")', source)
        self.assertIn('"settings")', source)
        self.assertIn('"omniroute")', source)
        self.assertNotIn("target_idx", source)

    def test_provider_key_changes_invalidate_omniroute_sync_cache(self):
        gateway_source = self.read("core/omniroute.py")
        ui_source = self.read("ui.py")
        self.assertIn("def mark_credentials_stale(self)", gateway_source)
        self.assertIn("gateway().mark_credentials_stale()", ui_source)

    def test_omniroute_provider_cache_mutations_use_gateway_lock(self):
        source = self.read("core/omniroute.py")
        start = source.index("def configure_provider")
        end = source.index("def test_provider", start)
        block = source[start:end]
        self.assertIn("with self._lock:", block)
        self.assertIn("def sync_credentials", block)
        self.assertIn("with self._lock:", block)

    def test_openrouter_401_is_not_swallowed_as_generic_error(self):
        source = self.read("or_client.py")
        self.assertIn("except PermissionError:", source)
        self.assertIn("            except PermissionError:\n                raise", source)

    def test_core_credential_lookup_reuses_canonical_provider_policy(self):
        source = self.read("config/__init__.py")
        self.assertIn("from core.provider_policy import", source)
        self.assertIn("normalize_provider", source)
        self.assertIn("storage_names = {", source)
        self.assertNotIn('"google gemini": "gemini"', source)
        self.assertNotIn('"open router": "openrouter"', source)

    def test_gemini_credentials_are_read_through_shared_config(self):
        source = self.read("main.py")
        self.assertIn("from config import get_api_key", source)
        self.assertIn('return get_api_key("Gemini")', source)
        self.assertNotIn('json.load(f)["gemini_api_key"]', source)

    def test_openrouter_credentials_use_shared_config(self):
        source = self.read("or_client.py")
        self.assertIn("from config import get_api_key", source)
        self.assertIn('return get_api_key("OpenRouter")', source)

    def test_live_config_does_not_send_removed_affective_dialog_flag(self):
        source = self.read("main.py")
        self.assertNotIn("enable_affective_dialog", source)

    def test_omniroute_uses_canonical_runtime_endpoint(self):
        source = self.read("core/omniroute.py")
        self.assertIn("OMNIROUTE_DEFAULT_BASE_URL", source)
        self.assertNotIn('"http://127.0.0.1:20128/v1"', source)

    def test_omniroute_runtime_version_pin_is_verified_release(self):
        setup_source = self.read("core/omniroute_setup.py")
        prepare_source = self.read("scripts/prepare_omniroute_runtime.py")
        contract_source = self.read("core/runtime_contract.py")
        self.assertIn('OMNIROUTE_VERSION = "3.8.50"', contract_source)
        self.assertIn('OMNIROUTE_COMMIT = "5458026c216f77a3da68ea49152dc33470cfe2cb"', contract_source)
        self.assertIn("from core.runtime_contract import OMNIROUTE_VERSION", setup_source)
        self.assertIn("from core.runtime_contract import NODE_VERSION, OMNIROUTE_COMMIT, OMNIROUTE_VERSION", prepare_source)

    def test_ui_does_not_duplicate_omniroute_default_endpoint(self):
        source = self.read("ui.py")
        self.assertNotIn('"http://127.0.0.1:20128/"', source)
        self.assertIn("OMNIROUTE_DEFAULT_BASE_URL", source)

    def test_llm_settings_use_canonical_store(self):
        source = self.read("llm_client.py")
        self.assertIn("from memory.config_manager import load_settings", source)
        self.assertNotIn('open(SETTINGS_PATH, "r"', source)

    def test_dashboard_credentials_use_canonical_config_accessor(self):
        source = self.read("dashboard/server.py")
        self.assertIn('get_api_key("Gemini")', source)
        self.assertNotIn('api_keys.json', source)

    def test_main_ui_api_reads_use_canonical_config_accessor(self):
        source = self.read("ui.py")
        self.assertIn("from config import get_config", source)
        self.assertNotIn("API_FILE.read_text(encoding=\"utf-8\")", source)

    def test_provider_normalization_has_one_canonical_alias_table(self):
        source = self.read("core/provider_policy.py")
        self.assertIn("ALIASES: Final[dict[str, str]] = {", source)
        config_source = self.read("config/__init__.py")
        self.assertNotIn('"google gemini": "gemini"', config_source)
        self.assertIn("normalize_provider", config_source)

    def test_omniroute_packaging_reuses_runtime_version_pin(self):
        source = self.read("scripts/prepare_omniroute_runtime.py")
        self.assertIn("from core.runtime_contract import NODE_VERSION, OMNIROUTE_COMMIT, OMNIROUTE_VERSION", source)
        self.assertNotIn("\nOMNIROUTE_VERSION = ", source)
        self.assertNotIn("\nOMNIROUTE_COMMIT = ", source)

    def test_omniroute_dashboard_retries_gateway_startup(self):
        source = self.read("ui.py")
        self.assertIn("def _retry_gateway_and_reload(self)", source)
        self.assertIn("gateway().ensure_ready()", source)
        self.assertIn("self._gateway_retry_inflight", source)

    def test_navigation_back_to_settings_uses_central_controller(self):
        source = self.read("ui.py")
        self.assertIn('win._set_page("settings")', source)
        self.assertIn('win._set_page("omniroute")', source)

    def test_instagram_browser_login_uses_shared_config_writer(self):
        source = self.read("actions/instagram_mcp.py")
        self.assertIn("from config import save_config", source)
        self.assertNotIn('open(CONFIG_PATH, "w"', source)

    def test_shortcut_creation_uses_hidden_powershell(self):
        source = self.read("main.py")
        self.assertIn('creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)', source)

    def test_offline_mode_restores_previous_cloud_provider(self):
        source = self.read("ui.py")
        self.assertIn('"last_cloud_provider"', source)
        self.assertIn('restore_provider = normalize_provider', source)

    def test_identity_persistence_is_atomic_and_thread_safe(self):
        source = self.read("core/identity.py")
        self.assertIn("self._lock = threading.RLock()", source)
        self.assertIn("def _set_value(", source)
        self.assertIn("temp.replace(self.config_file)", source)
        self.assertIn("raise", source)
        self.assertIn("with self._lock:", source)

    def test_identity_state_writes_use_shared_mutation_helper(self):
        source = self.read("core/identity.py")
        for method in (
            "set_assistant_name",
            "set_application_name",
            "set_assistant_title",
            "set_owner_role",
            "set_owner_location",
            "set_owner_interests",
            "set_owner_about",
            "set_behavior_mode",
            "set_custom_instructions",
            "set_proactive",
            "set_shared_computer",
        ):
            start = source.index(f"def {method}")
            next_def = source.find("\n    def ", start + 5)
            block = source[start: next_def if next_def >= 0 else len(source)]
            self.assertIn("_set_value(", block, method)

    def test_omniroute_gateway_has_one_canonical_owner(self):
        setup = self.read("core/omniroute_setup.py")
        gateway = self.read("core/omniroute.py")
        self.assertNotIn("class OmniRouteGateway", setup)
        self.assertIn("class OmniRouteGateway", gateway)
        self.assertIn("_gateway = OmniRouteGateway()", gateway)

    def test_omniroute_has_application_owned_shutdown_cleanup(self):
        setup = self.read("core/omniroute_setup.py")
        gateway = self.read("core/omniroute.py")
        self.assertIn("def stop(self)", setup)
        self.assertIn("def stop(self)", gateway)
        self.assertIn("self.provisioner.stop()", gateway)
        self.assertIn("atexit.register(_gateway.stop)", gateway)

    def test_launcher_does_not_bypass_canonical_runtime(self):
        source = self.read("start_brahma.vbs")
        self.assertIn("bootstrap = root &", source)
        self.assertIn("ElseIf fso.FileExists(bootstrap) Then", source)
        self.assertNotIn('shell.Run "python.exe ', source)
        launch_block = source.split("If fso.FileExists(venvPython)", 1)[1].split("ElseIf", 1)[0]
        self.assertIn("venvPython", launch_block)

    def test_documentation_matches_supported_python_runtime(self):
        source = self.read("README.md")
        self.assertIn("Python-3.12-blue", source)
        self.assertIn("- **Python 3.12**", source)
        self.assertNotIn("Python 3.11", source)

    def test_requirements_do_not_duplicate_package_names(self):
        names = []
        for line in self.read("requirements.txt").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip().lower())
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
