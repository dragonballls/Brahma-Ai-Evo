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
        self.assertIn("Mission note reopened; autonomous work continues.", source)
        self.assertIn("Mission note hidden; autonomous work continues.", source)

class AutonomousMissionTests(TestCase):
    def test_duration_parser(self):
        mod = _load_skill_module("autonomous_mission_skill", "features/autonomous_mission/skill.py")
        self.assertEqual(mod._parse_duration("15 seconds"), 15)
        self.assertEqual(mod._parse_duration("5 minutes"), 300)
        self.assertEqual(mod._parse_duration("24 hours"), 86400)
        self.assertEqual(mod._parse_duration("24h 5m"), 86700)

    def test_completion_phrase(self):
        mod = _load_skill_module("autonomous_mission_skill", "features/autonomous_mission/skill.py")
        mission = {"completion_type": "phrase", "until": "tests pass"}
        self.assertTrue(mod._check_completion(mission, "Verification complete: all tests pass."))
        self.assertFalse(mod._check_completion(mission, "Tests are still failing."))

    def test_memory_completion(self):
        mod = _load_skill_module("autonomous_mission_skill", "features/autonomous_mission/skill.py")
        mission = {"completion_type": "memory_below", "completion_target": "70"}
        with patch.object(mod.psutil, "virtual_memory", return_value=type("VM", (), {"percent": 65})()):
            self.assertTrue(mod._check_completion(mission, "ignored"))


class PCHealthGuardianEdgeCaseTests(TestCase):
    def test_duration_parser_clamps_numeric_input_and_preserves_zero(self):
        mod = _load_skill_module("pc_health_guardian_skill_edge_duration", "features/pc_health_guardian/skill.py")
        self.assertEqual(mod._parse_duration(0), 0)
        self.assertEqual(mod._parse_duration(31 * 86400), 30 * 86400)

    def test_diagnose_skips_expensive_integrity_check_unless_deep(self):
        mod = _load_skill_module("pc_health_guardian_skill_edge_diagnose", "features/pc_health_guardian/skill.py")
        with patch.object(mod, "_memory_summary", return_value={"percent": 10, "used_gb": 1, "available_gb": 9, "total_gb": 10, "swap_percent": 0, "swap_used_gb": 0}), \
             patch.object(mod, "_snapshot_processes", return_value=[]), \
             patch.object(mod, "_storage_summary", return_value=[]), \
             patch.object(mod.psutil, "boot_time", return_value=0), \
             patch.object(mod.time, "time", side_effect=[3600, 3600]), \
             patch.object(mod.psutil, "cpu_percent", return_value=5), \
             patch.object(mod.psutil, "pids", return_value=[1, 2]), \
             patch.object(mod, "_network_check", return_value={"healthy": True, "checks": {}}), \
             patch.object(mod, "_event_errors", return_value=[]), \
             patch.object(mod, "_windows_integrity_check") as integrity:
            result = mod.diagnose(deep=False)
        integrity.assert_not_called()
        self.assertTrue(result["integrity"]["skipped"])

    def test_protected_process_cannot_be_terminated(self):
        mod = _load_skill_module("pc_health_guardian_skill_edge_protected", "features/pc_health_guardian/skill.py")
        fake = SimpleNamespace(name=lambda: "explorer.exe", terminate=lambda: (_ for _ in ()).throw(AssertionError("terminate called")))
        with patch.object(mod.psutil, "Process", return_value=fake):
            result = mod._terminate_process(1234)
        self.assertFalse(result["success"])
        self.assertIn("protected process", result["message"].lower())

    def test_monitor_zero_duration_is_passed_as_zero_not_indefinite(self):
        mod = _load_skill_module("pc_health_guardian_skill_edge_monitor", "features/pc_health_guardian/skill.py")
        captured = {}

        class FakeThread:
            def __init__(self, target=None, kwargs=None, daemon=None, name=None):
                captured["kwargs"] = kwargs or {}
                self._alive = False
            def start(self):
                self._alive = False
            def is_alive(self):
                return self._alive

        mod._WATCH_THREAD = None
        with patch.object(mod.threading, "Thread", FakeThread):
            result = mod.start_monitor(duration=0, interval_seconds=60, auto_repair=False)
        self.assertIn("0s", result)
        self.assertEqual(captured["kwargs"]["duration_seconds"], 0.0)
        mod._WATCH_THREAD = None


