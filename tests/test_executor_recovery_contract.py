from pathlib import Path

import pytest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def test_executor_recovery_alternative_uses_the_same_failure_contract():
    source = (ROOT / "agent" / "executor.py").read_text(encoding="utf-8")
    start = source.index("if fix_suggestion and tool != \"generated_code\":")
    end = source.index("failed_step = step", start)
    block = source[start:end]
    assert "_raise_for_failed_tool_result(res)" in block
    assert "completed_steps.append(fixed_step)" in block

def test_executor_does_not_invent_success_for_empty_tool_results():
    import agent.executor as executor

    try:
        executor._require_tool_result("test_tool", None)
    except RuntimeError as exc:
        assert "returned no result" in str(exc)
    else:
        raise AssertionError("empty tool result was accepted as success")


def test_executor_explicitly_marks_skipped_steps():
    source = (ROOT / "agent" / "executor.py").read_text(encoding="utf-8")
    block = source[source.index("elif decision == ErrorDecision.SKIP"):source.index("elif decision == ErrorDecision.ABORT", source.index("elif decision == ErrorDecision.SKIP"))]
    assert 'skipped_step["_skipped"] = True' in block
    assert "SKIPPED after failure" in block


def test_executor_forge_failure_is_structured_not_reported_as_success():
    source = (ROOT / "agent" / "executor.py").read_text(encoding="utf-8")
    block = source[source.index("execution_output = """):source.index("def _raise_for_failed_tool_result", source.index("execution_output = """))]
    assert "_raise_for_failed_tool_result(run_result)" in block
    assert 'return {"success": False, "error": message, "name": name, "created": True}' in block


def test_executor_preserves_falsy_action_failure_results():
    import agent.executor as executor

    with (
        patch("actions.open_app.open_app", return_value=False),
        patch("actions.web_search.web_search", return_value=False),
        patch("actions.game_updater.game_updater", return_value=False),
        patch("actions.browser_control.browser_control", return_value=False),
        patch("actions.file_controller.file_controller", return_value=False),
        patch("actions.cmd_control.cmd_control", return_value=False),
    ):
        for tool in ("open_app", "web_search", "game_updater", "browser_control", "file_controller", "cmd_control"):
            result = executor._call_tool(tool, {}, None)
            assert result is False
            with pytest.raises(RuntimeError):
                executor._raise_for_failed_tool_result(result)


def test_call_screening_control_rejects_inactive_operations():
    import agent.executor as executor
    with (
        patch("actions.call_assistant.take_over_active_call", return_value=False),
        patch("actions.call_assistant.hang_up_active_call", return_value=False),
    ):
        with pytest.raises(RuntimeError):
            executor._call_tool("call_screening", {"action": "take_over"}, None)
        with pytest.raises(RuntimeError):
            executor._call_tool("call_screening", {"action": "hang_up"}, None)


def test_call_assistant_does_not_activate_when_answer_is_unconfirmed():
    from actions.call_assistant import CallAssistant

    CallAssistant._active_instance = None
    assistant = CallAssistant({"title": "Caller", "app": "TestApp"})
    with patch(
        "actions.attention_monitor.handle_call_action",
        return_value="I found the call on TestApp, but could not confirm the answer button.",
    ):
        assert assistant.start() is False

    assert assistant.is_active is False
    assert CallAssistant.get_active() is None
