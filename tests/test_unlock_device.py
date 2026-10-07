import pytest



def test_unlock_device_uses_canonical_settings_and_propagates_corruption(monkeypatch):
    import actions.unlock_device as unlock

    monkeypatch.setattr(
        "memory.config_manager.load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("settings corrupt")),
    )
    with pytest.raises(RuntimeError, match="settings corrupt"):
        unlock._get_settings()


def test_unlock_device_rejects_malformed_device_pin_store(monkeypatch):
    import actions.unlock_device as unlock

    monkeypatch.setattr(unlock, "_get_settings", lambda: {"device_pins": []})
    result = unlock.unlock_device({"target": "phone"})
    assert "PIN settings are malformed" in result
