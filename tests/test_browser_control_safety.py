from pathlib import Path

import pytest


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

def test_browser_navigation_rejects_local_and_script_url_schemes():
    from actions.playwright_mcp_client import validate_browser_url

    for unsafe in (
        "file:///C:/Users/test/secret.txt",
        "FILE:///C:/Users/test/secret.txt",
        "javascript:alert(1)",
        "data:text/html,<h1>secret</h1>",
        "vbscript:MsgBox(1)",
        "blob:https://example.com/id",
        "filesystem:https://example.com/temporary/file.txt",
        "view-source:https://example.com",
        "about:srcdoc",
    ):
        with pytest.raises(ValueError):
            validate_browser_url(unsafe)

    assert validate_browser_url("https://example.com") == "https://example.com"
    assert validate_browser_url("http://127.0.0.1:8080/health") == "http://127.0.0.1:8080/health"
    assert validate_browser_url("localhost:8765") == "https://localhost:8765"
    assert validate_browser_url("about:blank") == "about:blank"


def test_playwright_file_upload_accepts_target_arguments_used_by_browser_control():
    source = Path("actions/playwright_mcp_client.py").read_text(encoding="utf-8")
    assert "def file_upload(" in source
    assert "element: Optional[str] = None" in source
    assert "selector: Optional[str] = None" in source
    assert '"browser_file_upload", args' in source


def test_browser_public_contract_does_not_advertise_blocked_arbitrary_code_actions():
    source = Path("actions/browser_control.py").read_text(encoding="utf-8")
    public_start = source.index("Complete browser automation")
    public_block = source[public_start:source.index('    import time', public_start)]
    assert "evaluate" not in public_block
    assert "run_code" not in public_block
    assert "expression" not in public_block
