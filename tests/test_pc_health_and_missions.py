from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import psutil


def _load_skill_module(name: str, relative_path: str):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PCHealthGuardianTests(TestCase):
    def test_duration_parser(self):
        mod = _load_skill_module("pc_health_guardian_skill", "features/pc_health_guardian/skill.py")
        self.assertEqual(mod._parse_duration("15 seconds"), 15)
        self.assertEqual(mod._parse_duration("5 minutes"), 300)
        self.assertEqual(mod._parse_duration("24 hours"), 86400)
        self.assertEqual(mod._parse_duration("1h 5m"), 3900)

    def test_leak_detection_requires_sustained_growth(self):
        mod = _load_skill_module("pc_health_guardian_skill", "features/pc_health_guardian/skill.py")

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
        mod = _load_skill_module("pc_health_guardian_skill", "features/pc_health_guardian/skill.py")
        with (
            patch.object(mod, "diagnose", return_value={"findings": [], "memory": {"percent": 1}, "cpu_percent": 2}),
            patch.object(mod, "detect_memory_leaks", return_value=[]),
            patch.object(mod, "repair", return_value={"success": True, "actions": []}),
            patch.object(mod, "start_monitor", return_value="started"),
            patch.object(mod, "stop_monitor", return_value="stopped"),
            patch.object(mod, "get_status", return_value={"running": False}),
        ):
            self.assertIn("PC diagnosis complete", mod.execute(action="diagnose")["summary"])
            self.assertEqual(mod.execute(action="memory_leak")["leaks"], [])
            self.assertTrue(mod.execute(action="repair")["output"]["success"])
            self.assertEqual(mod.execute(action="monitor")["summary"], "started")
            self.assertEqual(mod.execute(action="stop")["summary"], "stopped")
            self.assertEqual(mod.execute(action="status")["status"]["running"], False)

    def test_autonomous_action_dispatch(self):
        mod = _load_skill_module("autonomous_mission_skill_dispatch", "features/autonomous_mission/skill.py")
        with (
            patch.object(mod, "start_mission", return_value="Started autonomous mission mission-test for 5 minutes."),
            patch.object(mod, "list_missions", return_value=[{"mission_id": "mission-test"}]),
            patch.object(mod, "status", return_value={"mission_id": "mission-test", "status": "running"}),
            patch.object(mod, "cancel", return_value="Cancellation requested for mission mission-test."),
        ):
            self.assertIn("mission-test", mod.execute(action="start", goal="test")["summary"])
            self.assertEqual(mod.execute(action="list")["missions"][0]["mission_id"], "mission-test")
            self.assertEqual(mod.execute(action="status", mission_id="mission-test")["status"]["status"], "running")
            self.assertIn("Cancellation requested", mod.execute(action="cancel", mission_id="mission-test")["summary"])

    def test_invalid_pc_monitor_duration_is_rejected(self):
        mod = _load_skill_module("pc_health_guardian_skill_invalid_duration", "features/pc_health_guardian/skill.py")
        with patch.object(mod.threading, "Thread") as thread:
            result = mod.start_monitor(duration="not-a-duration")
        self.assertIn("could not parse", result.lower())
        thread.assert_not_called()

if __name__ == "__main__":
    import unittest
    unittest.main()
