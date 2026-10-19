from __future__ import annotations

import base64
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.portable_intelligence.contracts import (
    BudgetExceeded, FunctionTool, ModelReply, ModelRequest, ProviderFailure,
    Route, RunPolicy, ToolCall,
)
from core.portable_intelligence.engine import ReasoningEngine
from core.portable_intelligence.evaluation import BenchmarkCase, EvaluationHarness
from core.portable_intelligence.providers import (
    AnthropicMessagesProvider, CallableProvider, OpenAICompatibleProvider,
    OpenAIResponsesProvider, validate_base_url,
)
from core.portable_intelligence.voice import (
    BoundedPlaybackBuffer, GPTLiveVoiceAdapter, OpenAIRealtimeVoiceAdapter, VoicePolicy,
)


class FakeProvider:
    capabilities = frozenset({"text", "tools"})

    def __init__(self, provider_id: str, answer: str, fail: Exception | None = None) -> None:
        self.provider_id = provider_id
        self.answer = answer
        self.fail = fail
        self.calls = []

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        self.calls.append((request, model))
        if self.fail:
            error, self.fail = self.fail, None
            raise error
        if "final synthesis" in request.system.casefold():
            return ModelReply("final synthesized answer", self.provider_id, model)
        if "Audit the following independent answers" in " ".join(
            str(item.get("content", "")) for item in request.messages
        ) and self.answer.startswith("critic"):
            return ModelReply(self.answer, self.provider_id, model)
        return ModelReply(self.answer, self.provider_id, model)


def make_route(name: str, model: str, priority: int = 1, *, priced: bool = False) -> Route:
    return Route(
        name, model, priority=priority, capabilities=frozenset({"text", "tools"}),
        input_usd_per_million=2.0 if priced else None,
        output_usd_per_million=8.0 if priced else None,
    )


