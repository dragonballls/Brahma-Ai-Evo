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
        self.assertTrue(str(API_CONFIG_PATH).endswith("config/api_keys.json"))

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

    def test_unsafe_core_updater_reset_is_gone(self):
        source = self.read("core/updater.py")
        self.assertNotIn('["git", "reset", "--hard"', source)
        self.assertIn("from updater import restart_application, update_from_github", source)

    def test_ui_omniroute_uses_central_page_and_gateway_endpoint(self):
        source = self.read("ui.py")
        self.assertIn('"omniroute": 5', source)
        self.assertIn("gateway().base_url", source)
        self.assertIn("gateway().ensure_ready(force=True)", source)

    def test_requirements_do_not_duplicate_package_names(self):
        names = []
        for line in self.read("requirements.txt").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].strip().lower())
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
