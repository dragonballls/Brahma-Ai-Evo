from __future__ import annotations

import ast
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RuntimeConsistencyTests(unittest.TestCase):
    def read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_settings_load_uses_file_signature_cache(self):
        source = self.read("memory/config_manager.py")
        self.assertIn("_SETTINGS_CACHE:", source)
        self.assertIn("def _settings_signature()", source)
        self.assertIn("_SETTINGS_CACHE[0] == signature", source)
        self.assertIn("_SETTINGS_CACHE = (_settings_signature(), dict(current))", source)

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
        source = (ROOT / "memory" / "memory_manager.py").read_text(encoding="utf-8")
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

    def test_attention_monitor_window_dedupe_is_only_active_state(self):
        source = self.read("actions/attention_monitor.py")
        self.assertIn("self._active_window_keys: set[str] = set()", source)
        self.assertIn("current_window_keys: set[str] = set()", source)
        self.assertIn("current_window_keys.add(dedupe)", source)
        self.assertIn("if dedupe in self._active_window_keys:", source)
        self.assertIn(
            "self._active_window_keys.intersection_update(current_window_keys)",
            source,
        )
        self.assertNotIn(
            "self._remember_seen(dedupe)",
            source[source.index("def _poll_windows"):],
        )

        self.assertIn("scan_succeeded = False", source)
        self.assertIn("scan_succeeded = True", source)
        self.assertIn("if scan_succeeded:", source)

    def test_attention_monitor_lifecycle_and_speech_sink_are_single_owner(self):
        source = self.read("actions/attention_monitor.py")
        tree = ast.parse(source, filename="actions/attention_monitor.py")

        speech_sink_defs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "set_speech_sink"
        ]
        self.assertEqual(len(speech_sink_defs), 1)

        self.assertIn("self._thread = threading.Thread(", source)
        self.assertIn("thread.is_alive()", source)
        self.assertIn("self._thread = None", source)
        self.assertIn("finally:", source)
        self.assertIn("self._running = False", source)
        self.assertIn("_current_speech_proc: subprocess.Popen | None", source)
        self.assertNotIn("Optional[subprocess.Popen]", source)

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

    def test_local_brain_reloads_persisted_endpoint_and_model(self):
        source = (ROOT / "core" / "local_brain.py").read_text(encoding="utf-8")
        self.assertIn("def reload_settings(self) -> None:", source)
        self.assertIn('settings.get("local_ai_url")', source)
        self.assertIn('settings.get("local_ai_model")', source)
        self.assertIn("self.reload_settings()", source)
    def test_local_model_default_is_shared_with_unified_client(self):
        brain = self.read("core/local_brain.py")
        client = self.read("llm_client.py")
        self.assertIn('DEFAULT_MODEL = "qwen2.5:3b"', brain)
        self.assertIn("from core.local_brain import DEFAULT_ENDPOINT as LOCAL_DEFAULT_ENDPOINT, DEFAULT_MODEL as LOCAL_DEFAULT_MODEL", client)
        self.assertNotIn('"llama3.2"', client)
        self.assertIn("or LOCAL_DEFAULT_MODEL", client)

    def test_omniroute_control_commands_are_nonblocking(self):
        main = self.read("main.py")
        status = main.split('if lower_exact in {"omniroute status"', 1)[1].split(
            'if lower_exact in {"sync omniroute keys"', 1
        )[0]
        self.assertIn('name="omniroute-status"', status)
        self.assertIn("gateway().status()", status)
        self.assertNotIn("status = gateway().status()", status.split("def _omni_status", 1)[0])

        sync = main.split('if lower_exact in {"sync omniroute keys"', 1)[1].split(
            'm = re.fullmatch(r"(?:test )?omniroute provider', 1
        )[0]
        self.assertIn("gateway().sync_credentials()", sync)
        self.assertNotIn("gateway().provisioner.sync_existing_provider_keys", sync)

    def test_omniroute_windows_helpers_hide_subprocesses(self):
        setup = self.read("core/omniroute_setup.py")
        gateway = self.read("core/omniroute.py")
        self.assertIn("def _hidden_creationflags()", setup)
        self.assertGreaterEqual(setup.count("creationflags=_hidden_creationflags()"), 2)
        self.assertIn("creationflags=int(getattr(subprocess, \"CREATE_NO_WINDOW\", 0))", gateway)

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

    def test_evolution_engine_is_initialized_before_startup_use(self):
        source = self.read("main.py")
        impl = source[source.index("def _main_impl():"):]
        init_pos = impl.index("    evolution_engine = None")
        start_use_pos = impl.index("    if evolution_engine is not None and not BRAHMA_EVO_TEST_MODE:")
        self.assertLess(init_pos, start_use_pos)
        self.assertEqual(impl.count("    evolution_engine = None"), 1)

    def test_omniroute_uses_floating_window_without_user_port_ui(self):
        source = self.read("ui.py")
        self.assertIn("class OmniRouteFloatingWindow(QDialog):", source)
        self.assertIn("self.setMinimumSize(600, 420)", source)
        self.assertIn("self.resize(760, 540)", source)
        self.assertIn('max(600, int(settings.get("omniroute_window_width"', source)
        self.assertIn('max(420, int(settings.get("omniroute_window_height"', source)
        self.assertIn("Qt.WindowType.Window", source)
        self.assertIn("omniroute_window_width", source)
        self.assertNotIn("class OmniRouteEmbeddedPage(QWidget):", source)
        self.assertNotIn("Open OmniRoute Dashboard Inside Brahma", source)
        self.assertIn("Open OmniRoute", source)

    def test_omniroute_can_select_free_loopback_port_automatically(self):
        source = self.read("core/omniroute_setup.py")
        self.assertIn("def _select_loopback_port(self) -> None:", source)
        self.assertIn("probe.bind((\"127.0.0.1\", 0))", source)
        self.assertIn("self.base_url = urllib.parse.urlunparse", source)

    def test_boot_sentry_runs_after_singleton_import_boundary(self):
        main = self.read("main.py")
        self.assertNotIn("import core.boot_sentry", main)
        self.assertIn("from core.boot_sentry import check_and_recover_on_boot", main)
        boot_pos = main.index("from core.boot_sentry import check_and_recover_on_boot")
        gui_pos = main.index("from PyQt6.QtCore import")
        audio_pos = main.index("import sounddevice as sd")
        self.assertLess(boot_pos, gui_pos)
        self.assertLess(boot_pos, audio_pos)

    def test_conversation_tool_errors_schedule_automatic_self_heal(self):
        source = self.read("main.py")
        self.assertIn("_schedule_conversational_auto_heal(", source)
        self.assertIn("brahma-conversational-autoheal", source)
        self.assertIn('AutoHealEngine.heal_traceback(text, context_notes=context)', source)
        self.assertIn("Automatic conversational recovery.", source)

    def test_packaged_smoke_exit_uses_hard_ci_process_exit(self):
        source = self.read("main.py")
        self.assertIn("QCoreApplication.instance()", source)
        self.assertIn("app_instance.processEvents()", source)
        self.assertIn("time.monotonic() < deadline", source)
        self.assertIn("_cleanup_runtime_services()", source)
        self.assertIn("os._exit(0)", source)
        self.assertNotIn("ui.root.quit()", source)
        self.assertNotIn("ui.root.destroy()", source)
        self.assertNotIn("QTimer.singleShot(int(test_exit_seconds * 1000), _finish_packaged_smoke_test)", source)
    def test_windows_omniroute_cache_skips_rebuild_on_exact_hit(self):
        workflow = self.read(".github/workflows/windows-release.yml")
        self.assertIn("id: omni-cache", workflow)
        self.assertIn("steps.omni-cache.outputs.cache-hit", workflow)
        self.assertIn("if: ${{ steps.omni-cache.outputs.cache-hit != 'true' }}", workflow)
        self.assertIn("actions/cache/save@v6", workflow)
        self.assertIn("actions/cache/restore@v6", workflow)

    def test_windows_payload_is_reused_by_content_hash(self):
        workflow = self.read(".github/workflows/windows-release.yml")
        self.assertIn("id: payload-cache", workflow)
        self.assertIn("windows-payload-", workflow)
        self.assertIn("hashFiles('dist/BrahmaEvo/**')", workflow)
        self.assertIn("steps.payload-cache.outputs.cache-hit != 'true'", workflow)
        self.assertIn("compression-level: 0", workflow)

    def test_setup_payload_archive_is_the_release_fast_path(self):
        spec = self.read("installer/BrahmaEvo_Setup.spec")
        wizard = self.read("installer/install_wizard.py")
        packer = self.read("scripts/pack_windows_payload.py")
        workflow = self.read(".github/workflows/windows-release.yml")
        self.assertIn("BrahmaEvoPayload.zip", spec)
        self.assertIn("zipfile.ZipFile", wizard)
        self.assertNotIn("QWebEngineView", wizard)
        self.assertIn("Unsafe installer payload entry", wizard)
        self.assertIn("BrahmaEvoPayload.zip", workflow)
        self.assertIn("actions/cache/restore@v6", workflow)
        self.assertIn("required", packer)
        self.assertIn("--verify", packer)
        self.assertIn("compresslevel=1", packer)

    def test_supervisor_spec_is_compatible_with_pyinstaller_spec_execution(self):
        source = self.read("installer/BrahmaEvo_Supervisor.spec")
        tree = ast.parse(source)
        self.assertFalse(
            any(isinstance(node, ast.Name) and node.id == "__file__" for node in ast.walk(tree)),
            "supervisor spec must not access the unavailable runtime name",
        )
        self.assertIn("os.path.abspath(os.getcwd())", source)
        self.assertIn("BrahmaEvoSupervisor", source)

    def test_boot_sentry_uses_patch_age_guard(self):
        source = self.read("core/boot_sentry.py")
        self.assertIn('float(entry.get("timestamp"))', source)
        self.assertIn("age <= 300.0", source)

    def test_unsafe_core_updater_reset_is_gone(self):
        source = self.read("core/updater.py")
        self.assertNotIn('["git", "reset", "--hard"', source)
        self.assertIn("from updater import restart_application, update_from_github", source)

    def test_ui_omniroute_uses_floating_window_and_gateway_endpoint(self):
        source = self.read("ui.py")
        self.assertIn("class OmniRouteFloatingWindow(QDialog):", source)
        self.assertIn("gateway().base_url", source)
        self.assertIn("gateway().ensure_ready(force=True)", source)
        self.assertIn("omniroute_window_width", source)
        self.assertIn("omniroute_window_height", source)
        self.assertNotIn('"omniroute": 5', source)

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
        self.assertIn("class OmniRouteFloatingWindow(QDialog):", source)
        self.assertIn("def _retry_gateway(self)", source)
        self.assertIn("gateway().ensure_ready()", source)
        self.assertIn("self._gateway_retry_inflight", source)
        self.assertIn("QTimer", source)

    def test_navigation_to_omniroute_uses_floating_window_controller(self):
        source = self.read("ui.py")
        self.assertIn('self._omniroute_window = None', source)
        self.assertIn('dialog = OmniRouteFloatingWindow(owner=win)', source)
        self.assertIn('dialog.show()', source)
        self.assertNotIn('win._set_page("omniroute")', source)

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
        self.assertIn("ElseIf fso.FileExists(bootstrap) And fso.FileExists(mainPy) Then", source)
        self.assertNotIn('shell.Run "python.exe ', source)
        self.assertIn("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File", source)
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

    def test_hardening_dashboard_payloads_are_authenticated_and_ws_tokens_are_not_query_params(self):
        server = self.read("dashboard/server.py")
        app = self.read("dashboard/static/app.html")
        self.assertIn("hmac.compare_digest", server)
        self.assertIn("HMAC-SHA256", server)
        self.assertIn('subprotocol=f"brahma-auth.{tok}"', server)
        self.assertNotIn('async def download_file(filename: str, token: str = "")', server)
        self.assertNotIn("/ws?token=", app)
        self.assertNotIn("/ws/phone-audio?token=", app)
        self.assertIn("CryptoJS.HmacSHA256", app)
        self.assertIn("brahma-auth.", app)

    def test_ota_and_bootstrap_verify_download_integrity(self):
        ota = self.read("core/updater_ota.py")
        bootstrap = self.read("bootstrap.ps1")
        self.assertIn("SHA-256 digest", ota)
        self.assertIn("def _sha256", ota)
        self.assertIn("actual = _sha256(temp_path)", ota)
        self.assertIn("OTA installer SHA-256 verification failed", ota)
        self.assertIn("function Get-Sha256", bootstrap)
        self.assertIn("Get-NodeChecksum", bootstrap)
        self.assertIn("Test-Authenticode", bootstrap)
        self.assertIn("Node.js installer SHA256 verification failed", bootstrap)

    def test_installer_stages_and_validates_before_replacing_live_install(self):
        source = self.read("installer/install_wizard.py")
        block = source.split("    def run(self):", 1)[1].split("\nclass InstallWizard", 1)[0]
        self.assertIn("staging_dir = target.parent /", block)
        self.assertIn("missing_required", block)
        self.assertIn("target.replace(backup_dir)", block)
        self.assertIn("staging_dir.replace(target)", block)
        self.assertIn("Staged installation is missing BrahmaEvo.exe or BrahmaEvoSupervisor.exe.", block)
        self.assertNotIn("shutil.rmtree(self.target_dir", block)
        self.assertNotIn("os.remove(self.target_dir", block)

    def test_ensemble_and_balanced_profile_are_bounded(self):
        source = self.read("core/intelligence_orchestrator.py")
        config = self.read("config/intelligence.json")
        self.assertIn('"balanced"', source)
        self.assertIn('profiles = c.get("profiles", {})', source)
        self.assertIn("provider_cap = max(2", source)
        self.assertIn('"balanced"', config)
        self.assertIn('"ensemble_max_providers": 3', config)

    def test_action_planner_has_safe_universal_fallback(self):
        planner = self.read("agent/planner.py")
        executor = self.read("agent/executor.py")
        self.assertIn("universal_task", planner)
        self.assertIn('tool": "universal_task"', planner)
        self.assertIn('elif tool == "universal_task":', executor)
        self.assertIn("run_universal_task", executor)
        self.assertNotIn('tool["tool"] = "web_search"', planner)

    def test_task_queue_can_restart_after_stop(self):
        source = self.read("agent/task_queue.py")
        self.assertIn("thread.join(timeout=2.0)", source)
        self.assertIn("self._worker_thread = None", source)
        self.assertIn("def is_running(self) -> bool:", source)
        self.assertIn("with self._condition:", source)
        self.assertNotIn("_queue_started", source)

    def test_workspace_active_conversation_can_be_cleared(self):
        source = self.read("workspace_store.py")
        self.assertIn("DELETE FROM state WHERE key = 'active_conversation_id'", source)

    def test_omniroute_probe_requires_models_contract_and_retries_port_races(self):
        source = self.read("core/omniroute_setup.py")
        self.assertIn("payload = json.loads(response.read().decode", source)
        self.assertIn('isinstance(payload.get("data"), list)', source)
        self.assertIn("for attempt in range(3):", source)
        self.assertIn("self._select_loopback_port()", source)

    def test_openrouter_credentials_are_snapshotted_per_request(self):
        source = self.read("or_client.py")
        self.assertIn("def _request_headers", source)
        self.assertIn("headers = self._request_headers()", source)
        self.assertIn("def _refresh_credentials", source)
        self.assertIn("_credential_lock", source)
        self.assertIn("_model_state_lock", source)
        self.assertIn("headers=headers,", source)
        self.assertNotIn("headers=self._headers,", source)

    def test_openrouter_tool_responses_require_usable_content_or_calls(self):
        source = self.read("or_client.py")
        self.assertIn("tool-capable response had no choices; trying next model", source)
        self.assertIn("tool-capable response had no usable content or tool calls; trying next model", source)
        block = source[source.index("    def _call_tool_capable("):source.index("    def _call_omniroute_tool_capable(", source.index("    def _call_tool_capable("))]
        self.assertIn("if not isinstance(data, dict):", block)
        self.assertIn("if not isinstance(choices, list) or not choices:", block)
        self.assertIn("return {}", block)

    def test_dashboard_encryption_contract_is_consistent(self):
        server = self.read("dashboard/server.py")
        app = self.read("dashboard/static/app.html")
        self.assertIn("_AES_SALT = b'BRAHMA-DASHBOARD-v1'", server)
        self.assertIn("const _AES_SALT = 'BRAHMA-DASHBOARD-v1';", app)
        self.assertIn("CryptoJS.HmacSHA256(CryptoJS.enc.Hex.parse(bodyHex), _aesKey)", app)
        self.assertIn("hmac.new(aes_key, body, hashlib.sha256).digest()", server)

    def test_dashboard_download_and_ws_expiry_are_authenticated(self):
        source = self.read("dashboard/server.py")
        self.assertIn('async def download_file(req: Request, filename: str)', source)
        self.assertIn('if not _auth(req):', source)
        self.assertIn('target.relative_to(root)', source)
        ws_block = source[source.index('@app.websocket("/ws")'):]
        self.assertIn('_valid_token(tok)', ws_block)

    def test_dashboard_background_files_are_path_constrained(self):
        source = self.read("dashboard/server.py")
        self.assertIn('async def web_background_static(filename: str)', source)
        background_block = source[source.index('@app.get("/web_background/{filename:path}")'):]
        background_block = background_block[:background_block.index('@app.websocket("/ws")')]
        self.assertIn('bg_dir = (BASE_DIR / "assets" / "web_background").resolve()', background_block)
        self.assertIn('target.relative_to(bg_dir)', background_block)
        self.assertNotIn('target = bg_dir / safe', background_block)

    def test_startup_health_marker_waits_for_stable_event_loop(self):
        source = self.read("main.py")
        self.assertIn("QTimer.singleShot(15000, _mark_startup_healthy)", source)
        self.assertNotIn('        mark_startup_healthy()\n        _startup_log("startup health marker cleared")', source)


