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
