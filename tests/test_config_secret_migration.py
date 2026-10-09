import base64
import json
import os
import sys
import types
from pathlib import Path

import pytest
import config as config_module


def _use_mocked_windows_dpapi(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(config_module, "API_CONFIG_PATH", path)
    monkeypatch.setattr(config_module.platform, "system", lambda: "Windows")
    fake = types.ModuleType("win32crypt")
    def protect(data, *args, **kwargs):
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return (None, b"test-dpapi:" + raw)
    def unprotect(blob, *args, **kwargs):
        return (None, bytes(blob).removeprefix(b"test-dpapi:"))
    fake.CryptProtectData = protect
    fake.CryptUnprotectData = unprotect
    monkeypatch.setitem(sys.modules, "win32crypt", fake)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_legacy_windows_keys_migrate_and_unrelated_settings_survive(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    _use_mocked_windows_dpapi(monkeypatch, path)
    legacy_key, second_key = "synthetic-legacy-gemini-key", "synthetic-legacy-router-key"
    _write(path, {"gemini_api_key": legacy_key, "openrouter_api_key": second_key, "theme": "dark", "os_system": "windows", "feature_flags": {"voice": True}})
    loaded = config_module.get_config()
    assert loaded.get("gemini_api_key") == legacy_key, "first legacy key did not round-trip"
    assert loaded.get("openrouter_api_key") == second_key, "second legacy key did not round-trip"
    assert loaded.get("theme") == "dark", "unrelated setting was lost"
    assert loaded.get("feature_flags") == {"voice": True}, "nested settings were lost"
    raw = path.read_text(encoding="utf-8")
    stored = json.loads(raw)
    assert stored["gemini_api_key"].startswith("dpapi:"), "Windows key was not protected"
    assert stored["openrouter_api_key"].startswith("dpapi:"), "second Windows key was not protected"
    assert legacy_key not in raw and second_key not in raw, "legacy keys remained plaintext on disk"


def test_already_protected_windows_key_is_read_without_rewriting(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    _use_mocked_windows_dpapi(monkeypatch, path)
    key = "synthetic-already-protected-key"
    protected = "dpapi:" + base64.b64encode(b"test-dpapi:" + key.encode("utf-8")).decode("ascii")
    _write(path, {"gemini_api_key": protected, "theme": "dark"})
    original = path.read_bytes()
    loaded = config_module.get_config()
    assert loaded.get("gemini_api_key") == key, "protected key did not decode"
    assert path.read_bytes() == original, "loading a protected config rewrote the file"


def test_storage_encoder_does_not_rewrap_existing_protected_prefixes():
    portable, dpapi = "portable:v1:synthetic-encrypted-payload", "dpapi:synthetic-protected-payload"
    encoded = config_module._encode_config_for_storage({"gemini_api_key": portable, "openrouter_api_key": dpapi})
    assert encoded["gemini_api_key"] == portable, "portable value was rewrapped"
    assert encoded["openrouter_api_key"] == dpapi, "DPAPI value was rewrapped"


def test_missing_configuration_returns_defaults_without_creating_file(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "api_keys.json"
    _use_mocked_windows_dpapi(monkeypatch, path)
    assert config_module.get_config().get("os_system") == "windows", "Windows default missing"
    assert not path.exists(), "loading created a missing configuration file"


def test_malformed_configuration_is_preserved_without_secret_output(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    _use_mocked_windows_dpapi(monkeypatch, path)
    malformed = b'{"gemini_api_key":'
    path.write_bytes(malformed)
    assert config_module.get_config().get("os_system") == "windows", "malformed config did not fall back"
    assert path.read_bytes() == malformed, "malformed config was overwritten"


def test_failed_atomic_migration_preserves_original_config_and_cleans_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    _use_mocked_windows_dpapi(monkeypatch, path)
    original_data = {"gemini_api_key": "synthetic-legacy-key", "theme": "dark"}
    _write(path, original_data)
    original_bytes, temp_path, real_replace = path.read_bytes(), path.with_suffix(".json.tmp"), Path.replace
    def fail_target_replace(self, target):
        if self == temp_path and Path(target) == path:
            raise OSError("synthetic atomic replacement failure")
        return real_replace(self, target)
    monkeypatch.setattr(Path, "replace", fail_target_replace)
    loaded = config_module.get_config()
    assert loaded.get("gemini_api_key") == original_data["gemini_api_key"], "failed migration lost in-memory key"
    assert path.read_bytes() == original_bytes, "failed migration changed the original file"
    assert not temp_path.exists(), "failed migration left a temporary file"


def test_repeated_load_after_migration_does_not_rewrite_config(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    _use_mocked_windows_dpapi(monkeypatch, path)
    _write(path, {"gemini_api_key": "synthetic-repeat-key", "theme": "dark"})
    first, protected_bytes = config_module.get_config(), path.read_bytes()
    second = config_module.get_config()
    assert first.get("gemini_api_key") == "synthetic-repeat-key", "first load lost key"
    assert second.get("gemini_api_key") == "synthetic-repeat-key", "second load lost key"
    assert path.read_bytes() == protected_bytes, "repeated loading rewrote protected configuration"


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows DPAPI")
def test_real_windows_dpapi_migrates_legacy_key_and_round_trips(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(config_module, "API_CONFIG_PATH", path)
    monkeypatch.setattr(config_module.platform, "system", lambda: "Windows")
    _write(path, {"gemini_api_key": "synthetic-real-dpapi-key", "theme": "dark"})
    loaded = config_module.get_config()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["gemini_api_key"].startswith("dpapi:"), "real Windows DPAPI migration did not run"
    assert loaded.get("gemini_api_key") == "synthetic-real-dpapi-key", "real Windows DPAPI round-trip failed"
    assert loaded.get("theme") == "dark", "real DPAPI migration lost unrelated settings"
