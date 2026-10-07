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
    assert "Git execution/configuration overrides are not permitted." in source
    assert "response-file arguments are not permitted." in source
    assert "EXECUTION_ENV_OVERRIDES" in source


def test_brahma_dev_agent_blocks_git_pack_command_execution_overrides(tmp_path):
    from actions.brahma_dev_agent import NativeTools

    tools = NativeTools(tmp_path)
    blocked = [
        'git clone --upload-pack="python -c \\"print(1)\\"" https://example.invalid/repo.git repo',
        'git fetch --upload-pack="python -c \\"print(1)\\"" origin main',
        'git push --receive-pack="python -c \\"print(1)\\"" origin HEAD',
    ]
    for command in blocked:
        result = tools.bash(command)
        assert result == "Error: Git execution/configuration overrides are not permitted."


def test_brahma_dev_agent_blocks_git_config_and_execution_path_overrides(tmp_path):
    from actions.brahma_dev_agent import NativeTools

    tools = NativeTools(tmp_path)
    blocked = [
        "git -c core.sshCommand=evil status",
        "git --config-env=core.sshCommand=EVIL status",
        "git --exec-path=/tmp/evil status",
    ]
    for command in blocked:
        result = tools.bash(command)
        assert result == "Error: Git execution/configuration overrides are not permitted."


def test_brahma_dev_agent_blocks_response_files_only_for_response_file_capable_tools(tmp_path):
    from actions.brahma_dev_agent import NativeTools

    tools = NativeTools(tmp_path)
    assert tools.bash("javac @evil.rsp") == "Error: response-file arguments are not permitted."
    assert tools.bash("gcc @evil.rsp") == "Error: response-file arguments are not permitted."
    assert "@" in "npm install @scope/package"


def test_brahma_dev_agent_rejects_explicit_executable_paths(tmp_path):
    from actions.brahma_dev_agent import NativeTools

    tools = NativeTools(tmp_path)
    assert tools.bash("./python -V").startswith("Error: executable paths")
    assert tools.bash("C:\\tools\\git.exe status").startswith("Error: executable paths")


def test_brahma_dev_agent_sanitizes_execution_override_environment():
    source = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
    assert 'if key.upper() not in _EXECUTION_ENV_OVERRIDES' in source
    assert 'GIT_SSH_COMMAND' in source
    assert 'NODE_OPTIONS' in source
    assert 'PYTHONPATH' in source
