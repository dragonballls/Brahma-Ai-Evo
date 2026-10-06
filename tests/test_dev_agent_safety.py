from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dev_agent_vscode_launcher_does_not_use_shell_execution():
    source = (ROOT / "actions" / "dev_agent.py").read_text(encoding="utf-8")
    start = source.index("def _open_vscode")
    end = source.index("def _run_project", start)
    block = source[start:end]
    assert "shell=True" not in block
    assert "shell=False" in block
    assert "shutil.which(cmd)" in block

def test_dev_agent_treats_timeouts_and_nonzero_exit_codes_as_failures():
    import actions.dev_agent as dev_agent

    assert dev_agent._has_error("Run timed out after 30s.", "python main.py")
    assert dev_agent._has_error("Run failed (exit 2).", "python main.py")
    assert not dev_agent._has_error("Ran with no output.", "python main.py")
