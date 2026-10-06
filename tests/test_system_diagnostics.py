from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_brightness_fallback_uses_argument_list_without_shell():
    source = (ROOT / "actions" / "system_diagnostics_mcp.py").read_text(encoding="utf-8")
    start = source.index("def get_display_brightness_status")
    end = source.index("\ndef get_full_diagnostics", start)
    block = source[start:end]
    assert "shell=True" not in block
    assert '"-NoProfile"' in block
    assert '"-NonInteractive"' in block
    assert "subprocess.check_output(" in block


def test_process_termination_uses_exact_name_matching():
    source = (ROOT / "actions" / "system_diagnostics_mcp.py").read_text(encoding="utf-8")
    start = source.index("def kill_process")
    end = source.index("\n\n# ── 4.", start)
    block = source[start:end]
    assert "if p_name not in {clean_target, clean_target_exe}:" in block
    assert "clean_target in p_name" not in block
