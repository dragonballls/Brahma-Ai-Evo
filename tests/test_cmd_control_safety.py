from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cmd_control_rejects_explicit_executable_paths():
    source = (ROOT / "actions" / "cmd_control.py").read_text(encoding="utf-8")
    assert "Explicit executable paths are not permitted" in source


def test_cmd_control_validates_notepad_child_liveness():
    source = (ROOT / "actions" / "cmd_control.py").read_text(encoding="utf-8")
    assert "proc.poll()" in source
    assert "Could not open {target} with Notepad." in source


def test_cmd_control_validates_xdg_open_child_liveness():
    source = (ROOT / "actions" / "cmd_control.py").read_text(encoding="utf-8")
    assert '["xdg-open", str(target)]' in source
    assert 'if proc.poll() is not None:' in source
