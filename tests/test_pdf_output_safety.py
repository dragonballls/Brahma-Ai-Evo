from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_pdf_explicit_output_paths_are_home_confined_and_non_overwriting_by_default():
    source = (ROOT / "actions" / "pdf_tools.py").read_text(encoding="utf-8")
    assert "PDF output path must remain inside the user's home directory." in source
    assert "PDF output path may not contain symlinked components." in source
    assert "Refusing to overwrite existing PDF without overwrite=True" in source
    assert 'overwrite=bool(parameters.get("overwrite", False))' in source


def test_pdf_implicit_output_collisions_get_unique_names():
    source = (ROOT / "actions" / "pdf_tools.py").read_text(encoding="utf-8")
    assert "if overwrite or not base.exists():" in source
    assert "candidate = DEFAULT_OUTPUT_DIR / f\"{base.stem}_{counter}{base.suffix}\"" in source


def test_pdf_create_cannot_claim_success_when_build_produces_no_artifact(tmp_path, monkeypatch):
    from actions import pdf_tools

    monkeypatch.setattr(pdf_tools.Path, "home", classmethod(lambda cls: tmp_path))
    real_pdf = pdf_tools._import_pdf()
    RealDocTemplate = real_pdf["SimpleDocTemplate"]

    class NoBuildDocTemplate(RealDocTemplate):
        def build(self, *args, **kwargs):
            return None

    fake_pdf = dict(real_pdf)
    fake_pdf["SimpleDocTemplate"] = NoBuildDocTemplate
    monkeypatch.setattr(pdf_tools, "_import_pdf", lambda: fake_pdf)

    result = pdf_tools.create_pdf({
        "action": "create",
        "title": "Verification Test",
        "content": "hello",
        "output_path": str(tmp_path / "document.pdf"),
        "auto_open": False,
    })
    assert result.startswith("PDF creation failed:")
    assert not (tmp_path / "document.pdf").exists()
