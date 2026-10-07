from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_auto_heal_history_save_uses_atomic_replace_and_lock():
    source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
    block_start = source.index("    def _save_history")
    block = source[block_start:source.index("# ── 3.", block_start)]
    assert "_history_lock" in source
    assert "os.replace(temp, PATCH_HISTORY_FILE)" in block
    assert 'open(PATCH_HISTORY_FILE, "w"' not in block

def test_auto_heal_history_read_corruption_is_quarantined():
    import actions.auto_heal_engine as auto_heal

    original = auto_heal.PATCH_HISTORY_FILE
    try:
        auto_heal.PATCH_HISTORY_FILE = ROOT / "auto-heal-history-corrupt-test.json"
        auto_heal.PATCH_HISTORY_FILE.write_text("{broken", encoding="utf-8")
        import pytest
        with pytest.raises(RuntimeError, match="Patch history was corrupt"):
            auto_heal.SafetySandbox._load_history()
        assert not auto_heal.PATCH_HISTORY_FILE.exists()
        assert list(auto_heal.PATCH_HISTORY_FILE.parent.glob(
            auto_heal.PATCH_HISTORY_FILE.name + ".corrupt-*"
        ))
    finally:
        auto_heal.PATCH_HISTORY_FILE.unlink(missing_ok=True)
        for candidate in auto_heal.PATCH_HISTORY_FILE.parent.glob(
            auto_heal.PATCH_HISTORY_FILE.name + ".corrupt-*"
        ):
            candidate.unlink(missing_ok=True)
        auto_heal.PATCH_HISTORY_FILE = original


def test_auto_heal_history_save_reports_failure():
    import actions.auto_heal_engine as auto_heal

    original = auto_heal.PATCH_HISTORY_FILE
    class BrokenPath:
        def with_name(self, *_args):
            raise OSError("disk unavailable")
    try:
        auto_heal.PATCH_HISTORY_FILE = BrokenPath()
        assert auto_heal.SafetySandbox._save_history([]) is False
    finally:
        auto_heal.PATCH_HISTORY_FILE = original
