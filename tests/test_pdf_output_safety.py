from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pdf_explicit_output_paths_are_home_confined_and_non_overwriting_by_default():
    source = (ROOT / "actions" / "pdf_tools.py").read_text(encoding="utf-8")
    assert "PDF output path must remain inside the user's home directory." in source
    assert "PDF output path may not contain symlinked components." in source
    assert "Refusing to overwrite existing PDF without overwrite=True" in source
    assert "overwrite=bool(parameters.get("overwrite", False))" in source
