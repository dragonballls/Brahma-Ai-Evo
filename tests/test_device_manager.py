from __future__ import annotations

import json
from pathlib import Path

from core.desktop.device_manager import DeviceManager


class _Info:
    def __init__(self, *, installed=False, path=None, python_module=False):
        self.installed = installed
        self.path = path
        self.python_module = python_module


def test_pair_persists_and_renames(tmp_path: Path):
    path = tmp_path / "devices.json"
    manager = DeviceManager(path)
    device = manager.pair(
        name="Living Room PC",
        device_type="pc",
        address="192.168.1.40",
        mac="AA-BB-CC-DD-EE-FF",
        backend="manual",
        capabilities=["web_control", "background_connection"],
    )
    assert device["device_id"].startswith("pc:")
    assert device["status"] == "Standby"
    assert device["wake_method"] == "wol"

    renamed = manager.rename(device["device_id"], "Living Room Desktop")
    assert renamed["name"] == "Living Room Desktop"

    loaded = DeviceManager(path)
    restored = loaded.get(device["device_id"])
    assert restored is not None
    assert restored.name == "Living Room Desktop"
    assert restored.mac == "AA:BB:CC:DD:EE:FF"


def test_android_scan_creates_phone_and_tablet(monkeypatch, tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    monkeypatch.setattr(
        "core.desktop.device_manager.integrations.android_devices",
        lambda: [
            {"serial": "192.168.1.21:5555", "metadata": "product:phone model:Pixel_9"},
            {"serial": "ABC123", "metadata": "product:tablet model:Galaxy_Tab"},
        ],
    )
    monkeypatch.setattr(
        "core.desktop.device_manager.integrations.apple_tv_scan",
        lambda: [],
    )

    devices = manager.scan(include_appletv=False)
    by_serial = {item["serial"]: item for item in devices}
    assert by_serial["192.168.1.21:5555"]["device_type"] == "phone"
    assert by_serial["ABC123"]["device_type"] == "tablet"
    assert by_serial["192.168.1.21:5555"]["status"] == "Connected"
    assert "screen" in by_serial["192.168.1.21:5555"]["capabilities"]


def test_appletv_scan_is_normalized(monkeypatch, tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    monkeypatch.setattr(
        "core.desktop.device_manager.integrations.android_devices",
        lambda: [],
    )
    monkeypatch.setattr(
        "core.desktop.device_manager.integrations.apple_tv_scan",
        lambda: [{
            "name": "Living Room TV",
            "address": "192.168.1.50",
            "identifier": "tv-123",
            "device_info": "AppleTV",
        }],
    )

    devices = manager.scan(include_android=False)
    assert len(devices) == 1
    assert devices[0]["backend"] == "pyatv"
    assert devices[0]["device_type"] == "tv"
    assert "remote" in devices[0]["capabilities"]


def test_modes_and_show_spec(monkeypatch, tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    device = manager.pair(
        name="Phone 1",
        device_type="phone",
        serial="192.168.1.21:5555",
        address="192.168.1.21:5555",
        backend="adb",
    )

    monkeypatch.setattr(
        "core.desktop.device_manager.integrations.info",
        lambda key: _Info(installed=True, path="scrcpy.exe") if key == "scrcpy"
        else _Info(installed=True, path="adb.exe"),
    )

    shown = manager.show_spec(device["device_id"])
    assert shown["ok"] is True
    assert shown["backend"] == "scrcpy"

    hidden = manager.set_mode(device["device_id"], "background")
    assert hidden["mode"] == "background"


def test_android_command_mapping(monkeypatch, tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    device = manager.pair(
        name="Phone 1",
        device_type="phone",
        serial="192.168.1.21:5555",
        address="192.168.1.21:5555",
        backend="adb",
    )

    calls = []

    def fake_run(adb_path, args, timeout=4.0):
        calls.append((adb_path, args))
        return {"ok": True, "args": args}

    monkeypatch.setattr(
        "core.desktop.device_manager.integrations.info",
        lambda key: _Info(installed=True, path="adb.exe"),
    )
    monkeypatch.setattr("core.desktop.device_manager._run_adb", fake_run)

    result = manager.command(device["device_id"], "volume_up")
    assert result["ok"] is True
    assert calls[-1][1][-1] == "KEYCODE_VOLUME_UP"


def test_wake_on_lan_packet(monkeypatch, tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    device = manager.pair(
        name="TV",
        device_type="tv",
        mac="AA:BB:CC:DD:EE:FF",
        backend="manual",
        metadata={"broadcast": "192.168.1.255", "wake_port": 9},
    )

    sent = []

    class FakeSocket:
        def setsockopt(self, *args):
            pass

        def sendto(self, payload, target):
            sent.append((payload, target))

        def close(self):
            pass

    monkeypatch.setattr("core.desktop.device_manager.socket.socket", lambda *args, **kwargs: FakeSocket())

    result = manager.wake(device["device_id"])
    assert result["ok"] is True
    assert result["wake_method"] == "wol"
    assert sent
    packet, target = sent[0]
    assert len(packet) == 102
    assert target == ("192.168.1.255", 9)


def test_forget_is_idempotent(tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    device = manager.pair(name="Phone", device_type="phone", serial="ABC")
    assert manager.forget(device["device_id"]) is True
    assert manager.forget(device["device_id"]) is False
