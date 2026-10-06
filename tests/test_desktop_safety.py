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