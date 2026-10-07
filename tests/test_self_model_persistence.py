from __future__ import annotations

import json
import threading

import pytest

from core import self_model
from core.self_model import SelfAwareness


def _instance(tmp_path, monkeypatch):
    path = tmp_path / "self_awareness.json"
    monkeypatch.setattr(self_model, "PATH", path)
    obj = object.__new__(SelfAwareness)
    obj._state = {
        "schema_version": SelfAwareness.SCHEMA_VERSION,
        "created_at": 1.0,
        "last_state": "idle",
        "current_task": "",
        "last_action": "",
        "last_action_status": "",
    }
    return obj, path


def test_self_model_corrupt_state_fails_closed(tmp_path, monkeypatch):
    obj, path = _instance(tmp_path, monkeypatch)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable or corrupted"):
        obj._load()


def test_self_model_save_rejects_symlinked_target(tmp_path, monkeypatch):
    obj, path = _instance(tmp_path, monkeypatch)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="symlink, junction, or reparse"):
        obj.save()


def test_self_model_save_rejects_hardlinked_target(tmp_path, monkeypatch):
    obj, path = _instance(tmp_path, monkeypatch)
    sibling = tmp_path / "self_awareness-hardlink.json"
    try:
        import os
        os.link(path, sibling)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(RuntimeError, match="multiple hard links"):
        obj.save()


def test_self_model_save_verifies_final_state_and_cleans_temp(tmp_path, monkeypatch):
    obj, path = _instance(tmp_path, monkeypatch)
    original_loads = json.loads
    monkeypatch.setattr(
        self_model.json,
        "loads",
        lambda raw: {} if raw else original_loads(raw),
    )

    with pytest.raises(RuntimeError, match="mismatched final state"):
        obj.save()

    assert path.is_file()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_self_model_concurrent_saves_leave_valid_json(tmp_path, monkeypatch):
    obj, path = _instance(tmp_path, monkeypatch)
    errors = []

    def worker(i):
        try:
            obj.set_runtime_state("running", current_task=f"task-{i}", persist=True)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == SelfAwareness.SCHEMA_VERSION
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_self_model_missing_target_is_not_misclassified_as_link(tmp_path):
    missing = tmp_path / "new-self-awareness.json"
    assert self_model._is_link_like(missing) is False
