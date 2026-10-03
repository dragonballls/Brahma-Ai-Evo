from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.single_instance import SingleInstance


class LowPowerGuardTests(unittest.TestCase):
    def read(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_webgl_is_not_uncapped(self):
        main = self.read("main.py")
        ui = self.read("ui.py")
        globe = self.read("core/globe_window.py")
        html = self.read("assets/web_background/index.html")
        for source in (main, ui, globe):
            self.assertNotIn("--disable-frame-rate-limit", source)
            self.assertNotIn("--disable-gpu-vsync", source)
            self.assertNotIn("fmt.setSwapInterval(0)", source)
            self.assertIn("--num-raster-threads=2", source)
        self.assertIn("fmt.setSwapInterval(1)", main)
        self.assertIn("fmt.setSwapInterval(1)", globe)
        self.assertIn("powerPreference: 'low-power'", html)
        self.assertIn("Math.min(window.devicePixelRatio || 1, 1.25)", html)
        self.assertNotIn("requestAnimationFrame(renderLoop)", html)
        self.assertIn("window.setTimeout(renderLoop", html)

    def test_background_monitor_is_lazy(self):
        source = self.read("actions/background_monitor.py")
        self.assertIn("_ensure_monitor_thread()", source)
        self.assertIn("def add_monitor(", source)
        self.assertIn("if not _monitors:", source)

    def test_runtime_configuration_is_not_bundled_from_repo_state(self):
        spec = self.read("installer/BrahmaEvo.spec")
        self.assertNotIn("(os.path.join(cwd, 'config'), 'config')", spec)
        self.assertIn("(os.path.join(cwd, 'config', 'models'), 'config', 'models')", spec)
        self.assertIn("(os.path.join(cwd, 'config', 'intelligence.json'), 'config', 'intelligence.json')", spec)
        ui = self.read("ui.py")
        self.assertIn('CONFIG_DIR = get_user_data_dir() / "config"', ui)
        self.assertNotIn('Path("config/api_keys.json")', ui)
        self.assertNotIn('Path("config/ig_session.json")', ui)

    def test_passive_watchers_back_off(self):
        sensorium = self.read("core/sensorium.py")
        attention = self.read("actions/attention_monitor.py")
        clipboard = self.read("core/clipboard_sentry.py")
        self.assertIn("poll_interval: float = 10.0", sensorium)
        self.assertIn("sleep_for = 30.0 if self.user_idle_seconds >= 120.0 else self.poll_interval", sensorium)
        self.assertIn("interval: float = 5.0", attention)
        self.assertIn("self._wake.wait(2.5)", clipboard)

    def test_audio_device_prefetch_is_idempotent_and_generation_safe(self):
        source = self.read("core/audio_devices.py")
        self.assertIn("_cache_generation = 0", source)
        self.assertIn("_prefetch_thread: threading.Thread | None = None", source)
        self.assertIn("if _prefetch_thread is not None and _prefetch_thread.is_alive()", source)
        self.assertIn("generation == _cache_generation", source)
        self.assertIn("_cache_generation += 1", source)

    def test_globe_is_lazy_loaded(self):
        main = self.read("main.py")
        self.assertNotIn("GlobeWindow.get_instance(parent=None)", main)
        self.assertIn("GlobeWindow.get_instance(parent=self.ui._win)", main)

    def test_clipboard_ai_is_opt_in(self):
        main = self.read("main.py")
        self.assertIn('get_setting("clipboard_auto_comment_enabled", False)', main)

    def test_clipboard_has_one_watcher_and_voice_guards(self):
        main = self.read("main.py")
        clipboard = self.read("core/clipboard_sentry.py")
        ui = self.read("ui.py")
        self.assertNotIn("def _clipboard_monitor():", main)
        self.assertIn("def _clipboard_ai_handler(category: str, content: str):", main)
        self.assertIn("self._clipboard_ai_handler", ui)
        self.assertIn("self._wake = threading.Event()", clipboard)
        self.assertIn("self._wake.wait(2.5)", clipboard)
        self.assertIn("from core.voice_guard import VoiceCommandGate, VoiceToolExecutionGate", main)
        self.assertIn("self._voice_command_gate.accept(text)", main)
        self.assertIn("self._voice_tool_gate.allow(fc.name, args)", main)
        self.assertIn("Generic \"hey/hi/hello\" must never wake", main)

    def test_deep_idle_suspends_nonessential_work(self):
        ui = self.read("ui.py")
        main = self.read("main.py")
        html = self.read("assets/web_background/index.html")
        self.assertIn("def set_deep_idle(self, enabled: bool)", ui)
        self.assertIn("def set_deep_idle_handlers(self, on_enter=None, on_exit=None)", ui)
        self.assertIn("self._deep_idle_tmr.start()", ui)
        self.assertIn("def pause(self):", ui)
        self.assertIn("self._resume_event", ui)
        self.assertIn("sensorium.stop()", main)
        self.assertIn("sensorium.start()", main)
        self.assertIn("brahma_evo._attention_monitor.stop()", main)
        self.assertIn("brahma_evo._attention_monitor.start()", main)
        self.assertIn('getattr(self.ui, "_deep_idle", False)', main)
        self.assertIn("window.setDeepIdle = function(enabled)", html)
        self.assertIn("if (isDeepIdle)", html)
        self.assertIn("renderTimer = null;", html)

    def test_single_instance_runtime_exclusion(self):
        token = f"test-{os.getpid()}"
        lock_path = Path(tempfile.gettempdir()) / f"brahma-singleton-{token}.lock"
        first = SingleInstance(f"Brahma-Evo-Test-{token}", lock_path=lock_path)
        second = SingleInstance(f"Brahma-Evo-Test-{token}", lock_path=lock_path)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            first.release()
            second.release()

        self.assertTrue(second.acquire())
        second.release()

    def test_single_instance_guard_is_real_and_main_enforced(self):
        guard = self.read("core/single_instance.py")
        main = self.read("main.py")
        ui = self.read("ui.py")
        self.assertIn("CreateMutexW", guard)
        self.assertIn("ERROR_ALREADY_EXISTS = 183", guard)
        self.assertIn("def release(self)", guard)
        self.assertIn('SingleInstance("Local\\\\Brahma-Ai-Evo.Singleton.v1")', main)
        self.assertIn("if not guard.acquire()", main)
        self.assertIn("duplicate launch ignored", main)
        self.assertIn("guard.release()", main)
        self.assertIn("QObject", ui)
        self.assertIn("QEvent.Type.WindowActivate", ui)

    def test_voice_session_cannot_run_twice(self):
        main = self.read("main.py")
        self.assertIn("_VOICE_SESSION_GUARD = threading.Lock()", main)
        self.assertIn("_VOICE_SESSION_GUARD.acquire(blocking=False)", main)
        self.assertIn("Voice session already active; duplicate voice start ignored.", main)
        self.assertIn("await self._run_impl()", main)
        self.assertIn("_VOICE_SESSION_GUARD.release()", main)

    def test_jev_hands_integration_contract(self):
        jev = self.read("core/jev_hands.py")
        computer = self.read("actions/computer_control.py")
        main = self.read("main.py")
        self.assertIn('DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"', jev)
        self.assertIn('DEFAULT_MODEL = "typesafe/jev-1.13"', jev)
        self.assertIn('"type": "choice"', jev)
        self.assertIn('"visible_controls"', jev)
        self.assertIn("def try_click(", jev)
        self.assertIn("def run_task(", jev)
        self.assertIn("pyautogui.PAUSE    = 0.01", computer)
        self.assertIn("duration: float = 0.06", computer)
        self.assertIn("duration: float = 0.14", computer)
        self.assertIn("from core.jev_hands import try_click", computer)
        self.assertIn("jev_result = try_click", computer)
        self.assertIn("JEV Hands", main)

    def test_jev_contract_validator_rejects_unknown_choice(self):
        source = self.read("core/jev_hands.py")
        self.assertIn("if choice not in criteria:", source)
        self.assertIn("if set(probabilities) != set(criteria):", source)

    def test_updater_targets_this_repository(self):
        root_updater = self.read("updater.py")
        core_updater = self.read("core/updater.py")
        self.assertIn("https://github.com/dragonballls/Brahma-Ai-Evo.git", root_updater)
        self.assertIn('repo_owner="dragonballls", repo_name="Brahma-Ai-Evo"', core_updater)


if __name__ == "__main__":
    unittest.main()