class AutonomousMissionEdgeCaseTests(TestCase):
    def test_duration_parser_clamps_to_thirty_days(self):
        mod = _load_skill_module("autonomous_mission_skill_edge_duration", "features/autonomous_mission/skill.py")
        self.assertEqual(mod._parse_duration("31 days"), 30 * 86400)

    def test_completion_strategies(self):
        mod = _load_skill_module("autonomous_mission_skill_edge_completion", "features/autonomous_mission/skill.py")
        self.assertTrue(mod._check_completion({"completion_type": "regex", "completion_target": r"all\s+tests\s+pass"}, "ALL tests pass"))
        with __import__("tempfile").TemporaryDirectory() as td:
            path = str(Path(td) / "done.txt")
            Path(path).write_text("done", encoding="utf-8")
            self.assertTrue(mod._check_completion({"completion_type": "file_exists", "completion_target": path}, "ignored"))
        with patch.object(mod, "_iter_processes", return_value=iter([{"pid": 99, "name": "demo.exe"}])):
            self.assertFalse(mod._check_completion({"completion_type": "process_gone", "completion_target": "demo.exe"}, "ignored"))
        with patch.object(mod.psutil, "virtual_memory", return_value=SimpleNamespace(percent=40)):
            self.assertTrue(mod._check_completion({"completion_type": "memory_below", "completion_target": "50"}, "ignored"))

    def test_start_status_list_and_cancel_persist_state(self):
        mod = _load_skill_module("autonomous_mission_skill_edge_lifecycle", "features/autonomous_mission/skill.py")
        with __import__("tempfile").TemporaryDirectory() as td:
            td_path = Path(td)
            old_file, old_dir = mod._STATE_FILE, mod._STATE_DIR
            mod._STATE_DIR = td_path
            mod._STATE_FILE = td_path / "missions.json"
            mod._MISSIONS.clear()
            mod._EVENTS.clear()
            mod._THREADS.clear()

            class FakeThread:
                def __init__(self, target=None, args=None, daemon=None, name=None):
                    self.target, self.args, self.daemon, self.name = target, args, daemon, name
                def start(self):
                    pass
                def is_alive(self):
                    return False

            try:
                with patch.object(mod.threading, "Thread", FakeThread):
                    reply = mod.start_mission("verify the build", duration="5 minutes")
                mission_id = reply.split("mission-", 1)[1].split(" for", 1)[0]
                mission_id = "mission-" + mission_id
                self.assertEqual(mod.status(mission_id)["status"], "pending")
                self.assertEqual(mod.list_missions()[0]["mission_id"], mission_id)
                self.assertTrue(mod._STATE_FILE.exists())
                cancel_reply = mod.cancel(mission_id)
                self.assertIn("Cancellation requested", cancel_reply)
                self.assertEqual(mod.status(mission_id)["status"], "cancelling")
            finally:
                mod._STATE_FILE, mod._STATE_DIR = old_file, old_dir
                mod._MISSIONS.clear()
                mod._EVENTS.clear()
                mod._THREADS.clear()

    def test_cancellation_wins_over_a_successful_executor_result(self):
        mod = _load_skill_module("autonomous_mission_skill_edge_cancel", "features/autonomous_mission/skill.py")
        import types
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        executor_mod = types.ModuleType("agent.executor")

        class FakeExecutor:
            def execute(self, goal, cancel_flag=None, player=None):
                cancel_flag.set()
                return "Everything is done"

        executor_mod.AgentExecutor = FakeExecutor
        mod._MISSIONS.clear()
        mod._EVENTS.clear()
        mod._THREADS.clear()
        mod._MISSIONS["mission-cancel"] = {
            "mission_id": "mission-cancel",
            "goal": "test cancellation",
            "status": "running",
            "created_at": 0,
            "duration_seconds": None,
            "until": None,
            "completion_type": "agent_success",
            "completion_target": None,
            "interval_seconds": 5,
            "keep_working": False,
            "iterations": 0,
            "last_result": "",
            "error": "",
        }
        mod._EVENTS["mission-cancel"] = __import__("threading").Event()
        with patch.dict(__import__("sys").modules, {"agent": agent_pkg, "agent.executor": executor_mod}), \
             patch.object(mod, "_save"):
            mod._mission_loop("mission-cancel")
        self.assertEqual(mod.status("mission-cancel")["status"], "cancelled")

    def test_deadline_preempts_an_executor_that_runs_past_the_time_limit(self):
        mod = _load_skill_module("autonomous_mission_skill_edge_deadline", "features/autonomous_mission/skill.py")
        import types
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        executor_mod = types.ModuleType("agent.executor")

        class SlowExecutor:
            def execute(self, goal, cancel_flag=None, player=None):
                __import__("time").sleep(0.05)
                return "still working"

        executor_mod.AgentExecutor = SlowExecutor
        mod._MISSIONS.clear()
        mod._EVENTS.clear()
        mod._THREADS.clear()
        mod._MISSIONS["mission-deadline"] = {
            "mission_id": "mission-deadline",
            "goal": "test deadline",
            "status": "pending",
            "created_at": 0,
            "duration_seconds": 0.01,
            "until": None,
            "completion_type": "agent_success",
            "completion_target": None,
            "interval_seconds": 5,
            "keep_working": False,
            "iterations": 0,
            "last_result": "",
            "error": "",
        }
        mod._EVENTS["mission-deadline"] = __import__("threading").Event()
        with patch.dict(__import__("sys").modules, {"agent": agent_pkg, "agent.executor": executor_mod}), \
             patch.object(mod, "_save"):
            mod._mission_loop("mission-deadline")
        self.assertEqual(mod.status("mission-deadline")["status"], "timed_out")


