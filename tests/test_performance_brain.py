from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from core.desktop.performance_brain import (
    ApplicationProfile,
    PerformanceBrain,
    classify_application,
)


class PerformanceBrainTests(TestCase):
    def test_application_classification_is_deterministic(self):
        self.assertEqual(classify_application("javaw.exe", "Minecraft"), "game")
        self.assertEqual(classify_application("chrome.exe", "Google"), "browser")
        self.assertEqual(classify_application("code.exe", "project.py"), "development")
        self.assertEqual(classify_application("discord.exe", "Discord"), "communication")
        self.assertEqual(classify_application("notepad.exe", "notes"), "generic")

    def test_unknown_profile_is_observe_only(self):
        brain = PerformanceBrain(path=Path(tempfile.mkdtemp()) / "profiles.json")
        result = brain.recommend_background_action(
            None,
            cpu=95,
            memory_mb=9000,
            minimized=True,
            system_memory_percent=95,
            game_active=False,
        )
        self.assertEqual(result["action"], "observe")
        self.assertEqual(result["confidence"], 0.0)

    def test_confidence_grows_without_becoming_sudden(self):
        brain = PerformanceBrain(path=Path(tempfile.mkdtemp()) / "profiles.json")
        for _ in range(8):
            brain.observe_process(
                exe="chrome.exe",
                title="Google",
                cpu=12,
                memory_mb=800,
                read_mb_s=1,
                write_mb_s=1,
                foreground=False,
            )
        profile = brain.profile_for("chrome.exe")
        self.assertIsNotNone(profile)
        self.assertGreaterEqual(brain.confidence(profile), 0.20)
        self.assertLessEqual(brain.confidence(profile), 1.0)

    def test_high_deviation_can_trigger_background_priority_action(self):
        brain = PerformanceBrain(path=Path(tempfile.mkdtemp()) / "profiles.json")
        profile = ApplicationProfile(
            exe="chrome.exe",
            role="browser",
            samples=40,
            ewma_cpu=5,
            ewma_memory_mb=500,
            ewma_read_mb_s=1,
            ewma_write_mb_s=1,
            ewma_active_ratio=0.1,
        )
        result = brain.recommend_background_action(
            profile,
            cpu=20,
            memory_mb=700,
            minimized=False,
            system_memory_percent=80,
            game_active=False,
        )
        self.assertEqual(result["action"], "lower_priority")

    def test_memory_trim_requires_minimized_app_and_high_pressure(self):
        brain = PerformanceBrain(path=Path(tempfile.mkdtemp()) / "profiles.json")
        profile = ApplicationProfile(
            exe="chrome.exe",
            role="browser",
            samples=40,
            ewma_cpu=5,
            ewma_memory_mb=500,
        )
        result = brain.recommend_background_action(
            profile,
            cpu=1,
            memory_mb=750,
            minimized=True,
            system_memory_percent=92,
            game_active=False,
        )
        self.assertEqual(result["action"], "trim_memory")
