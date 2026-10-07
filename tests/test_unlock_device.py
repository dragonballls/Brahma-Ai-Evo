import pytest


def test_unlock_device_fails_closed_without_reading_or_sending_pin(monkeypatch):
    import actions.unlock_device as unlock

    called = {"settings": False}

    def fail_settings():
        called["settings"] = True
        raise AssertionError("Deprecated unlock shim must not read PIN settings.")

    monkeypatch.setattr(unlock, "_get_settings", fail_settings, raising=False)
    result = unlock.unlock_device({"target": "phone"})

    assert "not supported" in result.lower()
    assert "no unlock operation was performed" in result.lower()
    assert called["settings"] is False


def test_unlock_device_never_reports_success():
    import actions.unlock_device as unlock

    result = unlock.unlock_device({"target": "phone"})
    assert "successfully" not in result.lower()
    assert "unlock operation was performed" in result.lower()
