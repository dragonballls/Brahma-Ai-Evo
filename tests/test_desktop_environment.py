from __future__ import annotations

import importlib.util
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


def _load(relative_path: str, name: str):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkspaceStoreTests(TestCase):
    def test_workspace_store_is_crash_safe_and_persistent(self):
        module = _load("core/desktop/workspace.py", "desktop_workspace_test")
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
        module = _load("core/desktop/workspace.py", "desktop_workspace_remove")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "workspace.json"
            store = module.WorkspaceStore(path)
            store.upsert_window("main", {"identity": "demo"})
            self.assertTrue(store.remove_window("main", "demo"))
            self.assertFalse(store.remove_window("main", "demo"))


class PerformancePolicyTests(TestCase):
    def test_game_foreground_reduces_background_and_prioritizes_game(self):
        module = _load("core/desktop/performance.py", "desktop_performance_policy")
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
        module = _load("core/desktop/performance.py", "desktop_performance_normal")
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
        module = _load("core/desktop/performance.py", "desktop_performance_profile")
        engine = module.AdaptivePerformanceEngine()
        with self.assertRaises(ValueError):
            engine.set_profile("dangerous")

    def test_priority_changes_are_restorable_by_pid_and_create_time(self):
        module = _load("core/desktop/performance.py", "desktop_performance_restore")
        fake = SimpleNamespace(pid=321, create_time=lambda: 100.0, nice=lambda *args: 8)
        engine = module.AdaptivePerformanceEngine()
        engine._original_priority[(321, 100.0)] = 8
        fake.nice = lambda *_: 8
        with patch.object(module, "psutil", SimpleNamespace(Process=lambda pid: fake)):
            with patch.object(module.WindowManager, "set_priority", return_value=True) as set_priority:
                restored = engine.restore()
        self.assertEqual(restored, 1)
        set_priority.assert_called_once_with(fake, 8)
