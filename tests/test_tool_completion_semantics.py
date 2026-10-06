from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tool_execution_initializes_result_and_shutdown_result():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("    async def _execute_tool")
    block = source[start:source.index("    async def _serve_dashboard", start)]
    assert "result = \"\"" in block
    assert 'result = "Shutdown initiated."' in block


def test_tool_completion_detects_explicit_failure_results():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'result.get("success") is False' in source
    assert 'result.get("ok") is False' in source
    assert 'startswith(\n                ("error:", "failed:", "failure:", "unable to ", "could not ")' in source
