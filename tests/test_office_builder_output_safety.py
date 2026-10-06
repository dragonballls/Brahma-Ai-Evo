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
