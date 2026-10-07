import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workspace_store_redacts_secrets_at_persistence_boundaries():
    source = (ROOT / "workspace_store.py").read_text(encoding="utf-8")
    assert "_SECRET_RE" in source
    assert "_TOKEN_RE" in source
    assert "_redact_persisted" in source
    assert "_redact_secret_text(_clean_text(content))" in source
    assert "self._ingest_memory(safe_content" in source
    assert "safe_value = _redact_secret_text" in source


def test_workspace_store_does_not_hide_corrupt_attachment_metadata():
    source = (ROOT / "workspace_store.py").read_text(encoding="utf-8")
    assert "Conversation attachment metadata is corrupted." in source
    assert "Conversation attachment metadata has an invalid schema." in source
    assert 'return "[]"' not in source[source.index("def _serialize_attachments"):source.index("class WorkspaceStore")]


def test_workspace_store_rejects_symlinked_database(tmp_path):
    from workspace_store import WorkspaceStore

    real = tmp_path / "real.sqlite3"
    link = tmp_path / "workspace_store.sqlite3"
    real.write_bytes(b"not-a-sqlite-db")
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        return

    try:
        WorkspaceStore(link)
    except RuntimeError as exc:
        assert "symlink" in str(exc).lower()
    else:
        raise AssertionError("symlinked workspace database must be rejected")


def test_workspace_store_rejects_hardlinked_database(tmp_path):
    from workspace_store import WorkspaceStore

    first = tmp_path / "workspace_store.sqlite3"
    second = tmp_path / "alias.sqlite3"
    first.touch()
    try:
        os.link(first, second)
    except (OSError, NotImplementedError):
        return

    try:
        WorkspaceStore(first)
    except RuntimeError as exc:
        assert "hard link" in str(exc).lower()
    else:
        raise AssertionError("hardlinked workspace database must be rejected")
