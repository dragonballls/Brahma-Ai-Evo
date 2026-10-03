from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import psutil


class PCHealthGuardianTests(TestCase):
    def test_duration_parser(self):
        mod = importlib.import_module("features.pc_health_guardian.skill")
        self.assertEqual(mod._parse_duration("15 seconds"), 15)
        self.assertEqual(mod._parse_duration("5 minutes"), 300)
        self.assertEqual(mod._parse_duration("24 hours"), 86400)
        self.assertEqual(mod._parse_duration("1h 5m"), 3900)

    def test_leak_detection_requires_sustained_growth(self):
        mod = importlib.import_module("features.pc_health_guardian.skill")

        class FakeProc:
            def __init__(self, pid, name, rss):
                self.info = {
                    "pid": pid,
                    "name": name,
                    "memory_info": SimpleNamespace(rss=rss),
                }

        snapshots = [
            [FakeProc(10, "demo.exe", 100 * 1024 * 1024)],
            [FakeProc(10, "demo.exe", 150 * 1024 * 1024)],
            [FakeProc(10, "demo.exe", 220 * 1024 * 1024)],
        ]

        with patch.object(mod.psutil, "process_iter", side_effect=snapshots),              patch.object(mod.time, "sleep", return_value=None),              patch.object(mod.time, "time", side_effect=[0, 5 * 60, 10 * 60]):
            leaks = mod.detect_memory_leaks(sample_seconds=5, samples=3, limit=5)

        self.assertEqual(len(leaks), 1)
        self.assertEqual(leaks[0]["pid"], 10)
        self.assertGreater(leaks[0]["growth_mb_per_min"], 5)

    def test_repair_refuses_disruptive_actions_by_default(self):
        mod = importlib.import_module("features.pc_health_guardian.skill")
        with patch.object(mod, "diagnose", return_value={"findings": []}),              patch.object(mod, "detect_memory_leaks", return_value=[{
                 "pid": 123,
                 "name": "demo.exe",
                 "growth_mb_per_min": 20,
             }]):
            result = mod.repair(target="memory", allow_disruptive=False)
        self.assertFalse(result["actions"][0]["success"])
        self.assertIn("allow_disruptive", result["actions"][0]["message"])


class MissionNoteUiTests(TestCase):
    def test_mission_note_has_live_progress_and_timing_contract(self):
        source = __import__("pathlib").Path(__file__).resolve().parents[1].joinpath("ui.py").read_text(encoding="utf-8")
        self.assertIn('QProgressBar()', source)
        self.assertIn('self._mission_tmr.setInterval(1000)', source)
        self.assertIn('self._elapsed_lbl = QLabel("Elapsed: 00:00")', source)
        self.assertIn('self._eta_lbl = QLabel("ETA: —")', source)
        self.assertIn('self._title.setText("AUTONOMOUS MISSION NOTE")', source)
        self.assertIn('self._bar.setRange(0, 0)', source)
        self.assertIn('self._eta_lbl.setText("ETA: estimating…")', source)
        self.assertIn('self._mission_hide_btn = QPushButton("×")', source)
        self.assertIn('self._mission_hide_btn.clicked.connect(self._hide_mission_note)', source)
        self.assertIn('self._mission_note_ui_state_path', source)
        self.assertIn('def _hide_mission_note(self) -> bool:', source)
        self.assertIn('def reopen_mission_note(self) -> bool:', source)
        self.assertIn('self._set_mission_note_hidden(True, self._mission_id)', source)
        self.assertIn('self._set_mission_note_hidden(False, mission.get("mission_id"))', source)
        self.assertIn('if self._mission_note_hidden:', source)
        self.assertIn('return self._set_mission_note_visibility(True)', source)
        self.assertIn('return self._set_mission_note_visibility(False)', source)
        self.assertIn('"Mission note reopened; autonomous work continues."', source)
        self.assertIn('"Mission note hidden; autonomous work continues."', source)

class AutonomousMissionTests(TestCase):
    def test_duration_parser(self):
        mod = importlib.import_module("features.autonomous_mission.skill")
        self.assertEqual(mod._parse_duration("15 seconds"), 15)
        self.assertEqual(mod._parse_duration("5 minutes"), 300)
        self.assertEqual(mod._parse_duration("24 hours"), 86400)
        self.assertEqual(mod._parse_duration("24h 5m"), 86700)

    def test_completion_phrase(self):
        mod = importlib.import_module("features.autonomous_mission.skill")
        mission = {"completion_type": "phrase", "until": "tests pass"}
        self.assertTrue(mod._check_completion(mission, "Verification complete: all tests pass."))
        self.assertFalse(mod._check_completion(mission, "Tests are still failing."))

    def test_memory_completion(self):
        mod = importlib.import_module("features.autonomous_mission.skill")
        mission = {"completion_type": "memory_below", "completion_target": "70"}
        with patch.object(mod.psutil, "virtual_memory", return_value=type("VM", (), {"percent": 65})()):
            self.assertTrue(mod._check_completion(mission, "ignored"))

if __name__ == "__main__":
    import unittest
    unittest.main()
