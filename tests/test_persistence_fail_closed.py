from pathlib import Path
import json
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


def test_corrupt_memory_is_preserved_without_renaming_the_original(tmp_path, monkeypatch):
    import memory.memory_manager as mm

    path = tmp_path / "long_term.json"
    raw = "{not valid json"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(mm, "MEMORY_PATH", path)

    with pytest.raises(RuntimeError, match="refusing to use empty defaults"):
        mm.load_memory()

    assert path.exists()
    assert path.read_text(encoding="utf-8") == raw
    copies = list(tmp_path.glob("long_term.json.corrupt-*"))
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == raw


def test_screen_processor_does_not_overwrite_corrupt_camera_config(tmp_path, monkeypatch):
    from actions import screen_processor as sp

    path = tmp_path / "api_config.json"
    raw = "{broken"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(sp, "API_CONFIG_PATH", path)

    with pytest.raises(RuntimeError, match="corrupted"):
        sp._get_camera_index()

    assert path.read_text(encoding="utf-8") == raw


def test_screen_processor_rejects_invalid_camera_config_schema(tmp_path, monkeypatch):
    from actions import screen_processor as sp

    path = tmp_path / "api_config.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(sp, "API_CONFIG_PATH", path)

    with pytest.raises(RuntimeError, match="invalid root schema"):
        sp._get_camera_index()

    assert path.read_text(encoding="utf-8") == "[]"


def test_screen_processor_rejects_invalid_camera_index_without_autodetection(tmp_path, monkeypatch):
    from actions import screen_processor as sp

    path = tmp_path / "api_config.json"
    path.write_text('{"camera_index": "not-an-int"}', encoding="utf-8")
    monkeypatch.setattr(sp, "API_CONFIG_PATH", path)

    camera_calls = []

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            camera_calls.append((args, kwargs))

    monkeypatch.setattr(sp.cv2, "VideoCapture", FakeCapture)

    with pytest.raises(RuntimeError, match="invalid camera_index"):
        sp._get_camera_index()

    assert camera_calls == []
    assert path.read_text(encoding="utf-8") == '{"camera_index": "not-an-int"}'


def test_youtube_token_atomic_replace_does_not_follow_existing_symlink(tmp_path):
    from core import creator_publish

    outside = tmp_path / "outside-token.json"
    outside.write_text("do-not-overwrite", encoding="utf-8")
    target = tmp_path / "youtube_token.json"
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink support unavailable")

    class Credentials:
        def to_json(self):
            return '{"token":"new-secret"}'

    creator_publish._save_token(target, Credentials())
    assert target.read_text(encoding="utf-8") == '{"token":"new-secret"}'
    assert outside.read_text(encoding="utf-8") == "do-not-overwrite"


def test_smart_home_database_rejects_hard_linked_wal(tmp_path):
    import os
    from smart_home import storage

    db = tmp_path / "smart_home.sqlite3"
    sidecar = Path(f"{db}-wal")
    outside = tmp_path / "outside-wal"
    outside.write_bytes(b"wal")
    try:
        os.link(outside, sidecar)
    except (OSError, NotImplementedError):
        pytest.skip("Hard-link support unavailable")

    with pytest.raises(RuntimeError, match="multiple hard links"):
        storage._validate_db_path(db)


def test_workspace_database_rejects_hard_linked_shm(tmp_path):
    import os
    import workspace_store

    db = tmp_path / "workspace.sqlite3"
    sidecar = Path(f"{db}-shm")
    outside = tmp_path / "outside-shm"
    outside.write_bytes(b"shm")
    try:
        os.link(outside, sidecar)
    except (OSError, NotImplementedError):
        pytest.skip("Hard-link support unavailable")

    with pytest.raises(RuntimeError, match="multiple hard links"):
        workspace_store._validate_store_path(db)


def test_memory_atomic_writer_uses_exclusive_temp_creation(monkeypatch, tmp_path):
    from memory import memory_manager as mm
    seen = {}
    real_open = mm.os.open

    def checked_open(path, flags, mode=0o666):
        seen["flags"] = flags
        return real_open(path, flags, mode)

    target = tmp_path / "memory.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", target)
    monkeypatch.setattr(mm.os, "open", checked_open)
    mm._atomic_write_json(target, {"ok": True})
    assert seen["flags"] & mm.os.O_EXCL
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_config_atomic_writer_uses_exclusive_temp_creation(monkeypatch, tmp_path):
    from memory import config_manager as cm
    seen_flags = []
    real_open = cm.os.open

    def checked_open(path, flags, mode=0o666):
        seen_flags.append(flags)
        return real_open(path, flags, mode)

    target = tmp_path / "settings.json"
    monkeypatch.setattr(cm, "SETTINGS_FILE", target)
    monkeypatch.setattr(cm.os, "open", checked_open)
    monkeypatch.setattr(cm, "_SETTINGS_CACHE", None)
    cm.save_settings({"race_test": "ok"})
    assert any(flags & cm.os.O_EXCL for flags in seen_flags)
    assert target.exists()


def test_settings_safe_read_refuses_symlink_without_following_it(tmp_path, monkeypatch):
    import os
    import memory.config_manager as config_manager

    real = tmp_path / "real.json"
    real.write_text('{"secret":"keep"}', encoding="utf-8")
    link = tmp_path / "app_settings.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink support unavailable")

    monkeypatch.setattr(config_manager, "SETTINGS_FILE", link)
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    config_manager._SETTINGS_CACHE = None

    with pytest.raises(RuntimeError, match="symlink"):
        config_manager.load_settings()
    assert real.read_text(encoding="utf-8") == '{"secret":"keep"}'


def test_memory_safe_read_refuses_symlink_without_following_it(tmp_path, monkeypatch):
    import os
    import memory.memory_manager as mm

    real = tmp_path / "real.json"
    real.write_text('{"identity":{"name":{"value":"keep"}}}', encoding="utf-8")
    link = tmp_path / "long_term.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink support unavailable")

    monkeypatch.setattr(mm, "MEMORY_PATH", link)
    with pytest.raises(RuntimeError, match="symlink"):
        mm.load_memory()
    assert real.read_text(encoding="utf-8") == '{"identity":{"name":{"value":"keep"}}}'


def test_selected_capability_state_refuses_symlink_read(tmp_path):
    import os
    from core.selected_capabilities import _json_load

    real = tmp_path / "real.json"
    real.write_text('{"events":[]}', encoding="utf-8")
    link = tmp_path / "state.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink support unavailable")

    with pytest.raises(RuntimeError, match="symlink"):
        _json_load(link, {"events": []})
    assert real.read_text(encoding="utf-8") == '{"events":[]}'


def test_model_performance_state_refuses_symlink_read(tmp_path, monkeypatch):
    import os
    import core.model_performance as mp

    real = tmp_path / "real.json"
    real.write_text('{"schema_version":1,"models":{}}', encoding="utf-8")
    link = tmp_path / "model_performance.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink support unavailable")

    monkeypatch.setattr(mp, "_PATH", link)
    with pytest.raises((RuntimeError, OSError)):
        mp._load()
    assert real.read_text(encoding="utf-8") == '{"schema_version":1,"models":{}}'
