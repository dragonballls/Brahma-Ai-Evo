from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_window_title_is_escaped_for_windows_and_macos_command_languages():
    source = (ROOT / "actions" / "computer_control.py").read_text(encoding="utf-8")
    assert "powershell_title = title.replace" in source
    assert 'replace("\'\", "\'\'\")' in source
    assert "applescript_title = (" in source
    assert "replace(\"\\\\\", \"\\\\\\\\\")" in source
