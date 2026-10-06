from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_brahma_dev_agent_does_not_invoke_a_shell_for_bash_tool():
    source = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
    start = source.index("def bash(self, command")
    end = source.index("def file_read", start)
    block = source[start:end]
    assert "shell=False" in block
    assert "powershell" not in block
    assert "subprocess.run(" in block


def test_brahma_dev_agent_rejects_shell_control_and_eval_execution():
    source = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
    assert "_BLOCKED_COMMAND_INTERPRETERS" in source
    assert "_BLOCKED_EVAL_FLAGS" in source
    assert "shell control operators and redirection are not permitted." in source
    assert "interpreter evaluation flags are not permitted." in source


def test_brahma_dev_agent_rejects_out_of_workspace_command_paths():
    source = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
    assert "command arguments may not access paths outside the configured developer workspace." in source
    assert "resolved.relative_to(root)" in source
    assert "Git repository/work-tree overrides are not permitted." in source
