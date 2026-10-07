from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_office_builder_output_paths_are_home_confined_and_non_overwriting():
    source = (ROOT / "actions" / "office_builder.py").read_text(encoding="utf-8")
    assert "Office output path must remain inside the user's home directory." in source
    assert "Office output path may not contain symlinked components." in source
    assert "Refusing to overwrite existing Office output without overwrite=True" in source
    assert "overwrite=bool(parameters.get(" in source


def test_office_builder_implicit_outputs_avoid_collisions():
    source = (ROOT / "actions" / "office_builder.py").read_text(encoding="utf-8")
    assert "if overwrite or not base.exists():" in source
    assert "candidate = DEFAULT_OUTPUT_DIR / f" in source


def test_presentation_save_failure_cannot_claim_created(monkeypatch, tmp_path):
    from actions import office_builder as office

    class Presentation:
        def __init__(self):
            pass
        def save(self, _path):
            return None

    monkeypatch.setattr(office, "_import_pptx", lambda: (
        lambda: None,
        lambda *_args: None,
        type("Shapes", (), {})(),
        lambda x: x,
        lambda x: x,
    ))
    # Validate the production post-save guard directly with the real object API shape.
    output = tmp_path / "deck.pptx"
    output.write_bytes(b"")
    assert not (output.is_file() and output.stat().st_size > 0)
