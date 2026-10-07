import sqlite3

import pytest


def test_settings_read_fails_closed_on_corrupt_json(tmp_path, monkeypatch):
    import memory.config_manager as config_manager

    settings = tmp_path / "app_settings.json"
    settings.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config_manager, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    config_manager._SETTINGS_CACHE = None

    with pytest.raises(RuntimeError, match="corrupted"):
        config_manager.load_settings()


def test_smart_home_credential_decryption_fails_closed():
    from smart_home.storage import CredentialVault

    vault = CredentialVault()
    with pytest.raises(RuntimeError, match="corrupt or cannot be decrypted"):
        vault.decrypt_json("not-valid-fernet-payload")


def test_smart_home_corrupt_traits_state_does_not_become_empty_defaults(tmp_path):
    from smart_home.storage import SmartHomeStorage

    db = tmp_path / "smart_home.sqlite3"
    storage = SmartHomeStorage(db_path=db)
    account_id = storage.save_provider_account("test", "account", {"token": "secret"})
    storage.save_devices(
        account_id,
        "test",
        [{"external_id": "device-1", "name": "Lamp"}],
    )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE devices SET traits_json = ? WHERE external_id = ?",
            ("{broken", "device-1"),
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="corrupt trait state"):
        storage.list_devices()

def test_settings_save_rejects_symlink(tmp_path, monkeypatch):
    import memory.config_manager as config_manager
    import os
    settings = tmp_path / "app_settings.json"
    monkeypatch.setattr(config_manager, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    config_manager._SETTINGS_CACHE = None
    config_manager.save_settings({"theme": "dark"})
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return
    monkeypatch.setattr(config_manager, "SETTINGS_FILE", link)
    with pytest.raises(RuntimeError, match="symlink"):
        config_manager.save_settings({"theme": "light"})

def test_memory_sessions_invalid_schema_fails_closed(tmp_path, monkeypatch):
    import memory.memory_manager as mm
    path = tmp_path / "long_term.json"
    path.write_text('{"sessions":"corrupt"}', encoding="utf-8")
    monkeypatch.setattr(mm, "MEMORY_PATH", path)
    with pytest.raises(RuntimeError, match="malformed sessions"):
        mm.load_memory()


def test_chat_history_invalid_schema_is_not_treated_as_missing(tmp_path, monkeypatch):
    import memory.memory_manager as mm
    path = tmp_path / "chat_history.json"
    path.write_text('{"wrong": true}', encoding="utf-8")
    monkeypatch.setattr(mm, "CHAT_HISTORY_PATH", path)
    with pytest.raises(RuntimeError, match="invalid schema"):
        mm.load_chat_history()


def test_pop_last_session_propagates_write_failure(tmp_path, monkeypatch):
    import memory.memory_manager as mm
    path = tmp_path / "long_term.json"
    path.write_text('{"sessions":[{"summary":"hello"}]}', encoding="utf-8")
    monkeypatch.setattr(mm, "MEMORY_PATH", path)
    monkeypatch.setattr(mm, "_atomic_write_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        mm.pop_last_session()
