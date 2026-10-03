from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from core.desktop.app_host import ApplicationHost
from core.desktop.performance import AdaptivePerformanceEngine, PerformanceSnapshot
from core.desktop.window_manager import WindowInfo, WindowManager, is_game_window
from core.desktop.workspace import WorkspaceStore

class WorkspaceStoreTests(TestCase):
    def test_workspace_store_is_crash_safe_and_persistent(self):
        module = __import__("core.desktop.workspace", fromlist=["WorkspaceStore"])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "workspace.json"
            store = module.WorkspaceStore(path)
            self.assertTrue(store.set(desktop_mode=True))
            state = store.upsert_window(
                "main",
                {"identity": "chrome.exe:Google", "title": "Google", "exe": "chrome.exe"},
            )
            self.assertTrue(state)
            loaded = module.WorkspaceStore(path).load()
            self.assertTrue(loaded["desktop_mode"])
            self.assertEqual(len(loaded["workspaces"]["main"]["windows"]), 1)

    def test_remove_window_is_idempotent(self):
        module = __import__("core.desktop.workspace", fromlist=["WorkspaceStore"])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "workspace.json"
            store = module.WorkspaceStore(path)
            store.upsert_window("main", {"identity": "demo"})
            self.assertTrue(store.remove_window("main", "demo"))
            self.assertFalse(store.remove_window("main", "demo"))


