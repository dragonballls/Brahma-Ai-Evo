from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auto_heal_history_save_uses_atomic_replace_and_lock():
    source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
    block_start = source.index("    def _save_history")
    block = source[block_start:source.index("# ── 3.", block_start)]
    assert "_history_lock" in source
    assert "os.replace(temp, PATCH_HISTORY_FILE)" in block
    assert 'open(PATCH_HISTORY_FILE, "w"' not in block
