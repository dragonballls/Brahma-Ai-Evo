from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_volume_set_rejects_command_success_without_matching_final_state(monkeypatch):
    from actions import computer_settings as settings

    monkeypatch.setattr(settings, "_OS", "Linux")
    class Result:
        returncode = 0
        stderr = ""
    monkeypatch.setattr(settings.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(settings, "volume_get", lambda: 41)

    with pytest.raises(RuntimeError, match="final state mismatch"):
        settings.volume_set(50)


def test_volume_set_accepts_only_verified_final_state(monkeypatch):
    from actions import computer_settings as settings

    monkeypatch.setattr(settings, "_OS", "Linux")
    class Result:
        returncode = 0
        stderr = ""
    monkeypatch.setattr(settings.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setattr(settings, "volume_get", lambda: 50)
    settings.volume_set(50)


def test_windows_volume_set_does_not_fall_back_to_fake_toggle(monkeypatch):
    from actions import computer_settings as settings

    monkeypatch.setattr(settings, "_OS", "Windows")
    monkeypatch.setattr(settings, "pyautogui", type("PyAuto", (), {"press": lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fake mute fallback used"))})())
    def broken_audio_imports(*_args, **_kwargs):
        raise RuntimeError("audio backend unavailable")
    monkeypatch.setitem(__import__("sys").modules, "pycaw.pycaw", None)
    with pytest.raises(RuntimeError, match="Unable to set volume on Windows"):
        settings.volume_set(50)


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
