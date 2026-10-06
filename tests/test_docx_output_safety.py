from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docx_explicit_output_paths_are_home_confined_and_non_overwriting_by_default():
    source = (ROOT / "actions" / "docx_tools.py").read_text(encoding="utf-8")
    assert "DOCX output path must remain inside the user's home directory." in source
    assert "DOCX output path may not contain symlinked components." in source
    assert "Refusing to overwrite existing DOCX without overwrite=True" in source
    assert "overwrite = bool(params.get(" in source


def test_docx_open_does_not_claim_success_when_launcher_fails():
    source = (ROOT / "actions" / "docx_tools.py").read_text(encoding="utf-8")
    assert "def _open_file(path: Path) -> bool:" in source
    assert "if not _open_file(source_path):" in source
    assert 'return f"Failed to open {source_path.name}."' in source
