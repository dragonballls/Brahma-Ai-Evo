from __future__ import annotations

from unittest.mock import Mock

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
