from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_system_manager_kill_is_exact_user_owned_and_protected():
    source = (ROOT / "actions" / "system_manager.py").read_text(encoding="utf-8")
    start = source.index("PROTECTED_PROCESSES")
    block = source[start:source.index("\ndef run(", start)]
    assert "PROTECTED_PROCESSES" in block
    assert "normalized_name != target_name" in block
    assert 'owner_short = owner.casefold().rsplit("\\", 1)[-1]' in block
    assert "Refusing to terminate a protected system PID." in block
    assert "name.lower() in" not in block
