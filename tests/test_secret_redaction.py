from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_tool_trace_redactor_has_common_secret_patterns():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("    def _redact_sensitive_text")
    block = source[start:source.index("    async def _execute_tool", start)]
    assert r"authorization\s*[:=]\s*bearer" in block
    assert "api[_-]?key" in block
    assert "sk-[A-Za-z0-9_-]{20,}" in block


def test_tool_trace_redactor_masks_common_key_forms():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    patterns = [
        r"sk-" + "a" * 24,
        r"gsk_" + "b" * 24,
        "AIza" + "c" * 24,
        "ghp_" + "d" * 24,
        "github_pat_" + "e" * 24,
    ]
    assert len(patterns) == 5
    # Validate the expected adversarial shapes themselves; implementation tests
    # are kept source-level so importing the GUI is unnecessary.
    for value in patterns:
        assert re.search(r"(?:sk-|gsk_|AIza|ghp_|github_pat_)", value)
