from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from actions.brahma_dev_agent import BrahmaDevAgent, NativeTools


def _tool_call(name: str, arguments: dict) -> str:
    import json
    return (
        "<tool_call><name>"
        + name
        + "</name><arguments>"
        + json.dumps(arguments)
        + "</arguments></tool_call>"
    )


def _agent(tmp_path: Path) -> BrahmaDevAgent:
    agent = BrahmaDevAgent(tmp_path)
    agent._github_research_preflight = Mock(return_value="research complete")
    agent.tools.file_edit = Mock(return_value="Successfully updated example.py (replaced 1 occurrence(s)).")
    agent.tools.bash = Mock(return_value="pytest passed\n[Exit code: 0]")
    return agent


def test_dev_agent_rejects_final_response_after_edit_that_follows_verification(tmp_path):
    agent = _agent(tmp_path)
    agent._call_llm = Mock(
        side_effect=[
            _tool_call("Bash", {"command": "pytest tests/test_example.py"}),
            _tool_call(
                "FileEdit",
                {"file_path": "example.py", "old_string": "old", "new_string": "new"},
            ),
            "Done.",
        ]
    )

    result = agent.run("update example.py")
    assert result.startswith("Error: Developer task incomplete:")
    assert "after the last authoritative verification" in result


def test_dev_agent_accepts_final_response_when_verification_follows_last_edit(tmp_path):
    agent = _agent(tmp_path)
    agent._call_llm = Mock(
        side_effect=[
            _tool_call(
                "FileEdit",
                {"file_path": "example.py", "old_string": "old", "new_string": "new"},
            ),
            _tool_call("Bash", {"command": "pytest tests/test_example.py"}),
            "Done.",
        ]
    )

    assert agent.run("update example.py") == "Done."


def test_dev_agent_git_mutation_commands_are_rejected():
    tools = NativeTools(Path.cwd())
    for command in (
        "git add example.py",
        "git commit -m test",
        "git push origin main",
        "git reset --hard HEAD~1",
        "git switch main",
        "git checkout main",
        "git revert HEAD",
        "git merge main",
        "git rebase main",
        "git clean -fd",
        "git branch feature",
    ):
        result = tools.bash(command)
        assert result.startswith("Error: developer Git access is read-only"), command


def test_dev_agent_git_read_only_commands_remain_available(monkeypatch):
    tools = NativeTools(Path.cwd())
    monkeypatch.setattr(tools, "_notify", lambda _message: None)
    monkeypatch.setattr("actions.brahma_dev_agent.shutil.which", lambda _name, path=None: "/usr/bin/git")

    import subprocess

    fake = subprocess.CompletedProcess(["git", "status"], 0, stdout="clean", stderr="")
    monkeypatch.setattr("actions.brahma_dev_agent.subprocess.run", lambda *args, **kwargs: fake)
    result = tools.bash("git status")
    assert "[Exit code: 0]" in result
