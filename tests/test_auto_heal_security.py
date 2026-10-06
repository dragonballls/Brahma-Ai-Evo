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
