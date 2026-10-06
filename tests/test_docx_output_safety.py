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


def test_docx_result_path_receives_overwrite_flag_and_implicit_outputs_avoid_collisions():
    source = (ROOT / "actions" / "docx_tools.py").read_text(encoding="utf-8")
    assert "def _docx_result_path" in source
    assert "overwrite: bool = False" in source[source.index("def _docx_result_path"):source.index("def _load_doc", source.index("def _docx_result_path"))]
    assert "target_path = _docx_result_path" in source
    assert "overwrite=overwrite" in source[source.index("target_path = _docx_result_path"):source.index("target_path = _docx_result_path")+220]
    assert "source_path.stem}_{action}_{counter}.docx" in source