class PerformancePolicyTests(TestCase):
    def test_game_foreground_reduces_background_and_prioritizes_game(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        engine = module.AdaptivePerformanceEngine()
        snapshot = module.PerformanceSnapshot(
            timestamp=time.time(),
            cpu_percent=42,
            memory_percent=60,
            memory_available_mb=4096,
            gpu_percent=90,
            foreground_pid=10,
            foreground_title="Minecraft",
            foreground_exe="javaw.exe",
            game_active=True,
        )
        decision = engine.decide(snapshot)
        self.assertEqual(decision.mode, "game")
        self.assertTrue(decision.prioritize_foreground)
        self.assertTrue(decision.reduce_background_work)

    def test_adaptive_policy_does_not_mutate_on_normal_pressure(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        engine = module.AdaptivePerformanceEngine()
        snapshot = module.PerformanceSnapshot(
            timestamp=time.time(),
            cpu_percent=25,
            memory_percent=45,
            memory_available_mb=8192,
            gpu_percent=20,
            foreground_pid=10,
            foreground_title="Google",
            foreground_exe="chrome.exe",
            game_active=False,
        )
        decision = engine.decide(snapshot)
        self.assertFalse(decision.reduce_background_work)
        self.assertFalse(decision.prioritize_foreground)
        self.assertFalse(decision.trim_background_memory)

    def test_invalid_profile_is_rejected(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        engine = module.AdaptivePerformanceEngine()
        with self.assertRaises(ValueError):
            engine.set_profile("dangerous")

    def test_priority_changes_are_restorable_by_pid_and_create_time(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        fake = SimpleNamespace(pid=321, create_time=lambda: 100.0, nice=lambda *args: 8)
        engine = module.AdaptivePerformanceEngine()
        engine._original_priority[(321, 100.0)] = 8
        fake.nice = lambda *_: 8
        with patch.object(module, "psutil", SimpleNamespace(Process=lambda pid: fake)):
            with patch.object(module.WindowManager, "set_priority", return_value=True) as set_priority:
                restored = engine.restore()
        self.assertEqual(restored, 1)
        set_priority.assert_called_once_with(fake, 8)


class WindowPolicyTests(TestCase):
    def test_game_window_detection_is_title_and_executable_based(self):
        module = __import__("core.desktop.window_manager", fromlist=["is_game_window"])
        self.assertTrue(module.is_game_window(module.WindowInfo(1, 2, "Minecraft 1.21", "javaw.exe", False, True)))
        self.assertTrue(module.is_game_window(module.WindowInfo(1, 2, "Roblox", "RobloxPlayerBeta.exe", False, True)))
        self.assertFalse(module.is_game_window(module.WindowInfo(1, 2, "Google", "chrome.exe", False, True)))

    def test_windows_protected_processes_are_never_candidates(self):
        module = __import__("core.desktop.window_manager", fromlist=["WindowManager"])
        protected = SimpleNamespace(name=lambda: "explorer.exe")
        self.assertTrue(module.WindowManager.is_protected_process(protected))


class ApplicationHostTests(TestCase):
    def test_empty_target_is_rejected_without_touching_windows(self):
        module = __import__("core.desktop.app_host", fromlist=["ApplicationHost"])
        result = module.ApplicationHost().open("")
        self.assertFalse(result["ok"])

    def test_window_lookup_requires_a_real_match(self):
        module = __import__("core.desktop.app_host", fromlist=["ApplicationHost"])
        host = module.ApplicationHost()
        with patch.object(module.WindowManager, "enumerate_windows", return_value=[]):
            self.assertIsNone(host._find("definitely-not-a-window"))


class WindowsBackendTests(TestCase):
    def test_workerw_backend_is_not_attached_by_default(self):
        module = __import__("core.desktop.windows_desktop", fromlist=["WindowsDesktopHost"])
        host = module.WindowsDesktopHost()
        self.assertFalse(host.attached)
        self.assertEqual(host.status()["backend"], "bottommost-window")

    def test_windows_folder_process_is_not_user_process(self):
        module = __import__("core.desktop.window_manager", fromlist=["WindowManager"])
        fake = SimpleNamespace(
            name=lambda: "test-system.exe",
            exe=lambda: r"C:\Windows\System32\test-system.exe",
        )
        with patch.dict(module.os.environ, {"WINDIR": r"C:\Windows"}, clear=False):
            self.assertFalse(module.WindowManager.is_user_process(fake))


class NativeHostTests(TestCase):
    def test_native_host_rejects_missing_target_without_touching_windows(self):
        module = __import__("core.desktop.native_host", fromlist=["NativeWindowHost"])
        host = module.NativeWindowHost()
        with patch.object(module.WindowManager, "enumerate_windows", return_value=[]):
            result = host.host("not-a-real-window")
        self.assertFalse(result["ok"])
        self.assertFalse(result["embedded"])

    def test_foreign_window_lookup_matches_exact_executable(self):
        module = __import__("core.desktop.native_host", fromlist=["NativeWindowHost"])
        window = SimpleNamespace(hwnd=44, pid=55, title="Example App", exe="example.exe")
        with patch.object(module.WindowManager, "enumerate_windows", return_value=[window]):
            self.assertEqual(module.NativeWindowHost.find_target("example.exe"), window)


class MainLifecycleTests(TestCase):
    def test_main_contains_local_qapplication_shutdown_hook(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        source = main_path.read_text(encoding="utf-8")
        self.assertIn("from PyQt6.QtWidgets import QApplication", source)
        self.assertIn("aboutToQuit.connect(desktop_controller.shutdown)", source)


class MemoryPrioritySafetyTests(TestCase):
    def test_memory_priority_capture_fails_closed(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        engine = module.AdaptivePerformanceEngine()
        proc = SimpleNamespace(pid=77, create_time=lambda: 123.0)
        with patch.object(module.WindowManager, "get_memory_priority", return_value=None):
            self.assertIsNone(engine._remember_memory_priority(proc))


class PressureHysteresisTests(TestCase):
    def test_pressure_hysteresis_rejects_brief_spikes(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        engine = module.AdaptivePerformanceEngine()
        first = engine._stabilize_pressure("high")
        self.assertEqual(first, "normal")
        engine._pressure_candidate_since -= 6.0
        self.assertEqual(engine._stabilize_pressure("high"), "high")

    def test_pressure_clear_requires_a_longer_stable_window(self):
        module = __import__("core.desktop.performance", fromlist=["AdaptivePerformanceEngine"])
        engine = module.AdaptivePerformanceEngine()
        engine._pressure_state = "high"
        engine._pressure_candidate = "normal"
        engine._pressure_candidate_since = time.monotonic()
        self.assertEqual(engine._stabilize_pressure("normal"), "high")
        engine._pressure_candidate_since -= 9.0
        self.assertEqual(engine._stabilize_pressure("normal"), "normal")


class LaunchDiscoveryTests(TestCase):
    def test_find_after_launch_prefers_new_matching_window(self):
        module = __import__("core.desktop.app_host", fromlist=["ApplicationHost"])
        host = module.ApplicationHost()
        window = SimpleNamespace(pid=999, visible=True, title="Minecraft", exe="javaw.exe", hwnd=77)
        with patch.object(module.WindowManager, "enumerate_windows", return_value=[window]):
            result = host.find_after_launch("minecraft", baseline_pids={1}, timeout=0.5)
        self.assertEqual(result, window)

    def test_find_after_launch_ignores_hidden_windows(self):
        module = __import__("core.desktop.app_host", fromlist=["ApplicationHost"])
        host = module.ApplicationHost()
        hidden = SimpleNamespace(pid=999, visible=False, title="Minecraft", exe="javaw.exe", hwnd=77)
        with patch.object(module.WindowManager, "enumerate_windows", return_value=[hidden]):
            result = host.find_after_launch("minecraft", baseline_pids=set(), timeout=0.5)
        self.assertIsNone(result)
