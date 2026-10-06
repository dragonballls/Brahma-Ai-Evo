from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_archive_extraction_rejects_traversal_links_and_size_abuse():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _safe_archive_target" in source
    assert "path-traversal member" in source
    assert "Archive symlink members are not allowed." in source
    assert "Archive link members are not allowed." in source
    assert "MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024" in source
    assert "def preflight(members)" in source
    assert 'target.open("xb")' in source
    assert "Archive would overwrite an existing path" in source


def test_video_transcription_uses_exclusive_temp_creation():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "tempfile.mkstemp" in source
    assert "tempfile.mktemp" not in source
