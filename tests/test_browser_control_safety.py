from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_browser_close_keeps_runtime_reusable_and_exposes_shutdown():
    source = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    close_start = source.index("async def _close_browser")
    close_end = source.index("def browser_control", close_start)
    close_block = source[close_start:close_end]
    assert "await self._playwright.stop()" not in close_block
    assert "async def _shutdown_resources" in source
    assert "def shutdown_browser" in source
    assert "_bt_started = False" in source


def test_public_browser_code_execution_requires_explicit_opt_in():
    source = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    assert "allow_unsafe_code=true" in source
    assert 'if action in {"evaluate", "eval", "run_code", "execute"}' in source
