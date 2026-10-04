from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RecoverySupervisorTests(unittest.TestCase):
    def read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_supervisor_exists_and_is_stdlib_first(self):
        source = self.read("scripts/recovery_supervisor.py")
        self.assertIn("def run_supervisor()", source)
        self.assertIn("child.wait()", source)
        self.assertIn("_run_recovery(tb, state)", source)
        self.assertIn("MAX_HEAL_ATTEMPTS_PER_CRASH = 2", source)
        self.assertIn("MAX_RAPID_RESTARTS = 6", source)

    def test_supervisor_keeps_recovery_outside_main_process(self):
        source = self.read("start_brahma.vbs")
        self.assertIn("supervisorExe", source)
        self.assertIn("recovery_supervisor.py", source)
        self.assertIn("ElseIf fso.FileExists(venvPython) And fso.FileExists(supervisorPy)", source)

    def test_bootstrap_prefers_supervisor(self):
        source = self.read("bootstrap.ps1")
        self.assertIn("crash-recovery supervisor", source)
        self.assertIn("$SupervisorPy = Join-Path $WorkingDir", source)
        self.assertIn("recovery_supervisor.py", source)

    def test_installer_shortcuts_and_startup_use_supervisor(self):
        source = self.read("installer/install_wizard.py")
        self.assertIn("BrahmaEvo_Supervisor.exe", source)
        self.assertIn('shell.SpecialFolders("Startup")', source)
        self.assertIn("Brahma Evo Recovery.lnk", source)

    def test_windows_build_packages_supervisor(self):
        workflow = self.read(".github/workflows/windows-release.yml")
        spec = self.read("installer/BrahmaEvo_Supervisor.spec")
        self.assertIn("Build recovery supervisor", workflow)
        self.assertIn("installer/BrahmaEvo_Supervisor.spec", workflow)
        self.assertIn("dist/BrahmaEvo/BrahmaEvo_Supervisor.exe", workflow)
        self.assertIn('name=\'BrahmaEvo_Supervisor\'', spec)

    def test_autoheal_runtime_state_is_initialized(self):
        source = self.read("actions/auto_heal_engine.py")
        self.assertIn("import threading", source)
        self.assertIn("_auto_lock = threading.RLock()", source)
        self.assertIn("_auto_inflight: set[str] = set()", source)
        self.assertIn("_auto_recent: Dict[str, float] = {}", source)
        self.assertIn("AUTO_HEAL_COOLDOWN_SECONDS = 300.0", source)
        self.assertIn('"recovery_supervisor.py"', source)

    def test_supervisor_source_command_points_at_main(self):
        from scripts.recovery_supervisor import ROOT, _child_command

        if getattr(sys, "frozen", False):
            self.skipTest("source command assertion applies to non-frozen test runner")
        cmd = _child_command()
        self.assertGreaterEqual(len(cmd), 2)
        self.assertEqual(Path(cmd[1]).resolve(), (ROOT / "main.py").resolve())
        self.assertEqual(cmd[-1], "--startup")

    def test_external_crash_invokes_bounded_healer(self):
        import actions.auto_heal_engine as auto_heal_engine
        import core.boot_sentry as boot_sentry
        import scripts.recovery_supervisor as supervisor

        tb = 'Traceback (most recent call last):\\n  File "actions/open_app.py", line 10, in execute\\nRuntimeError: demo failure'
        state = {}
        with patch.object(boot_sentry, "check_and_recover_on_boot", return_value=False),              patch.object(auto_heal_engine.AutoHealEngine, "heal_traceback", return_value={"success": True, "message": "fixed"}) as heal:
            result = supervisor._run_recovery(tb, state)

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "auto_heal")
        self.assertEqual(result["message"], "fixed")
        self.assertEqual(state["heal_attempts"], 1)
        heal.assert_called_once()

    def test_supervisor_does_not_require_windows_to_import(self):
        import scripts.recovery_supervisor as supervisor

        self.assertTrue(supervisor.LOG_PATH.name.endswith(".log"))
        self.assertTrue(os.path.isabs(str(supervisor.STATE_PATH)))


if __name__ == "__main__":
    unittest.main()
