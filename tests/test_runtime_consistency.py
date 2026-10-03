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

    def test_live_voice_does_not_retry_without_gemini_credential(self):
        source = self.read("main.py")
        self.assertIn("def _has_gemini_voice_credentials()", source)
        self.assertIn("if not offline_mode and not is_local(selected_provider) and gemini_voice_ready:", source)
        self.assertIn("Continuous Live voice is unavailable without a Gemini voice credential", source)

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

    def test_requirements_do_not_duplicate_package_names(self):
        names = []
        for line in self.read("requirements.txt").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip().lower())
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