class PublicActionDispatchTests(TestCase):
    def test_pc_health_action_dispatch(self):
        mod = _load_skill_module("pc_health_guardian_skill_dispatch", "features/pc_health_guardian/skill.py")
        with patch.object(mod, "diagnose", return_value={"findings": [], "memory": {"percent": 1}, "cpu_percent": 2}), \\
             patch.object(mod, "detect_memory_leaks", return_value=[]), \\
             patch.object(mod, "repair", return_value={"success": True, "actions": []}), \\
             patch.object(mod, "start_monitor", return_value="started"), \\
             patch.object(mod, "stop_monitor", return_value="stopped"), \\
             patch.object(mod, "get_status", return_value={"running": False}):
            self.assertIn("PC diagnosis complete", mod.execute(action="diagnose")["summary"])
            self.assertEqual(mod.execute(action="memory_leak")["leaks"], [])
            self.assertTrue(mod.execute(action="repair")["output"]["success"])
            self.assertEqual(mod.execute(action="monitor")["summary"], "started")
            self.assertEqual(mod.execute(action="stop")["summary"], "stopped")
            self.assertEqual(mod.execute(action="status")["status"]["running"], False)

    def test_autonomous_action_dispatch(self):
        mod = _load_skill_module("autonomous_mission_skill_dispatch", "features/autonomous_mission/skill.py")
        with patch.object(mod, "start_mission", return_value="Started autonomous mission mission-test for 5 minutes."), \\
             patch.object(mod, "list_missions", return_value=[{"mission_id": "mission-test"}]), \\
             patch.object(mod, "status", return_value={"mission_id": "mission-test", "status": "running"}), \\
             patch.object(mod, "cancel", return_value="Cancellation requested for mission mission-test."):
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
