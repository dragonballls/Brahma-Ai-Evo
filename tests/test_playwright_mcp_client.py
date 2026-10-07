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
