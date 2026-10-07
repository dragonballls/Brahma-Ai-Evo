from __future__ import annotations

import json
import os

import pytest

import core.identity as identity_module
from core.identity import IdentityService


def _service(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    bundled = tmp_path / "bundled-identity.json"
    monkeypatch.setattr(identity_module, "IDENTITY_PATH", path)
    service = IdentityService()
    service.bundled_config_file = bundled
    return service, path, bundled


def test_corrupt_persisted_identity_fails_closed_instead_of_using_defaults(tmp_path, monkeypatch):
    service, path, _ = _service(tmp_path, monkeypatch)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="corrupt or unreadable"):
        service.load()


def test_corrupt_persisted_identity_does_not_fall_back_to_bundled_template(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled-identity.json"
    bundled.write_text(
        json.dumps({"owner": {"name": "Bundled Owner"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(identity_module, "IDENTITY_PATH", tmp_path / "identity.json")
    service = IdentityService()
    service.bundled_config_file = bundled
    service.config_file.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="corrupt or unreadable"):
        service.load()


def test_identity_save_rejects_symlinked_target(tmp_path, monkeypatch):
    service, path, _ = _service(tmp_path, monkeypatch)
    path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        service.save()


def test_identity_save_rejects_hardlinked_target(tmp_path, monkeypatch):
    service, path, _ = _service(tmp_path, monkeypatch)
    sibling = tmp_path / "identity-hardlink.json"
    try:
        os.link(path, sibling)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="multiple hard links"):
        service.save()


def test_identity_save_detects_mismatched_final_state_and_cleans_temp(tmp_path, monkeypatch):
    service, path, _ = _service(tmp_path, monkeypatch)
    real_loads = json.loads
    monkeypatch.setattr(
        identity_module.json,
        "loads",
        lambda raw: {} if raw != "" else real_loads(raw),
    )

    with pytest.raises(RuntimeError, match="mismatched final state"):
        service.save()

    assert path.is_file()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
