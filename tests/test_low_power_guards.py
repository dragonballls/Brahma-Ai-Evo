from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        self.assertIn("powerPreference: 'low-power'", html)
        self.assertIn("Math.min(window.devicePixelRatio || 1, 1.25)", html)
        self.assertNotIn("requestAnimationFrame(renderLoop)", html)
        self.assertIn("window.setTimeout(renderLoop", html)

    def test_background_monitor_is_lazy(self):
        source = self.read("actions/background_monitor.py")
        self.assertIn("_ensure_monitor_thread()", source)
        self.assertIn("def add_monitor(", source)
        self.assertIn("if not _monitors:", source)

    def test_passive_watchers_back_off(self):
        sensorium = self.read("core/sensorium.py")
        attention = self.read("actions/attention_monitor.py")
        clipboard = self.read("core/clipboard_sentry.py")
        self.assertIn("poll_interval: float = 10.0", sensorium)
        self.assertIn("sleep_for = 30.0 if self.user_idle_seconds >= 120.0 else self.poll_interval", sensorium)
        self.assertIn("interval: float = 5.0", attention)
        self.assertIn("time.sleep(2.5)", clipboard)

    def test_clipboard_ai_is_opt_in(self):
        main = self.read("main.py")
        self.assertIn('get_setting("clipboard_auto_comment_enabled", False)', main)

    def test_updater_targets_this_repository(self):
        root_updater = self.read("updater.py")
        core_updater = self.read("core/updater.py")
        self.assertIn("https://github.com/dragonballls/Brahma-Ai-Evo.git", root_updater)
        self.assertIn('repo_owner="dragonballls", repo_name="Brahma-Ai-Evo"', core_updater)


if __name__ == "__main__":
    unittest.main()
