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


def test_public_browser_code_execution_is_not_exposed():
    source = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    assert 'if action in {"evaluate", "eval", "run_code", "execute"}' in source
    assert "arbitrary code execution is not exposed" in source
    assert "allow_unsafe_code" not in source


def test_playwright_mcp_launcher_does_not_request_shell_execution():
    source = (ROOT / "actions" / "playwright_mcp_client.py").read_text(encoding="utf-8")
    assert "resolved_npx = shutil.which(npx_cmd)" in source
    assert "shell=False" in source


def test_browser_evaluation_is_not_exposed_as_a_planner_action():
    planner = (ROOT / "agent" / "planner.py").read_text(encoding="utf-8")
    browser = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    assert '\"evaluate\"' not in planner
    assert "def browser_evaluate_internal" in browser
    assert 'allow_unsafe_code=true' not in browser


def test_browser_backend_registers_process_exit_cleanup():
    source = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    assert "import atexit" in source
    assert "atexit.register(shutdown_browser)" in source


def test_browser_shutdown_cleanup_is_registered_after_definition():
    source = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    assert source.index("def shutdown_browser") < source.index("atexit.register(shutdown_browser)")
