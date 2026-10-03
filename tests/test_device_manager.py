from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.desktop.device_manager import DeviceManager


class _Info:
    def __init__(self, *, installed=False, path=None, python_module=False):
        self.installed = installed
        self.path = path
        self.python_module = python_module


class DeviceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "devices.json"
        self.manager = DeviceManager(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pair_persists_and_renames(self):
        device = self.manager.pair(
            name="Living Room PC",
            device_type="pc",
            address="192.168.1.40",
            mac="AA-BB-CC-DD-EE-FF",
            backend="manual",
            capabilities=["web_control", "background_connection"],
        )
        self.assertTrue(device["device_id"].startswith("pc:"))
        self.assertEqual(device["status"], "Standby")
        self.assertEqual(device["wake_method"], "wol")

        renamed = self.manager.rename(device["device_id"], "Living Room Desktop")
        self.assertEqual(renamed["name"], "Living Room Desktop")

        loaded = DeviceManager(self.path)
        restored = loaded.get(device["device_id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored.name, "Living Room Desktop")
        self.assertEqual(restored.mac, "AA:BB:CC:DD:EE:FF")

    @patch("core.desktop.device_manager.integrations.android_devices")
    @patch("core.desktop.device_manager.integrations.apple_tv_scan")
    def test_android_scan_creates_phone_and_tablet(self, appletv_scan, android_devices):
        android_devices.return_value = [
            {"serial": "192.168.1.21:5555", "metadata": "product:phone model:Pixel_9"},
            {"serial": "ABC123", "metadata": "product:tablet model:Galaxy_Tab"},
        ]
        appletv_scan.return_value = []

        devices = self.manager.scan(include_appletv=False)
        by_serial = {item["serial"]: item for item in devices}
        self.assertEqual(by_serial["192.168.1.21:5555"]["device_type"], "phone")
        self.assertEqual(by_serial["ABC123"]["device_type"], "tablet")
        self.assertEqual(by_serial["192.168.1.21:5555"]["status"], "Connected")
        self.assertIn("screen", by_serial["192.168.1.21:5555"]["capabilities"])

    @patch("core.desktop.device_manager.integrations.android_devices")
    @patch("core.desktop.device_manager.integrations.apple_tv_scan")
    def test_appletv_scan_is_normalized(self, appletv_scan, android_devices):
        android_devices.return_value = []
        appletv_scan.return_value = [{
            "name": "Living Room TV",
            "address": "192.168.1.50",
            "identifier": "tv-123",
            "device_info": "AppleTV",
        }]

        devices = self.manager.scan(include_android=False)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["backend"], "pyatv")
        self.assertEqual(devices[0]["device_type"], "tv")
        self.assertIn("remote", devices[0]["capabilities"])

    @patch("core.desktop.device_manager.integrations.info")
    def test_modes_and_show_spec(self, info):
        device = self.manager.pair(
            name="Phone 1",
            device_type="phone",
            serial="192.168.1.21:5555",
            address="192.168.1.21:5555",
            backend="adb",
        )

        def info_for(key):
            return _Info(installed=True, path="scrcpy.exe" if key == "scrcpy" else "adb.exe")

        info.side_effect = info_for
        shown = self.manager.show_spec(device["device_id"])
        self.assertTrue(shown["ok"])
        self.assertEqual(shown["backend"], "scrcpy")

        hidden = self.manager.set_mode(device["device_id"], "background")
        self.assertEqual(hidden["mode"], "background")

    @patch("core.desktop.device_manager._run_adb")
    @patch("core.desktop.device_manager.integrations.info")
    def test_android_command_mapping(self, info, run_adb):
        device = self.manager.pair(
            name="Phone 1",
            device_type="phone",
            serial="192.168.1.21:5555",
            address="192.168.1.21:5555",
            backend="adb",
        )
        info.return_value = _Info(installed=True, path="adb.exe")
        run_adb.return_value = {"ok": True}

        result = self.manager.command(device["device_id"], "volume_up")
        self.assertTrue(result["ok"])
        args = run_adb.call_args.args[1]
        self.assertEqual(args[-1], "KEYCODE_VOLUME_UP")

    @patch("core.desktop.device_manager.socket.socket")
    def test_wake_on_lan_packet(self, socket_ctor):
        device = self.manager.pair(
            name="TV",
            device_type="tv",
            mac="AA:BB:CC:DD:EE:FF",
            backend="manual",
            metadata={"broadcast": "192.168.1.255", "wake_port": 9},
        )

        sock = socket_ctor.return_value
        result = self.manager.wake(device["device_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["wake_method"], "wol")
        payload, target = sock.sendto.call_args.args
        self.assertEqual(len(payload), 102)
        self.assertEqual(target, ("192.168.1.255", 9))

    @patch("core.desktop.device_manager.integrations.bluetooth_devices")
    def test_bluetooth_scan_creates_standby_device(self, bluetooth_devices):
        bluetooth_devices.return_value = [{
            "name": "BLE Controller",
            "address": "11:22:33:44:55:66",
            "rssi": -48,
            "service_uuids": ["00001812-0000-1000-8000-00805f9b34fb"],
            "local_name": "BLE Controller",
        }]
        devices = self.manager.scan(include_android=False, include_appletv=False)
        bluetooth = next(item for item in devices if item["backend"] == "bluetooth_le")
        self.assertEqual(bluetooth["status"], "Standby")
        self.assertEqual(bluetooth["device_type"], "controller")
        self.assertIn("gatt", bluetooth["capabilities"])
        self.assertEqual(bluetooth["metadata"]["bluetooth"]["rssi"], -48)

    @patch("core.desktop.device_manager.integrations.bluetooth_services")
    @patch("core.desktop.device_manager.integrations.bluetooth_devices")
    def test_pair_bluetooth_persists_pairing_state(self, bluetooth_devices, bluetooth_services):
        bluetooth_devices.return_value = [{
            "name": "BLE Keyboard",
            "address": "AA:BB:CC:DD:EE:FF",
            "rssi": -35,
            "service_uuids": [],
        }]
        bluetooth_services.return_value = {"ok": True, "services": []}
        with patch(
            "core.desktop.device_manager.integrations.bluetooth_pair",
            return_value={"ok": True, "address": "AA:BB:CC:DD:EE:FF", "name": "BLE Keyboard"},
        ):
            result = self.manager.pair_bluetooth("AA:BB:CC:DD:EE:FF")
        self.assertTrue(result["ok"])
        self.assertTrue(result["device"]["metadata"]["paired"])
        self.assertEqual(result["device"]["device_type"], "keyboard")

    @patch("core.desktop.device_manager.integrations.bluetooth_gatt_command")
    def test_bluetooth_gatt_write_forwards_payload(self, bluetooth_gatt_command):
        bluetooth_gatt_command.return_value = {"ok": True, "bytes_written": 2}
        device = self.manager.pair(
            name="BLE Peripheral",
            device_type="device",
            address="11:22:33:44:55:66",
            serial="11:22:33:44:55:66",
            backend="bluetooth_le",
            capabilities=["bluetooth_le", "gatt", "pairing"],
        )
        result = self.manager.command(
            device["device_id"],
            "write",
            {
                "characteristic_uuid": "0000abcd-0000-1000-8000-00805f9b34fb",
                "data": "0102",
                "hex_data": True,
                "response": True,
            },
        )
        self.assertTrue(result["ok"])
        bluetooth_gatt_command.assert_called_once_with(
            "11:22:33:44:55:66",
            "write",
            "0000abcd-0000-1000-8000-00805f9b34fb",
            data="0102",
            hex_data=True,
            response=True,
        )

    def test_forget_is_idempotent(self):
        device = self.manager.pair(name="Phone", device_type="phone", serial="ABC")
        self.assertTrue(self.manager.forget(device["device_id"]))
        self.assertFalse(self.manager.forget(device["device_id"]))


if __name__ == "__main__":
    unittest.main()