if __name__ == "__main__":
    unittest.main()

    def test_planner_rejects_malformed_or_oversized_model_plans(self):
        from unittest.mock import patch
        import agent.planner as planner

        bad_parameter_plan = '{"steps":[{"step":1,"tool":"web_search","description":"x","parameters":[],"critical":true}]}'
        oversized_plan = '{"steps":' + '[' + ','.join(
            '{"step":1,"tool":"web_search","description":"x","parameters":{}}' for _ in range(6)
        ) + ']}'

        with patch.object(planner, "_gemini_generate_text", return_value=bad_parameter_plan):
            fallback = planner.create_plan("test malformed plan")
        self.assertTrue(fallback["steps"])
        self.assertIsInstance(fallback["steps"][0]["parameters"], dict)

        with patch.object(planner, "_gemini_generate_text", return_value=oversized_plan):
            fallback2 = planner.create_plan("test oversized plan")
        self.assertLessEqual(len(fallback2["steps"]), 5)


    def test_crucible_blocks_module_alias_and_getattr_bypass_patterns(self):
        from core.skill_crucible import SkillCrucible

        cases = (
            'import os as ops\ndef execute(**kwargs):\n    return ops.system("whoami")',
            'import shutil as s\ndef execute(**kwargs):\n    return s.rmtree("x")',
            'import ssl as tls\ndef execute(**kwargs):\n    return tls._create_unverified_context()',
            'import pathlib as p\ndef execute(**kwargs):\n    return p.Path("x").unlink()',
            'import os as ops\ndef execute(**kwargs):\n    return getattr(ops, "system")("whoami")',
            'from pathlib import Path as P\ndef execute(**kwargs):\n    return getattr(P, "unlink")("x")',
        )
        for code in cases:
            ok, error = SkillCrucible.validate_ast(code)
            self.assertFalse(ok, code)
            self.assertIn("Security Violation", error or "")


    def test_dashboard_upload_destination_is_atomically_reserved(self):
        source = self.read("dashboard/server.py")
        self.assertIn("def _open_unique_upload(root: Path, safe_name: str)", source)
        self.assertIn('candidate.open("xb")', source)
        upload = source.split("async def upload_file", 1)[1].split("async def list_files", 1)[0]
        self.assertIn("_open_unique_upload(self._uploads_dir, safe)", upload)
        self.assertNotIn("while dest.exists():", upload)

    def test_executor_does_not_treat_tool_failures_as_success(self):
        from agent.executor import _raise_for_failed_tool_result

        with self.assertRaises(RuntimeError):
            _raise_for_failed_tool_result({"success": False, "error": "offline"})
        with self.assertRaises(RuntimeError):
            _raise_for_failed_tool_result('{"success": false, "error": "failed"}')
        with self.assertRaises(RuntimeError):
            _raise_for_failed_tool_result("")

    def test_tool_execution_logs_redact_credential_fields(self):
        from main import BrahmaEvo

        payload = BrahmaEvo._redact_tool_args({
            "api_key": "secret-api-key",
            "nested": {"token": "secret-token", "value": "safe"},
            "items": [{"password": "secret-password"}],
        })
        self.assertEqual(payload["api_key"], "<redacted>")
        self.assertEqual(payload["nested"]["token"], "<redacted>")
        self.assertEqual(payload["nested"]["value"], "safe")
        self.assertEqual(payload["items"][0]["password"], "<redacted>")
