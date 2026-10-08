from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_system_manager_kill_is_exact_user_owned_and_protected():
    source = (ROOT / "actions" / "system_manager.py").read_text(encoding="utf-8")
    start = source.index("PROTECTED_PROCESSES")
    block = source[start:source.index("\ndef run(", start)]
    assert "PROTECTED_PROCESSES" in block
    assert "normalized_name != target_name" in block
    assert "owner_short = owner.casefold()" in block
    assert "rsplit(" in block
    assert "Refusing to terminate a protected system PID." in block
    assert "name.lower() in" not in block


def test_process_manager_refuses_ambiguous_name_based_mass_kills():
    source = (ROOT / "actions" / "system_manager.py").read_text(encoding="utf-8")
    assert "Multiple matching processes found; refusing mass termination." in source
    assert "Specify a PID." in source


def test_process_manager_requires_verified_process_termination_before_success():
    source = (ROOT / "actions" / "system_manager.py").read_text(encoding="utf-8")
    assert "process.kill()" in source
    assert "process.wait(timeout=2)" in source
    assert "Failed to confirm termination" in source
