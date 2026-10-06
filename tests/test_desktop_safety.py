from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_task_never_executes_generated_python():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert "def _execute_desktop_plan" in source
    assert "exec(compile(" not in source
    assert "_execute_generated_code" not in source
    assert '"actions"' in source


def test_desktop_task_has_a_bounded_declarative_action_allowlist():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert 'allowed = {"click"' in source
    assert "len(actions) > 20" in source
    assert "shell commands" in source
    assert "arbitrary code" in source

def test_platform_script_paths_are_escaped_before_interpolation():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert "def _escape_script_string" in source
    assert "escaped_path = _escape_script_string(str(path))" in source
    assert 'POSIX file "{escaped_path}"' in source
    assert "file://{_escape_script_string(str(path))}" in source
