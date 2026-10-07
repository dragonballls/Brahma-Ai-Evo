from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from llm_client import UnifiedAIClient
from or_client import OpenRouterClient
from core.intelligence_orchestrator import IntelligenceOrchestrator
from core.provider_policy import GEMINI, OPENROUTER


def _strict_client() -> OpenRouterClient:
    client = object.__new__(OpenRouterClient)
    client._omniroute = Mock()
    return client


def test_provider_preserving_chat_refuses_direct_openrouter_fallback(monkeypatch):
    client = _strict_client()
    monkeypatch.setattr(client, "_call_omniroute", Mock(return_value=None))
    direct = Mock(side_effect=AssertionError("direct fallback must not run"))
    monkeypatch.setattr(client, "_call_with_fallback", direct)

    with pytest.raises(RuntimeError, match="automatic direct-provider fallback is disabled"):
        client.chat("hello", model="auto", allow_direct_fallback=False)

    direct.assert_not_called()


def test_provider_preserving_json_refuses_direct_openrouter_fallback(monkeypatch):
    client = _strict_client()
    monkeypatch.setattr(client, "_call_omniroute", Mock(return_value=None))
    direct = Mock(side_effect=AssertionError("direct fallback must not run"))
    monkeypatch.setattr(client, "_call_with_fallback", direct)

    with pytest.raises(RuntimeError, match="Provider-preserving structured request failed"):
        client.chat_json("return json", allow_direct_fallback=False)

    direct.assert_not_called()


def test_provider_preserving_vision_and_multiturn_refuse_direct_openrouter_fallback(monkeypatch):
    client = _strict_client()
    monkeypatch.setattr(client, "_call_omniroute", Mock(return_value=None))
    direct = Mock(side_effect=AssertionError("direct fallback must not run"))
    monkeypatch.setattr(client, "_call_with_fallback", direct)

    with pytest.raises(RuntimeError, match="Provider-preserving vision request failed"):
        client.vision("describe", "aW1hZ2U=", allow_direct_fallback=False)
    with pytest.raises(RuntimeError, match="Provider-preserving multi-turn request failed"):
        client.multi_turn([{"role": "user", "content": "hello"}], allow_direct_fallback=False)

    direct.assert_not_called()


def test_provider_preserving_tool_calls_refuse_direct_openrouter_fallback(monkeypatch):
    client = _strict_client()
    monkeypatch.setattr(client, "_call_omniroute_tool_capable", Mock(return_value=None))
    direct = Mock(side_effect=AssertionError("direct tool fallback must not run"))
    monkeypatch.setattr(client, "_call_tool_capable", direct)

    with pytest.raises(RuntimeError, match="automatic direct-provider fallback is disabled"):
        client.chat_with_tools(
            [{"role": "user", "content": "do it"}],
            [{"name": "safe", "description": "safe", "parameters": {"type": "object", "properties": {}}}],
            Mock(),
            allow_direct_fallback=False,
        )

    direct.assert_not_called()


def test_omniroute_request_pins_explicit_provider(monkeypatch):
    client = object.__new__(OpenRouterClient)
    client._omniroute = Mock()
    client._omniroute.ensure_ready.return_value = True
    client._omniroute.base_url = "http://127.0.0.1:8765"
    response = Mock()
    response.status_code = 200
    response.iter_content.return_value = [
        b'{"choices":[{"message":{"content":"provider-pinned"}}]}'
    ]
    with patch("or_client.requests.post", return_value=response) as post:
        result = client._call_omniroute(
            [{"role": "user", "content": "hello"}],
            model="auto",
            provider="Gemini",
        )
    assert result == "provider-pinned"
    headers = post.call_args.kwargs["headers"]
    assert headers["X-OmniRoute-Provider"] == "gemini"


def test_unified_client_provider_override_controls_omniroute_and_direct_fallback(monkeypatch):
    client = UnifiedAIClient.__new__(UnifiedAIClient)
    monkeypatch.setattr(client, "reload_settings", lambda: None)
    client._provider = GEMINI
    with patch("llm_client.openrouter_client.chat", return_value="OpenRouter answer") as routed:
        assert client.chat("hello", provider="OpenRouter") == "OpenRouter answer"
    assert routed.call_args.kwargs["provider"] == "openrouter"
    assert routed.call_args.kwargs["allow_direct_fallback"] is True

    with patch("llm_client.openrouter_client.chat", return_value="Gemini answer") as routed:
        assert client.chat("hello", provider="Gemini") == "Gemini answer"
    assert routed.call_args.kwargs["provider"] == "gemini"
    assert routed.call_args.kwargs["allow_direct_fallback"] is False


def test_unified_client_tool_provider_override_is_forwarded(monkeypatch):
    client = UnifiedAIClient.__new__(UnifiedAIClient)
    monkeypatch.setattr(client, "reload_settings", lambda: None)
    client._provider = GEMINI
    with patch("llm_client.openrouter_client.chat_with_tools", return_value="done") as routed:
        result = client.chat_with_tools(
            [{"role": "user", "content": "do it"}],
            [{"name": "safe", "description": "safe", "parameters": {"type": "object", "properties": {}}}],
            Mock(),
            provider="OpenRouter",
        )
    assert result == "done"
    assert routed.call_args.kwargs["provider"] == "openrouter"
    assert routed.call_args.kwargs["allow_direct_fallback"] is True


def test_unified_client_only_allows_direct_cloud_fallback_for_openrouter(monkeypatch):
    client = UnifiedAIClient.__new__(UnifiedAIClient)
    monkeypatch.setattr(client, "reload_settings", lambda: None)

    client._provider = GEMINI
    assert client._allow_direct_cloud_fallback() is False

    client._provider = OPENROUTER
    assert client._allow_direct_cloud_fallback() is True


def test_intelligence_orchestrator_is_provider_preserving_by_default():
    source = IntelligenceOrchestrator._call.__kwdefaults__
    # The public orchestrator helper intentionally defaults to no direct provider switch.
    assert source is not None
    assert source["allow_direct_fallback"] is False
