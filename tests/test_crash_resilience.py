from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CrashResilienceTests(unittest.TestCase):
    def test_supervisor_has_independent_lifecycle_and_recovery_modes(self):
        source = (ROOT / "core" / "process_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("SUPERVISOR_MUTEX", source)
        self.assertIn("BRAHMA_SUPERVISED", source)
        self.assertIn("--recover-crash", source)
        self.assertIn("RESTART_DELAYS", source)

    def test_supervisor_launches_main_as_child(self):
        from core.process_supervisor import application_command
        cmd = application_command(ROOT)
        self.assertTrue(cmd)
        self.assertTrue(cmd[-1] == "--startup")
        self.assertTrue(str(ROOT / "main.py") in cmd or str(ROOT / "BrahmaEvo.exe") in cmd)

    def test_source_recovery_is_a_separate_process(self):
        from core.process_supervisor import recovery_command
        cmd = recovery_command(ROOT)
        self.assertTrue(cmd)
        self.assertEqual(cmd[-1], "--recover-crash")
        self.assertTrue(cmd[-2].endswith("crash_recovery.py"))

    def test_main_exposes_out_of_process_recovery_switch(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('sys.argv[1] == "--recover-crash"', source)
        self.assertIn("recover_from_crash()", source)

    def test_crash_recovery_has_repeat_crash_circuit_breaker(self):
        source = (ROOT / "core" / "crash_recovery.py").read_text(encoding="utf-8")
        self.assertIn("blocked", source)
        self.assertIn("repair_attempts", source)
        self.assertIn("crash_fingerprint", source)
        self.assertIn("rolled_back_after_crash", source)

    def test_crash_signature_is_stable(self):
        from core.crash_recovery import _fingerprint
        trace = "Traceback (most recent call last):\n  File 'demo.py', line 4\nValueError: boom"
        self.assertEqual(_fingerprint(trace), _fingerprint(trace + "\n"))

    def test_self_heal_cannot_patch_supervisor_or_recovery_worker(self):
        source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
        self.assertIn('"process_supervisor.py"', source)
        self.assertIn('"crash_recovery.py"', source)

    def test_windows_launcher_prefers_supervisor(self):
        source = (ROOT / "start_brahma.vbs").read_text(encoding="utf-8")
        self.assertIn("BrahmaEvoSupervisor.exe", source)
        self.assertIn("process_supervisor.py", source)

    def test_installer_creates_supervised_desktop_and_startup_paths(self):
        source = (ROOT / "installer" / "install_wizard.py").read_text(encoding="utf-8")
        self.assertIn("supervisor_path", source)
        self.assertIn("SpecialFolders("Startup")", source)
        self.assertIn("Brahma Evo.lnk", source)

    def test_build_pipeline_builds_and_validates_supervisor(self):
        build = (ROOT / "build_all.ps1").read_text(encoding="utf-8")
        self.assertIn("BrahmaEvo_Supervisor.spec", build)
        self.assertIn("BrahmaEvoSupervisor.exe", build)

        workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
        self.assertIn("BrahmaEvo_Supervisor.spec", workflow)
        self.assertIn("BrahmaEvoSupervisor.exe", workflow)


if __name__ == "__main__":
    unittest.main()
