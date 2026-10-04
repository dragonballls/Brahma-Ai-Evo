from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class AudioQualityGuardTests(unittest.TestCase):
    def test_bluetooth_endpoint_matching(self):
        from core.audio_devices import _same_bluetooth_endpoint
        self.assertTrue(
            _same_bluetooth_endpoint(
                "WH-1000XM5 Hands-Free AG Audio",
                "WH-1000XM5 Stereo",
            )
        )
        self.assertTrue(
            _same_bluetooth_endpoint(
                "AirPods Microphone",
                "AirPods Stereo",
            )
        )
        self.assertFalse(
            _same_bluetooth_endpoint(
                "Built-in Microphone",
                "WH-1000XM5 Stereo",
            )
        )

    def test_bluetooth_output_does_not_override_separate_microphone(self):
        from core.audio_devices import resolve_voice_input
        with patch("core.audio_devices.resolve", return_value=7):
            with patch("core.audio_devices._device_name", return_value="Built-in Microphone"):
                selected, rerouted = resolve_voice_input(
                    "Built-in Microphone",
                    "WH-1000XM5 Stereo",
                )
        self.assertEqual(selected, 7)
        self.assertFalse(rerouted)

    def test_bluetooth_headset_microphone_is_rerouted_when_alternative_exists(self):
        from core import audio_devices

        fake_devices = [
            {
                "name": "WH-1000XM5 Hands-Free AG Audio",
                "max_input_channels": 1,
                "hostapi": 0,
            },
            {
                "name": "Built-in Microphone Array",
                "max_input_channels": 2,
                "hostapi": 0,
            },
        ]

        with patch.object(audio_devices, "resolve", return_value=4),              patch.object(audio_devices, "_device_name", return_value="WH-1000XM5 Hands-Free AG Audio"),              patch("sounddevice.query_devices", return_value=fake_devices),              patch("sounddevice.query_hostapis", return_value=[{"name": "DirectSound"}]),              patch.object(audio_devices, "_usable", return_value=True):
            selected, rerouted = audio_devices.resolve_voice_input(
                "WH-1000XM5 Hands-Free AG Audio",
                "WH-1000XM5 Stereo",
            )

        self.assertEqual(selected, 1)
        self.assertTrue(rerouted)

    def test_main_voice_path_uses_media_preserving_resolver(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("resolve_voice_input(", source)
        self.assertIn("_spk_name_for_voice", source)
        self.assertIn("preserve Bluetooth media quality", source)


if __name__ == "__main__":
    unittest.main()
