from brahma_connect.gateway.device_manager import DeviceManager
from brahma_connect.gateway.models import DeviceRecord


def _manager(tmp_path):
    manager = object.__new__(DeviceManager)
    manager.registry_path = tmp_path / "devices.json"
    manager._lock = __import__("threading").RLock()
    manager._devices = {
        "win_1": DeviceRecord(device_id="win_1", name="Office PC", platform="windows"),
        "android_1": DeviceRecord(device_id="android_1", name="Pixel Phone", platform="android"),
    }
    return manager


def test_device_category_resolution_does_not_match_inside_unrelated_words(tmp_path):
    manager = _manager(tmp_path)
    assert manager.resolve("windowsill") == []
    assert manager.resolve("office pc")[0].device_id == "win_1"
    assert manager.resolve("my phone")[0].device_id == "android_1"
