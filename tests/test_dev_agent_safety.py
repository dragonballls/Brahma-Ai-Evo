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

def test_dev_agent_file_tools_confine_paths_to_workspace():
    source = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
    assert "def _workspace_path" in source
    assert "Path escapes the configured developer workspace." in source
    assert "path = self._workspace_path(file_path)" in source
    assert "search_root = self._workspace_path(path)" in source


def test_brahma_dev_records_only_real_verification_commands():
    from actions.brahma_dev_agent import BrahmaDevAgent

    assert BrahmaDevAgent._is_verification_command("Bash", {"command": "python -m pytest -q"})
    assert BrahmaDevAgent._is_verification_command("bash", {"command": "npm run build"})
    assert not BrahmaDevAgent._is_verification_command("bash", {"command": "echo tests passed"})


def test_brahma_dev_refuses_success_without_verification(monkeypatch, tmp_path):
    from actions.brahma_dev_agent import BrahmaDevAgent

    agent = BrahmaDevAgent(tmp_path)
    monkeypatch.setattr(agent, "_github_research_preflight", lambda _instruction: "")
    monkeypatch.setattr(agent, "_call_llm", lambda: "The project is complete.")
    result = agent.run("build a project", max_turns=1)

    assert result.startswith("Error: Developer task incomplete")
    assert agent.verification_evidence == []


def test_brahma_dev_accepts_final_response_after_verified_command(monkeypatch, tmp_path):
    from actions.brahma_dev_agent import BrahmaDevAgent

    agent = BrahmaDevAgent(tmp_path)
    monkeypatch.setattr(agent, "_github_research_preflight", lambda _instruction: "")
    monkeypatch.setattr(agent.tools, "bash", lambda *_args, **_kwargs: "pytest: 1 passed")
    replies = iter([
        '<tool_call><name>Bash</name><arguments>{"command":"python -m pytest -q"}</arguments></tool_call>',
        "The project is complete and the verification command passed.",
    ])
    monkeypatch.setattr(agent, "_call_llm", lambda: next(replies))

    result = agent.run("build a project", max_turns=2)

    assert result == "The project is complete and the verification command passed."
    assert agent.verification_evidence == ["python -m pytest -q"]
