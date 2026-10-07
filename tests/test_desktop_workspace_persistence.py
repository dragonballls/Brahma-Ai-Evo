from pathlib import Path
import json

import pytest


def test_desktop_workspace_corruption_is_quarantined_and_not_replaced_with_defaults(tmp_path):
    from core.desktop.workspace import WorkspaceStore

    path = tmp_path / "desktop_workspace.json"
    path.write_text("{not-json", encoding="utf-8")
    store = WorkspaceStore(path)

    with pytest.raises(RuntimeError, match="corrupt"):
        store.load()

    backups = list(tmp_path.glob("desktop_workspace.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not-json"
    assert not path.exists()


def test_desktop_workspace_rejects_unknown_future_versions(tmp_path):
    from core.desktop.workspace import WorkspaceStore

    path = tmp_path / "desktop_workspace.json"
    path.write_text(json.dumps({
        "version": 99,
        "workspaces": {"main": {"name": "Main", "windows": []}},
    }), encoding="utf-8")

    store = WorkspaceStore(path)
    with pytest.raises(RuntimeError, match="corrupt"):
        store.load()


def test_desktop_workspace_set_propagates_persistence_failure(tmp_path, monkeypatch):
    from core.desktop.workspace import WorkspaceStore

    store = WorkspaceStore(tmp_path / "desktop_workspace.json")
    monkeypatch.setattr(store, "save", lambda _state: False)

    with pytest.raises(RuntimeError, match="could not be persisted"):
        store.set(desktop_mode=True)
