from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_destructive_settings_check_platform_command_results():
    source = (ROOT / "actions" / "computer_settings.py").read_text(encoding="utf-8")
    assert "def _confirmed_action" in source
    assert "outcome = f()" in source
    assert "if outcome is False:" in source
    assert "Restart command failed" in source
    assert "Shutdown command failed" in source
    assert "Unable to change Wi-Fi state" in source
    assert 'shell=True' not in source


def test_destructive_settings_confirmation_does_not_discard_action_failure():
    source = (ROOT / "actions" / "computer_settings.py").read_text(encoding="utf-8")
    start = source.index("if action in _IRREVERSIBLE:")
    end = source.index("if action == \"volume_set\":", start)
    block = source[start:end]
    assert "run=_confirmed_action" in block
    assert "f(), f\"{a} done.\"" not in block
