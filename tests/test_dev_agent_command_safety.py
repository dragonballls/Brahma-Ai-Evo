from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dev_agent_run_command_has_an_explicit_development_allowlist():
    source = (ROOT / "actions" / "dev_agent.py").read_text(encoding="utf-8")
    assert "_SAFE_RUN_PROGRAMS" in source
    assert "_BLOCKED_RUN_FLAGS" in source
    assert "unsupported development executable" in source
    assert "interpreter evaluation flags are not permitted" in source


def test_dev_agent_run_command_uses_shell_false_and_shlex():
    source = (ROOT / "actions" / "dev_agent.py").read_text(encoding="utf-8")
    start = source.index("def _run_project")
    end = source.index("def _try_auto_install", start)
    block = source[start:end]
    assert "shlex.split" in block
    assert "subprocess.run(" in block
    assert "shell=True" not in block
    assert "-c" in block

def test_dev_agent_rejects_path_disguises_for_allowlisted_commands():
    source = (ROOT / "actions" / "dev_agent.py").read_text(encoding="utf-8")
    start = source.index("def _run_project")
    end = source.index("def _try_auto_install", start)
    block = source[start:end]
    assert "executable paths must use the approved bare development-command names" in block
    assert 'if "/" in parts[0] or "\\\\" in parts[0]' in block


def test_dev_agent_dependency_install_rejects_unsafe_specifications():
    source = (ROOT / "actions" / "dev_agent.py").read_text(encoding="utf-8")
    assert "_DEP_SPEC_RE" in source
    assert "unsafe package specification" in source
    assert "allowed_dependencies" in source
