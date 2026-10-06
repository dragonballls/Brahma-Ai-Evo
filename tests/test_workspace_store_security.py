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
