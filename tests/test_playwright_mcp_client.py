from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from actions.playwright_mcp_client import PlaywrightMCPClient


def _client() -> PlaywrightMCPClient:
    return PlaywrightMCPClient()


def test_playwright_mcp_empty_success_payload_is_not_reported_as_completion(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "call_tool", Mock(return_value={"success": True, "text": ""}))
    result = client.navigate("https://example.com")
    assert result.startswith("Error: Playwright MCP returned no authoritative completion result")


def test_playwright_mcp_explicit_rpc_error_propagates(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "call_tool", Mock(return_value={"success": False, "error": "request failed"}))
    assert client.click("button") == "request failed"


def test_playwright_mcp_authoritative_rpc_text_is_returned(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "call_tool", Mock(return_value={"success": True, "text": "Clicked button."}))
    assert client.click("button") == "Clicked button."


def test_playwright_mcp_initialize_timeout_closes_spawned_process(monkeypatch):
    from unittest.mock import Mock, patch

    client = _client()
    proc = Mock()
    proc.poll.return_value = None
    proc.stdout = None
    proc.stdin = Mock()
    close = Mock(return_value="Browser closed.")
    client.close = close
    monkeypatch.setattr(client, "_send_request", Mock(return_value=None))
    with patch("actions.playwright_mcp_client.subprocess.Popen", return_value=proc):
        assert client.start() is False
    close.assert_called_once()


def test_playwright_mcp_missing_tools_result_does_not_mark_client_ready(monkeypatch):
    from unittest.mock import Mock, patch

    client = _client()
    proc = Mock()
    proc.poll.return_value = None
    proc.stdout = None
    proc.stdin = Mock()
    close = Mock(return_value="Browser closed.")
    client.close = close
    monkeypatch.setattr(
        client,
        "_send_request",
        Mock(side_effect=[{"result": {}}, {}]),
    )
    with patch("actions.playwright_mcp_client.subprocess.Popen", return_value=proc):
        assert client.start() is False
    assert not client._is_ready.is_set()
    close.assert_called_once()


def test_playwright_mcp_screenshot_path_requires_real_artifact(monkeypatch, tmp_path: Path):
    client = _client()
    monkeypatch.setattr(
        client,
        "call_tool",
        Mock(return_value={
            "success": True,
            "text": "Screenshot saved to '/does/not/exist.png'.",
        }),
    )
    output = tmp_path / "verified.png"
    result = client.take_screenshot(str(output))
    assert result.startswith("Error: screenshot save was not completed or verified")
    assert not output.exists()
