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


def test_auto_heal_copy_rejects_hardlinked_source(tmp_path):
    import os
    import pytest
    from actions.auto_heal_engine import _copy_file_exclusive

    source = tmp_path / "source.py"
    outside = tmp_path / "outside.py"
    destination = tmp_path / "copy.tmp"
    source.write_text("print('secret')", encoding="utf-8")
    try:
        os.link(source, outside)
    except (OSError, NotImplementedError):
        pytest.skip("Hard-link support unavailable")

    with pytest.raises(OSError, match="hard-linked"):
        _copy_file_exclusive(source, destination)


def test_auto_heal_refuses_concurrent_target_change_before_publish(tmp_path, monkeypatch):
    import actions.auto_heal_engine as auto_heal

    target = tmp_path / "repair_target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(auto_heal, "BASE_DIR", tmp_path)
    monkeypatch.setattr(auto_heal, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(auto_heal, "PATCH_HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(
        auto_heal.TracebackAnalyzer,
        "parse",
        staticmethod(lambda _tb: {
            "success": True,
            "target_file": str(target),
            "line_number": 1,
            "function_name": "<module>",
            "exception_type": "ValueError",
            "exception_message": "test",
        }),
    )
    monkeypatch.setattr(
        auto_heal.AutoHealEngine,
        "_synthesize_patch_code",
        classmethod(lambda cls, **_kwargs: {
            "success": True,
            "target_chunk": "value = 1",
            "replacement_chunk": "value = 2",
            "explanation": "test repair",
        }),
    )
    real_backup = auto_heal.SafetySandbox.create_backup
    def backup_then_change(path):
        backup = real_backup(path)
        path.write_text("value = 99\n", encoding="utf-8")
        return backup
    monkeypatch.setattr(auto_heal.SafetySandbox, "create_backup", staticmethod(backup_then_change))
    monkeypatch.setattr(auto_heal.SafetySandbox, "validate_code", staticmethod(lambda code, file_name="<staging>": (True, None)))
    monkeypatch.setattr(
        "core.repository_sync.publish_verified_repair",
        lambda *args, **kwargs: {"published": False, "reason": "test"},
    )

    result = auto_heal.AutoHealEngine.heal_traceback("traceback")
    assert result["success"] is False
    assert "changed after backup creation" in result["message"]
    assert target.read_text(encoding="utf-8") == "value = 99\n"


def test_auto_heal_heal_command_uses_canonical_crash_log(tmp_path, monkeypatch):
    import actions.auto_heal_engine as auto_heal

    crash_log = tmp_path / "FATAL_CRASH.log"
    crash_log.write_text("synthetic traceback", encoding="utf-8")
    monkeypatch.setattr(auto_heal, "FATAL_CRASH_LOG_PATH", crash_log)
    monkeypatch.setattr(auto_heal.AutoHealEngine, "get_last_error", classmethod(lambda cls: None))

    seen = {}

    def fake_heal(cls, traceback_text, context_notes="", dry_run=False):
        seen["traceback"] = traceback_text
        return {"message": "healed"}

    monkeypatch.setattr(
        auto_heal.AutoHealEngine,
        "heal_traceback",
        classmethod(fake_heal),
    )

    result = auto_heal.auto_heal({"action": "heal"})
    assert result == "healed"
    assert seen["traceback"] == "synthetic traceback"
