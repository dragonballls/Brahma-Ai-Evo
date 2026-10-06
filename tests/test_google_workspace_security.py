from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_google_workspace_local_reader_confines_paths_to_brahma_workspace():
    source = (ROOT / "actions" / "google_workspace_mcp.py").read_text(encoding="utf-8")
    start = source.index("def read_file(cls, filename: str) -> str:")
    end = source.index("@classmethod\n    def upload_file", start)
    block = source[start:end]
    assert ".resolve()" in block
    assert "target.relative_to(root)" in block
    assert "Requested file is outside the BrahmaAI workspace." in block