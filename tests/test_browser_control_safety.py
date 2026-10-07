import asyncio
from pathlib import Path
from unittest.mock import patch

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

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        assert validate_browser_url("https://example.com") == "https://example.com"
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 8080))]):
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


def test_playwright_mcp_errors_cannot_be_returned_as_successful_text():
    source = (ROOT / "actions" / "playwright_mcp_client.py").read_text(encoding="utf-8")
    assert "if result_obj.get(\"isError\", False):" in source
    assert '"success": False' in source[source.index("if result_obj.get(\"isError\", False):"):source.index("if result_obj.get(\"isError\", False):") + 500]


def test_playwright_mcp_server_disconnect_wakes_pending_requests():
    source = (ROOT / "actions" / "playwright_mcp_client.py").read_text(encoding="utf-8")
    assert "Playwright MCP server connection closed." in source
    assert "event.set()" in source[source.index("Playwright MCP server connection closed.")-250:source.index("Playwright MCP server connection closed.")+250]


def test_browser_url_rejects_private_ipv4_ipv6_credentials_and_remote_http():
    from actions.playwright_mcp_client import validate_browser_url

    unsafe = (
        "http://192.168.1.1/",
        "https://10.0.0.8/",
        "https://[::1]/",
        "https://[fc00::1]/",
        "https://[fe80::1]/",
        "https://[ff02::1]/",
        "https://[::]/",
        "https://user:password@example.com/",
    )
    for url in unsafe:
        with pytest.raises(ValueError):
            validate_browser_url(url)

    with patch("socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("93.184.216.34", 443)),
        (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443)),
    ]):
        assert validate_browser_url("https://example.com/") == "https://example.com/"

def test_browser_request_guard_blocks_cross_host_redirects_before_continue():
    from actions.browser_control import _BrowserThread

    class Request:
        def __init__(self, url, redirected_from=None):
            self.url = url
            self.resource_type = "document"
            self.redirected_from = redirected_from

    class Route:
        def __init__(self):
            self.aborted = None
            self.continued = False

        async def abort(self, reason):
            self.aborted = reason

        async def continue_(self):
            self.continued = True

    previous = Request("https://example.com/start")
    redirected = Request("https://example.net/landing", redirected_from=previous)
    route = Route()
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        asyncio.run(_BrowserThread()._guard_request(route, redirected))
    assert route.aborted == "blockedbyclient"
    assert route.continued is False

def test_browser_request_guard_allows_same_host_navigation_only_after_policy_validation():
    from actions.browser_control import _BrowserThread

    class Request:
        def __init__(self, url, redirected_from=None):
            self.url = url
            self.resource_type = "document"
            self.redirected_from = redirected_from

    class Route:
        def __init__(self):
            self.aborted = None
            self.continued = False

        async def abort(self, reason):
            self.aborted = reason

        async def continue_(self):
            self.continued = True

    previous = Request("https://example.com/start")
    redirected = Request("https://example.com/next", redirected_from=previous)
    route = Route()
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        asyncio.run(_BrowserThread()._guard_request(route, redirected))
    assert route.aborted is None
    assert route.continued is True

def test_public_browser_control_uses_guarded_native_backend_instead_of_mcp_navigation():
    source = (ROOT / "actions" / "browser_control.py").read_text(encoding="utf-8")
    start = source.index("def browser_control(")
    public = source[start:]
    assert "get_playwright_mcp_client()" not in public
    assert "_bt._go_to" in public
    assert "_bt._back" in public
    assert "_bt._forward" in public


def test_guarded_browser_context_disables_service_workers_before_request_routing():
    from actions.browser_control import _BrowserThread

    class Page:
        def is_closed(self):
            return False

    class Context:
        def __init__(self):
            self.kwargs = None
            self.route_args = None

        async def route(self, *args):
            self.route_args = args

        async def new_page(self):
            return Page()

    class Browser:
        def __init__(self):
            self.context = Context()

        def is_connected(self):
            return True

        async def new_context(self, **kwargs):
            self.context.kwargs = kwargs
            return self.context

    async def run():
        thread = _BrowserThread()
        thread._browser = Browser()
        page = await thread._get_page()
        return thread._browser.context, page

    context, page = asyncio.run(run())
    assert page is not None
    assert context.kwargs["service_workers"] == "block"
    assert context.route_args[0] == "**/*"
    assert context.route_args[1].__func__ is _BrowserThread._guard_request


def test_browser_localhost_alias_allows_local_https(monkeypatch):
    from actions.playwright_mcp_client import validate_browser_url

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    assert validate_browser_url("https://localhost:8443/health") == "https://localhost:8443/health"
    with pytest.raises(ValueError):
        validate_browser_url("https://127.0.0.1:8443/health")


def test_browser_fill_form_rejects_partial_completion():
    from actions.browser_control import _BrowserThread

    class Element:
        async def clear(self):
            return None

        async def type(self, *_args, **_kwargs):
            raise RuntimeError("field unavailable")

    class Locator:
        def __init__(self, element):
            self.first = element

    class Page:
        def locator(self, _selector):
            return Locator(Element())

    thread = _BrowserThread()
    thread._get_page = lambda: None

    async def fake_get_page():
        return Page()

    thread._get_page = fake_get_page
    result = asyncio.run(thread._fill_form({"#name": "Alice"}))
    assert result.startswith("Form fill failed:")
    assert "Form filled:" not in result
