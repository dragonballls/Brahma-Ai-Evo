from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auto_heal_traceback_target_is_confined_to_repo_root():
    source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
    start = source.index("    def parse(tb_text")
    block = source[start:source.index("# ── 2.", start)]
    assert "repo_root = BASE_DIR.resolve()" in block
    assert "candidate_resolved.relative_to(repo_root)" in block
    assert "p.is_absolute()" in block
    assert "site-packages" in block


def test_auto_heal_exclusive_helpers_create_real_files(tmp_path):
    from actions.auto_heal_engine import _copy_file_exclusive, _write_exclusive_text

    source = tmp_path / "source.py"
    copied = tmp_path / "copied.tmp"
    written = tmp_path / "written.tmp"
    source.write_text("print('ok')", encoding="utf-8")
    _copy_file_exclusive(source, copied)
    _write_exclusive_text(written, "safe")
    assert copied.read_text(encoding="utf-8") == "print('ok')"
    assert written.read_text(encoding="utf-8") == "safe"


def test_auto_heal_source_publish_uses_exclusive_temp_writer():
    source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
    assert "def _write_exclusive_text" in source
    assert "def _copy_file_exclusive" in source
    assert "_write_exclusive_text(target_tmp, staged_source)" in source
