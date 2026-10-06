from pathlib import Path

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