class PortableContractsTests(unittest.TestCase):
    def test_provider_url_requires_tls_except_loopback(self):
        self.assertEqual(validate_base_url("http://127.0.0.1:20128/v1"), "http://127.0.0.1:20128/v1")
        self.assertTrue(validate_base_url("https://gateway.example/v1").startswith("https://"))
        for url in (
            "http://gateway.example/v1",
            "https://user:secret@gateway.example/v1",
            "file:///tmp/model",
            "https://gateway.example/v1?api_key=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_base_url(url)

    def test_hard_cost_cap_refuses_routes_without_configured_prices(self):
        engine = ReasoningEngine(
            {"one": FakeProvider("one", "answer")}, [make_route("one", "model")],
            policy=RunPolicy(strategy="single", max_cost_usd=0.01),
        )
        with self.assertRaises(BudgetExceeded):
            engine.answer("hello")

    def test_tool_write_requires_explicit_authorization(self):
        executed = []
        tool = FunctionTool(
            "write_file", "Write a file",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            executor=lambda args: executed.append(dict(args)) or "done",
            risk="write", requires_approval=True,
        )
        provider = FakeProvider("one", "unused")
        provider.complete = Mock(side_effect=[
            ModelReply("", "one", "model", tool_calls=(ToolCall("call-1", "write_file", {"path": "file.txt"}),)),
            ModelReply("I could not make that change without approval.", "one", "model"),
        ])
        engine = ReasoningEngine({"one": provider}, [make_route("one", "model")])
        result = engine.answer("write a file", tools=[tool])
        self.assertEqual(executed, [])
        self.assertIn("approval", result.text)
        self.assertEqual(provider.complete.call_count, 2)

    def test_explicit_authorizer_allows_registered_tool(self):
        executed = []
        tool = FunctionTool(
            "read_value", "Read a value", {"type": "object", "properties": {}},
            executor=lambda _args: executed.append(True) or "value", risk="read",
        )
        provider = FakeProvider("one", "unused")
        provider.complete = Mock(side_effect=[
            ModelReply("", "one", "model", tool_calls=(ToolCall("c1", "read_value", {}),)),
            ModelReply("Done.", "one", "model"),
        ])
        engine = ReasoningEngine({"one": provider}, [make_route("one", "model")])
        result = engine.answer("read it", tools=[tool], authorize_tool=lambda _tool, _args: True)
        self.assertEqual(executed, [True])
        self.assertEqual(result.text, "Done.")

    def test_single_route_falls_back_after_retryable_provider_failure(self):
        first = FakeProvider("first", "", ProviderFailure("first", "rate_limited", retryable=True))
        second = FakeProvider("second", "fallback answer")
        engine = ReasoningEngine(
            {"first": first, "second": second},
            [make_route("first", "m1", 1), make_route("second", "m2", 2)],
            policy=RunPolicy(strategy="single", retry_attempts=1),
        )
        result = engine.answer("question")
        self.assertEqual(result.text, "fallback answer")
        self.assertTrue(any(not trace.ok for trace in result.traces))
        self.assertEqual(result.selected_routes, ("second/m2",))

    def test_complex_ensemble_is_bounded_and_synthesizes(self):
        a, b, c = FakeProvider("a", "candidate A"), FakeProvider("b", "candidate B"), FakeProvider("c", "critic review")
        engine = ReasoningEngine(
            {"a": a, "b": b, "c": c},
            [make_route("a", "ma", 1), make_route("b", "mb", 2), make_route("c", "mc", 3)],
            policy=RunPolicy(strategy="ensemble", max_candidate_calls=2, max_critic_calls=1, max_total_model_calls=4),
        )
        result = engine.answer("compare two options", strategy="ensemble")
        self.assertEqual(result.strategy, "ensemble")
        self.assertLessEqual(len(result.traces), 4)
        self.assertEqual(result.text, "final synthesized answer")
        self.assertTrue(any(trace.stage == "critic" for trace in result.traces))

    def test_model_request_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            ModelRequest(messages=({"role": "user", "content": "x"},), max_output_tokens=0)
        with self.assertRaises(ValueError):
            RunPolicy(max_total_model_calls=0)

    def test_compat_chat_adapter_extracts_tool_calls(self):
        provider = OpenAICompatibleProvider("test-key", "http://127.0.0.1:20128/v1")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "id": "r1", "model": "test",
            "choices": [{"message": {"content": "", "tool_calls": [{
                "id": "call1", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"}},
            ]}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
        provider._session = Mock()
        provider._session.post.return_value = response
        tool = FunctionTool(
            "lookup", "Lookup", {"type": "object", "properties": {"q": {"type": "string"}}},
            lambda _args: "ok",
        )
        reply = provider.complete(
            ModelRequest(messages=({"role": "user", "content": "look"},), tools=(tool,)),
            "test-model",
        )
        self.assertEqual(reply.tool_calls, (ToolCall("call1", "lookup", {"q": "x"}),))
        provider._session.post.assert_called_once()
        self.assertFalse(provider._session.post.call_args.kwargs["allow_redirects"])

    def test_chat_completions_adapter_omits_temperature_for_reasoning_effort(self):
        provider = OpenAICompatibleProvider("test-key", "http://127.0.0.1:20128/v1")
        provider._post = Mock(return_value={"choices": [{"message": {"content": "ok"}}]})
        provider.complete(
            ModelRequest(messages=({"role": "user", "content": "solve"},), reasoning_effort="high"),
            "model",
        )
        payload = provider._post.call_args.args[1]
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("temperature", payload)

    def test_callable_provider_adapts_existing_host_llm_client(self):
        seen = []
        provider = CallableProvider(
            "abacus_custom_llm",
            lambda request, model: seen.append((request, model)) or "host answer",
        )
        request = ModelRequest(messages=({"role": "user", "content": "hello"},))
        reply = provider.complete(request, "custom-model")
        self.assertEqual(reply.text, "host answer")
        self.assertEqual(reply.provider_id, "abacus_custom_llm")
        self.assertEqual(seen, [(request, "custom-model")])

    def test_anthropic_adapter_does_not_send_api_key_as_bearer(self):
        provider = AnthropicMessagesProvider("anthropic-test-key")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"content": [{"type": "text", "text": "ok"}], "usage": {}}
        provider._session = Mock()
        provider._session.post.return_value = response
        provider._post("https://api.anthropic.com/v1/messages", {"messages": []}, {
            "x-api-key": provider.api_key, "anthropic-version": "2023-06-01",
        })
        headers = provider._session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["x-api-key"], "anthropic-test-key")
        self.assertNotIn("Authorization", headers)

    def test_gpt6_astra_tool_calling_refuses_compatible_chat_endpoint(self):
        provider = OpenAICompatibleProvider("test-key", "http://127.0.0.1:20128/v1")
        tool = FunctionTool("lookup", "Lookup", {"type": "object", "properties": {}}, lambda _args: "ok")
        with self.assertRaises(ProviderFailure) as raised:
            provider.complete(
                ModelRequest(messages=({"role": "user", "content": "look"},), tools=(tool,)),
                "openai/gpt-6-astra",
            )
        self.assertEqual(raised.exception.category, "native_responses_required")

    def test_responses_adapter_emits_native_function_call_and_reasoning_effort(self):
        provider = OpenAIResponsesProvider("test-key")
        provider._post = Mock(return_value={
            "id": "resp-1", "model": "gpt-6-astra",
            "output": [{"type": "function_call", "call_id": "call-9", "name": "lookup",
                        "arguments": "{\"q\":\"abc\"}"}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        })
        tool = FunctionTool("lookup", "Lookup", {"type": "object", "properties": {}}, lambda _args: "ok")
        reply = provider.complete(
            ModelRequest(messages=({"role": "user", "content": "do lookup"},), tools=(tool,), reasoning_effort="high"),
            "gpt-6-astra",
        )
        self.assertEqual(reply.tool_calls[0].name, "lookup")
        payload = provider._post.call_args.args[1]
        self.assertEqual(payload["reasoning"]["effort"], "high")
        self.assertEqual(payload["tools"][0]["name"], "lookup")
        self.assertNotIn("temperature", payload)

    def test_bounded_voice_buffer_drops_oldest_and_clears_stale_audio_on_interrupt(self):
        audio = BoundedPlaybackBuffer(max_chunks=2)
        generation = audio.generation
        self.assertTrue(audio.push(generation, b"a"))
        self.assertTrue(audio.push(generation, b"b"))
        self.assertTrue(audio.push(generation, b"c"))
        self.assertEqual(audio.dropped_chunks, 1)
        self.assertEqual(audio.pop(), b"b")
        next_generation = audio.interrupt()
        self.assertFalse(audio.push(generation, b"stale"))
        self.assertTrue(audio.push(next_generation, b"fresh"))
        self.assertEqual(audio.pop(), b"fresh")

    def test_realtime_voice_adapter_sends_pcm_and_text_without_touching_hardware(self):
        class Socket:
            def __init__(self):
                self.sent = []
            def send(self, value):
                self.sent.append(json.loads(value))
            def settimeout(self, _value):
                pass
            def close(self):
                pass
        adapter = OpenAIRealtimeVoiceAdapter("key", websocket_url="wss://api.openai.com/v1/realtime")
        sock = Socket()
        adapter._socket = sock
        adapter.send_pcm16(b"\x01\x02")
        adapter.send_text("hello")
        self.assertEqual(sock.sent[0]["type"], "input_audio_buffer.append")
        self.assertEqual(sock.sent[1]["type"], "conversation.item.create")
        self.assertEqual(sock.sent[2]["type"], "response.create")
        adapter.close()

    def test_gpt_live_rejects_non_websocket_endpoint(self):
        with self.assertRaises(ValueError):
            GPTLiveVoiceAdapter("test-key", websocket_url="https://api.openai.com/v1/live/sessions")

    def test_gpt_live_uses_client_delegation_and_returns_delegations_to_host(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.incoming = [
                    json.dumps({"type": "session.started", "session": {"id": "live-1"}}),
                    json.dumps({"type": "session.delegation.created", "delegation": {"id": "delegation-7", "target": "client"}}),
                    json.dumps({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x01\x02").decode("ascii")}),
                ]
            def send(self, value):
                self.sent.append(json.loads(value))
            def settimeout(self, _value):
                pass
            def recv(self):
                if not self.incoming:
                    raise RuntimeError("no fake event left")
                return self.incoming.pop(0)
            def close(self):
                pass

        fake_socket = FakeSocket()
        fake_websocket = SimpleNamespace(
            create_connection=Mock(return_value=fake_socket),
            WebSocketTimeoutException=type("WebSocketTimeoutException", (Exception,), {}),
        )
        with patch.dict(sys.modules, {"websocket": fake_websocket}):
            adapter = GPTLiveVoiceAdapter("test-key")
            adapter.connect()
            self.assertEqual(adapter.receive_event().kind, "session_state")
            delegation = adapter.receive_event()
            self.assertEqual(delegation.kind, "delegation")
            self.assertEqual(delegation.payload["delegation_id"], "delegation-7")
            audio = adapter.receive_event()
            self.assertEqual(audio.kind, "audio_delta")
            self.assertEqual(audio.payload["pcm16"], b"\x01\x02")
            adapter.append_delegation_result("delegation-7", "Verified result")
            adapter.send_pcm16(b"\x03\x04")
            sent = fake_socket.sent
            self.assertEqual(sent[0]["type"], "session.start")
            self.assertEqual(sent[0]["session"]["model"], "gpt-live-1")
            self.assertEqual(sent[0]["session"]["delegation"]["type"], "client")
            self.assertEqual(sent[-2]["type"], "session.commentary.append")
            self.assertEqual(sent[-2]["delegation_id"], "delegation-7")
            self.assertEqual(sent[-1]["type"], "session.input_audio.append")
            with self.assertRaises(ValueError):
                adapter.send_pcm16(b"\x01")
            adapter.close()

    def test_voice_policy_enforces_realtime_chunking(self):
        self.assertEqual(VoicePolicy(chunk_ms=20).chunk_ms, 20)
        with self.assertRaises(ValueError):
            VoicePolicy(chunk_ms=5)

    def test_evaluation_harness_reports_objective_string_checks(self):
        provider = FakeProvider("one", "The answer is 42; I am uncertain about the rest.")
        engine = ReasoningEngine({"one": provider}, [make_route("one", "model")])
        results = EvaluationHarness(engine).run([BenchmarkCase(
            "arith-1", "reasoning", "what is the answer?",
            required_phrases=("42",), forbidden_phrases=("guaranteed",),
        )])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)


if __name__ == "__main__":
    unittest.main()
