from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_executor_recovery_alternative_uses_the_same_failure_contract():
    source = (ROOT / "agent" / "executor.py").read_text(encoding="utf-8")
    start = source.index("if fix_suggestion and tool != \"generated_code\":")
    end = source.index("failed_step = step", start)
    block = source[start:end]
    assert "_raise_for_failed_tool_result(res)" in block
    assert "completed_steps.append(fixed_step)" in block