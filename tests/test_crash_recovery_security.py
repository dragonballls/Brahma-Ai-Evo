from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_crash_recovery_confines_targets_and_backups():
    source = (ROOT / "core" / "crash_recovery.py").read_text(encoding="utf-8")
    assert "BASE_DIR = Path(__file__).resolve().parent.parent" in source
    assert "target.relative_to(BASE_DIR.resolve())" in source
    assert "backup.relative_to(BACKUPS_DIR.resolve())" in source
    assert "PROTECTED_RECOVERY_FILES" in source
    assert "os.replace(target_tmp, target)" in source


def test_boot_sentry_confines_targets_and_protected_files():
    source = (ROOT / "core" / "boot_sentry.py").read_text(encoding="utf-8")
    assert "target_file.relative_to(BASE_DIR.resolve())" in source
    assert "backup_file.relative_to(BACKUPS_DIR.resolve())" in source
    assert "PROTECTED_BOOT_FILES" in source
    assert "os.replace(target_tmp, target_file)" in source
