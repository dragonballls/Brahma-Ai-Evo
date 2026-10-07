from __future__ import annotations

import json
from pathlib import Path

import pytest

from brahma_connect.gateway.device_manager import DeviceManager


def test_invalid_device_record_is_quarantined_instead_of_silently_dropped(tmp_path):
    registry = tmp_path / "devices.json"
    registry.write_text(
        json.dumps(
            {
                "devices": {
                    "good": {
                        "device_id": "good",
                        "name": "Good",
                        "platform": "windows",
                        "secret_hash": "hash",
                    },
                    "bad": "not-an-object",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid record"):
        DeviceManager(registry)

    backups = list(tmp_path.glob("devices.json.corrupt-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["devices"]["bad"] == "not-an-object"


def test_pairing_save_failure_rolls_back_in_memory_state(tmp_path, monkeypatch):
    registry = tmp_path / "devices.json"
    manager = DeviceManager(registry)
    monkeypatch.setattr(manager, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        manager.create_from_pairing(name="Phone", platform="android")

    assert manager.list_devices() == []


def test_device_registry_rejects_symlinked_registry_path(tmp_path):
    target = tmp_path / "real.json"
    target.write_text('{"devices": {}}', encoding="utf-8")
    link = tmp_path / "devices.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return
    from brahma_connect.gateway.device_manager import DeviceManager
    with pytest.raises(RuntimeError, match="must not be a symlink"):
        DeviceManager(link)


def test_duplicate_device_id_records_are_quarantined(tmp_path):
    registry = tmp_path / "devices.json"
    registry.write_text(
        json.dumps(
            {
                "devices": {
                    "device-a": {
                        "device_id": "same",
                        "name": "A",
                        "platform": "android",
                        "secret_hash": "hash-a",
                    },
                    "device-b": {
                        "device_id": "same",
                        "name": "B",
                        "platform": "android",
                        "secret_hash": "hash-b",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate device identities"):
        DeviceManager(registry)

    backups = list(tmp_path.glob("devices.json.corrupt-*"))
    assert len(backups) == 1


def test_mismatched_registry_key_and_embedded_device_id_is_rejected(tmp_path):
    registry = tmp_path / "devices.json"
    registry.write_text(
        json.dumps(
            {
                "devices": {
                    "device-a": {
                        "device_id": "device-b",
                        "name": "A",
                        "platform": "android",
                        "secret_hash": "hash",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid record"):
        DeviceManager(registry)

    assert len(list(tmp_path.glob("devices.json.corrupt-*"))) == 1


def test_device_registry_rejects_symlinked_parent(tmp_path):
    from brahma_connect.gateway.device_manager import DeviceManager

    real_dir = tmp_path / "real-registry"
    real_dir.mkdir()
    link_dir = tmp_path / "registry-link"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        return

    with pytest.raises(RuntimeError, match="parent"):
        DeviceManager(link_dir / "devices.json")
