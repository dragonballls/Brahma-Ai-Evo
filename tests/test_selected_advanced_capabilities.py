"""Regression tests for the selected advanced Brahma capability set."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import tempfile
import unittest
from unittest.mock import patch

from core.selected_capabilities import (
    AdvancedAnalyzer,
    GodsEyeExpansion,
    LearningEngine,
    Life360Provider,
    OptimizationLearner,
    PhoneLinkBridge,
    RemoteComputeManager,
    SpatialAudioEngine,
    TimeMachine,
)


class _FakeResponse:
    def __init__(self, value):
        self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.value).encode("utf-8")


class SelectedCapabilitiesTests(unittest.TestCase):
    def test_learning_record_deduplicates_and_feedback_uses_existing_rules(self):
        with tempfile.TemporaryDirectory() as td, patch.object(LearningEngine, "PATH", Path(td) / "learning.json"):
            self.assertTrue(LearningEngine.record("clipboard", outcome="good")["success"])
            self.assertTrue(LearningEngine.record("clipboard", outcome="good")["success"])
            data = json.loads((Path(td) / "learning.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data["events"]), 1)
            with patch("core.learned_rules.LearnedRulesEngine.add_rule", return_value={"success": True}) as add:
                result = LearningEngine.feedback("Prefer concise confirmations.", "formatting")
            add.assert_called_once()
            self.assertTrue(result["success"])

    def test_advanced_analysis_has_trend_and_outlier_detection(self):
        result = AdvancedAnalyzer.analyze([1, 2, 3, 4, 100])
        self.assertTrue(result["success"])
        self.assertGreater(result["slope_per_sample"], 0)
        self.assertIn(100.0, result["outliers"])
        self.assertEqual(result["delta"], 99.0)

    def test_spatial_audio_equal_power_edges(self):
        left, right = SpatialAudioEngine.gains(-90, 1)
        self.assertGreater(left, right)
        left2, right2 = SpatialAudioEngine.gains(90, 1)
        self.assertGreater(right2, left2)
        self.assertEqual(SpatialAudioEngine.gains(0, 1)[0], SpatialAudioEngine.gains(0, 1)[1])
        self.assertEqual(len(SpatialAudioEngine.spatialize([1, 2, 3], azimuth_deg=0)), 3)

    def test_optimization_learner_adapts_sampling_interval_without_changing_system(self):
        with tempfile.TemporaryDirectory() as td, patch.object(OptimizationLearner, "PATH", Path(td) / "optimization.json"), patch("time.monotonic", side_effect=[100.0, 105.0, 131.0]):
            first = OptimizationLearner.observe(cpu_percent=20, memory_percent=40)
            second = OptimizationLearner.observe(cpu_percent=20, memory_percent=40)
            third = OptimizationLearner.observe(cpu_percent=90, memory_percent=92)
            self.assertTrue(first["sampled"])
            self.assertFalse(second["sampled"])
            self.assertTrue(third["sampled"])
            self.assertTrue(third["recommendations"])

    def test_time_machine_round_trip_diff_and_restore(self):
        with tempfile.TemporaryDirectory() as td, patch.object(TimeMachine, "ROOT", Path(td)):
            a = TimeMachine.save("before", {"workspace": {"tab": "home", "size": 1}})
            b = TimeMachine.save("after", {"workspace": {"tab": "coding", "size": 2}})
            self.assertTrue(a["success"] and b["success"])
            diff = TimeMachine.diff("before", "after")
            paths = {item["path"] for item in diff["changes"]}
            self.assertIn("workspace.tab", paths)
            self.assertIn("workspace.size", paths)
            restored = TimeMachine.restore("before")
            self.assertTrue(restored["success"])
            self.assertEqual(restored["state"]["workspace"]["tab"], "home")
            self.assertEqual(restored["restore_mode"], "explicit-apply")

    def test_life360_disabled_does_not_touch_network(self):
        provider = Life360Provider(enabled=False, token="secret")
        with patch("core.selected_capabilities.urlrequest.urlopen") as call:
            self.assertEqual(provider.locations(), [])
        call.assert_not_called()

    def test_life360_uses_bearer_auth_and_allowlist(self):
        provider = Life360Provider(enabled=True, base_url="http://127.0.0.1:8123", token="secret", entity_ids=["device_tracker.alice"])
        states = [
            {"entity_id": "device_tracker.alice", "state": "home", "attributes": {"friendly_name": "Alice", "latitude": 34.1, "longitude": -117.9, "gps_accuracy": 8, "attribution": "Life360", "battery_level": 73}},
            {"entity_id": "device_tracker.bob", "state": "home", "attributes": {"friendly_name": "Bob", "latitude": 35, "longitude": -118, "attribution": "Life360"}},
            {"entity_id": "sensor.no_tracker", "state": "on", "attributes": {"latitude": 35, "longitude": -118, "attribution": "Life360"}},
        ]
        class _FakeOpener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout=None):
                self.calls.append(request)
                return _FakeResponse({"message": "API running."}) if len(self.calls) == 1 else _FakeResponse(states)

        opener = _FakeOpener()
        with patch("core.selected_capabilities.urlrequest.build_opener", return_value=opener):
            rows = provider.locations()
        call = opener        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "Alice")
        self.assertEqual(rows[0]["source"], "life360")
        req = call.calls[1]
        self.assertEqual(req.headers.get("Authorization"), "Bearer secret")

    def test_life360_rejects_public_hosts(self):
        provider = Life360Provider(enabled=True, base_url="https://example.com", token="secret")
        with patch("core.selected_capabilities.urlrequest.urlopen") as call:
            self.assertEqual(provider.locations(), [])
        call.assert_not_called()

    def test_phone_link_bridge_reports_honest_capabilities(self):
        with patch.object(PhoneLinkBridge, "_windows", return_value=False):
            status = PhoneLinkBridge.status()
            self.assertFalse(status["phone_link_installed"])
            self.assertFalse(status["microsoft_client_api"])

    def test_remote_computing_reuses_gateway_route(self):
        class Service:
            def list_devices(self):
                return [{"device_id": "pc-1", "name": "Desk PC", "platform": "windows", "online": True}]
            async def route_command(self, target, action, parameters):
                return {"success": True, "device": target, "action": action, "parameters": parameters}
        with patch.object(RemoteComputeManager, "_service", return_value=Service()):
            pcs = RemoteComputeManager.computers()
            result = RemoteComputeManager.route("Desk PC", "screenshot", {"required_capabilities": ["screen_capture"]})
        self.assertEqual(pcs[0]["device_id"], "pc-1")
        self.assertTrue(result["success"])

    def test_gods_eye_expansion_preserves_existing_globe_contract(self):
        fake_eye = type("FakeEye", (), {"globe_payload": lambda self: {"schema_version": 3, "surface": "gods-eye", "locators": [{"kind": "family"}]}})()
        with patch("core.gods_eye.GodsEye", return_value=fake_eye):
            payload = GodsEyeExpansion.globe_payload()
        self.assertEqual(payload["surface"], "gods-eye")
        self.assertEqual(payload["locators"][0]["kind"], "family")


if __name__ == "__main__":
    unittest.main()


def test_selected_capability_state_quarantine_failure_is_not_silently_replaced():
    source = (ROOT / "core" / "selected_capabilities.py").read_text(encoding="utf-8")
    start = source.index("def _json_load")
    end = source.index("def _json_save", start)
    block = source[start:end]
    assert "could not be quarantined" in block
    assert "raise RuntimeError" in block
    assert "return default" not in block


def test_time_machine_rejects_snapshots_without_real_state():
    source = (ROOT / "core" / "selected_capabilities.py").read_text(encoding="utf-8")
    start = source.index("def restore(cls, selector")
    end = source.index("class RemoteComputeManager", start)
    block = source[start:end]
    assert "snapshot_state = payload.get(\"state\")" in block
    assert "Snapshot is malformed and cannot be restored." in block


def test_life360_marks_malformed_home_assistant_state_as_failure():
    source = (ROOT / "core" / "selected_capabilities.py").read_text(encoding="utf-8")
    assert "Home Assistant returned malformed state data." in source
    assert "self._last_error" in source


def test_selected_capabilities_bound_untrusted_input_sizes():
    source = (ROOT / "core" / "selected_capabilities.py").read_text(encoding="utf-8")
    assert "Analysis input exceeds the 10,000-sample limit." in source
    assert "200,000-sample limit" in source
    assert "Learning context exceeds the safe size limit." in source
    assert "Snapshot exceeds the 2 MB state limit." in source
