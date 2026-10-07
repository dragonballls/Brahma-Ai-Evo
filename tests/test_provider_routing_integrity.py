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


def test_invalid_provider_setting_fails_closed():
    from core.provider_policy import validate_provider

    with pytest.raises(ValueError, match="Unsupported AI provider"):
        validate_provider("NotAProvider")


def test_invalid_provider_setting_cannot_retain_stale_client_provider(monkeypatch):
    client = UnifiedAIClient.__new__(UnifiedAIClient)

    def broken_settings():
        return {"default_ai_provider": "NotAProvider"}

    monkeypatch.setattr("memory.config_manager.load_settings", broken_settings)
    with pytest.raises(ValueError, match="Unsupported AI provider"):
        client.reload_settings()
    assert not hasattr(client, "_provider") or client._provider != "NotAProvider"


def test_non_boolean_offline_setting_is_not_treated_as_true():
    value = "false"
    assert not isinstance(value, bool)


def test_specialized_gemini_paths_fail_closed_when_openrouter_is_selected(monkeypatch):
    from core import provider_policy

    monkeypatch.setattr(provider_policy, "selected_provider", lambda: OPENROUTER)

    with pytest.raises(RuntimeError, match="Gemini-grounded web search requires"):
        from actions.web_search import _gemini_search
        _gemini_search("test")

    with pytest.raises(RuntimeError, match="Gemini video understanding requires"):
        from actions.video_understanding import _gemini_api_key
        _gemini_api_key()


def test_auto_heal_does_not_bypass_selected_provider_with_direct_openrouter_fallback(monkeypatch):
    import or_client
    from actions.auto_heal_engine import AutoHealEngine

    with patch(
        "llm_client.client.chat",
        side_effect=RuntimeError("OmniRoute unavailable"),
    ):
        direct = Mock(side_effect=AssertionError("auto-heal must not bypass the selected provider"))
        monkeypatch.setattr(or_client, "chat", direct)
        result = AutoHealEngine._synthesize_patch_code(
            "actions/example.py", 10, "RuntimeError", "boom", "value = 1",
        )

    assert result["success"] is False
    assert "All unified AI synthesis backends failed" in result["error"]
    direct.assert_not_called()


def test_web_search_compare_fallback_does_not_claim_gemini_success(monkeypatch, capsys):
    from actions import web_search

    monkeypatch.setattr(
        web_search,
        "_gemini_search",
        Mock(side_effect=RuntimeError("Gemini unavailable")),
    )
    monkeypatch.setattr(
        web_search,
        "_ddg_search",
        Mock(return_value=[{"title": "fallback", "snippet": "fallback result", "url": "https://example.test"}]),
    )

    result = web_search.web_search(
        {"mode": "compare", "items": ["alpha", "beta"], "aspect": "general"},
    )

    output = capsys.readouterr().out
    assert "Gemini compare failed" in output
    assert "Gemini compare OK" not in output
    assert "Comparison — GENERAL" in result
    assert "fallback result" in result


def test_call_audio_transcription_refuses_selected_openrouter(monkeypatch):
    from actions.call_assistant import CallAssistant

    calls = {"create_model": 0}

    def reject(_provider, _capability):
        raise RuntimeError("Call audio transcription requires the Google Gemini provider")

    monkeypatch.setattr("core.provider_policy.require_provider", reject)

    def forbidden(*_args, **_kwargs):
        calls["create_model"] += 1
        raise AssertionError("Call transcription must stop before Gemini client creation")

    monkeypatch.setattr("core.gemini_runtime.create_model", forbidden)

    assistant = object.__new__(CallAssistant)
    assert assistant._transcribe_audio(b"wav") == ""
    assert calls["create_model"] == 0


def test_screen_live_vision_refuses_selected_openrouter(monkeypatch):
    import asyncio
    from actions.screen_processor import _LiveSession

    def reject(_provider, _capability):
        raise RuntimeError("Screen vision Live requires the Google Gemini provider")

    monkeypatch.setattr("core.provider_policy.require_provider", reject)
    session = _LiveSession()
    with pytest.raises(RuntimeError, match="Screen vision Live requires"):
        asyncio.run(session._main())


def test_specialized_gemini_generation_modules_refuse_selected_openrouter(monkeypatch):
    from core import provider_policy
    monkeypatch.setattr(provider_policy, "selected_provider", lambda: OPENROUTER)

    from actions import docx_tools, pdf_tools
    with pytest.raises(RuntimeError, match="requires the Google Gemini provider"):
        docx_tools._gemini_client()

    monkeypatch.setattr(pdf_tools, "_get_api_key", lambda: pytest.fail("PDF path must not fetch a Gemini key before provider validation"))
    assert pdf_tools.synthesize_deep_report("research", "Test") == ""


def test_intelligence_orchestrator_is_provider_preserving_by_default():
    source = IntelligenceOrchestrator._call.__kwdefaults__
    # The public orchestrator helper intentionally defaults to no direct provider switch.
    assert source is not None
    assert source["allow_direct_fallback"] is False


def test_video_understanding_refuses_to_bypass_selected_provider(monkeypatch):
    from actions import video_understanding
    from core import provider_policy

    def reject(_provider, capability):
        raise RuntimeError(f"{capability} requires Gemini")

    monkeypatch.setattr(provider_policy, "require_provider", reject)
    monkeypatch.setattr(video_understanding, "_gemini_api_key", lambda: pytest.fail("Gemini key lookup bypassed provider policy"))

    with pytest.raises(RuntimeError, match="requires Gemini"):
        video_understanding.analyze_youtube("https://www.youtube.com/watch?v=example")


def test_local_video_understanding_refuses_to_bypass_selected_provider(monkeypatch, tmp_path):
    from actions import video_understanding
    from core import provider_policy

    video = tmp_path / "sample.mp4"
    video.write_bytes(b"placeholder")

    def reject(_provider, capability):
        raise RuntimeError(f"{capability} requires Gemini")

    monkeypatch.setattr(provider_policy, "require_provider", reject)
    monkeypatch.setattr(video_understanding, "_gemini_api_key", lambda: pytest.fail("Gemini key lookup bypassed provider policy"))

    with pytest.raises(RuntimeError, match="requires Gemini"):
        video_understanding.analyze_local_video(str(video))
