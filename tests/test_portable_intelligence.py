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
from core.portable_intelligence.key_pool import (
    CredentialPoolProvider, CredentialPoolState, CredentialSpec,
    build_credential_pools, load_credentials_from_environment,
    load_credentials_from_sources, merge_credential_specs,
)
from core.portable_intelligence.evaluation import (
    BenchmarkCase, EvaluationHarness, build_pairwise_judge_request,
    judge_pair_blindly, load_benchmark_cases,
)
from core.portable_intelligence.providers import (
    AnthropicMessagesProvider, CallableProvider, OpenAICompatibleProvider,
    OpenAIResponsesProvider, _failure_details, validate_base_url,
)
from core.portable_intelligence.voice import (
    BoundedPlaybackBuffer, GPTLiveVoiceAdapter, LiveDelegationCoordinator,
    OpenAIRealtimeVoiceAdapter, VoiceEvent, VoicePolicy,
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

    def test_chat_completions_adapter_omits_temperature_for_gpt6_text_requests(self):
        provider = OpenAICompatibleProvider("test-key", "http://127.0.0.1:20128/v1")
        provider._post = Mock(return_value={"choices": [{"message": {"content": "ok"}}]})
        provider.complete(
            ModelRequest(messages=({"role": "user", "content": "solve"},)),
            "openai/gpt-6-astra",
        )
        self.assertNotIn("temperature", provider._post.call_args.args[1])

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

    def test_responses_adapter_omits_temperature_for_gpt6_astra_without_explicit_effort(self):
        provider = OpenAIResponsesProvider("test-key")
        provider._post = Mock(return_value={
            "id": "resp-2", "model": "gpt-6-astra",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
        })
        provider.complete(
            ModelRequest(messages=({"role": "user", "content": "answer"},)),
            "gpt-6-astra",
        )
        self.assertNotIn("temperature", provider._post.call_args.args[1])

    def test_responses_adapter_rejects_gpt6_astra_none_and_minimal_reasoning(self):
        provider = OpenAIResponsesProvider("test-key")
        for effort in ("none", "minimal"):
            with self.subTest(effort=effort), self.assertRaises(ProviderFailure) as raised:
                provider.complete(
                    ModelRequest(messages=({"role": "user", "content": "answer"},), reasoning_effort=effort),
                    "gpt-6-astra",
                )
            self.assertEqual(raised.exception.category, "unsupported_reasoning_effort")

    def test_gpt6_luna_can_use_none_reasoning_and_sampling(self):
        provider = OpenAIResponsesProvider("test-key")
        provider._post = Mock(return_value={
            "id": "resp-luna", "model": "gpt-6-luna",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
        })
        provider.complete(
            ModelRequest(messages=({"role": "user", "content": "answer"},), reasoning_effort="none", temperature=0.7),
            "gpt-6-luna",
        )
        payload = provider._post.call_args.args[1]
        self.assertEqual(payload["reasoning"]["effort"], "none")
        self.assertEqual(payload["temperature"], 0.7)

    def test_gpt6_sol_chat_tools_allow_only_none_effort(self):
        provider = OpenAICompatibleProvider("test-key", "http://127.0.0.1:20128/v1")
        tool = FunctionTool("lookup", "Lookup", {"type": "object", "properties": {}}, lambda _args: "ok")
        with self.assertRaises(ProviderFailure) as raised:
            provider.complete(
                ModelRequest(messages=({"role": "user", "content": "look"},), tools=(tool,), reasoning_effort="high"),
                "gpt-6-sol",
            )
        self.assertEqual(raised.exception.category, "chat_tool_calling_requires_none")

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

    def test_gpt_live_rejects_non_websocket_endpoint_and_mismatched_audio_rates(self):
        with self.assertRaises(ValueError):
            GPTLiveVoiceAdapter("test-key", websocket_url="https://api.openai.com/v1/live/sessions")
        with self.assertRaises(ValueError):
            GPTLiveVoiceAdapter("test-key", policy=VoicePolicy(input_sample_rate=16000, output_sample_rate=24000))

    def test_gpt_live_uses_client_delegation_and_returns_delegations_to_host(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.incoming = [
                    json.dumps({"type": "session.started", "session": {"id": "live-1"}}),
                    json.dumps({"type": "session.delegation.created", "delegation": {"id": "delegation-7", "target": "client"}}),
                    json.dumps({"type": "session.output_audio.delta", "delta": base64.b64encode(b"\x01\x02").decode("ascii")}),
                    json.dumps({"type": "session.closed", "usage": {"seconds": 4}}),
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
            self.assertEqual(audio.payload["byte_count"], 2)
            self.assertTrue(audio.payload["queued"])
            self.assertEqual(adapter.playback.pop(), b"\x01\x02")
            with self.assertRaises(ValueError):
                adapter.append_delegation_result("unknown-delegation", "Not allowed")
            adapter.append_delegation_result("delegation-7", "Verified result")
            adapter.append_instruction("Keep the answer concise.")
            adapter.send_pcm16(b"\x03\x04")
            sent = fake_socket.sent
            self.assertEqual(sent[0]["type"], "session.start")
            self.assertEqual(sent[0]["session"]["model"], "gpt-live-1")
            self.assertEqual(sent[0]["session"]["delegation"]["type"], "client")
            self.assertEqual(sent[-3]["type"], "session.commentary.append")
            self.assertEqual(sent[-3]["delegation_id"], "delegation-7")
            self.assertEqual(sent[-2]["type"], "session.instructions.append")
            self.assertIsNone(sent[-2]["delegation_id"])
            self.assertEqual(sent[-1]["type"], "session.input_audio.append")
            with self.assertRaises(ValueError):
                adapter.send_pcm16(b"\x01")
            closed = adapter.receive_event()
            self.assertEqual(closed.kind, "session_closed")
            self.assertTrue(adapter.session_closed_confirmed)
            self.assertIsNone(adapter.finalization_error)
            self.assertEqual(adapter.last_usage["seconds"], 4)
            adapter.close()


    def test_live_delegation_coordinator_builds_context_and_delivers_once(self):
        coordinator = LiveDelegationCoordinator()
        coordinator.observe(VoiceEvent("transcript_delta", {
            "text": "Can you check my schedule? ",
            "source_type": "session.input_transcript.delta",
            "start_ms": 0, "end_ms": 500,
        }))
        coordinator.observe(VoiceEvent("transcript_delta", {
            "text": "I meant tomorrow, not today.",
            "source_type": "session.input_transcript.delta",
            "start_ms": 510, "end_ms": 900,
        }))
        task_id = coordinator.observe(VoiceEvent("delegation", {
            "delegation_id": "task-live-1", "target": "client", "offset_ms": 900,
        }))
        self.assertEqual(task_id, "task-live-1")
        self.assertIsNone(coordinator.observe(VoiceEvent("delegation", {
            "delegation_id": "task-live-1", "target": "client", "offset_ms": 900,
        })))
        context = coordinator.context_for(
            task_id, application_context="Selected calendar: personal"
        )
        self.assertIn("tomorrow, not today", context)
        self.assertIn("Selected calendar: personal", context)

        provider = FakeProvider("one", "Your calendar is clear tomorrow.")
        engine = ReasoningEngine({"one": provider}, [make_route("one", "model")])

        class Sink:
            def __init__(self):
                self.results = []
            def append_delegation_result(self, delegation_id, content, *, spoken=True):
                self.results.append((delegation_id, content, spoken))

        sink = Sink()
        result = coordinator.run_delegation(
            task_id, engine, sink, application_context="Selected calendar: personal"
        )
        self.assertEqual(result.text, "Your calendar is clear tomorrow.")
        self.assertEqual(coordinator.status(task_id), "delivered")
        self.assertEqual(len(sink.results), 1)
        with self.assertRaises(RuntimeError):
            coordinator.run_delegation(task_id, engine, sink)

    def test_live_delegation_delivery_retry_does_not_repeat_backend_call(self):
        coordinator = LiveDelegationCoordinator()
        coordinator.observe(VoiceEvent("transcript_delta", {
            "text": "Summarize the notes.",
            "source_type": "session.input_transcript.delta",
            "start_ms": 0, "end_ms": 200,
        }))
        task_id = coordinator.observe(VoiceEvent("delegation", {
            "delegation_id": "task-live-2", "target": "client", "offset_ms": 200,
        }))
        provider = FakeProvider("one", "Notes summarized.")
        engine = ReasoningEngine({"one": provider}, [make_route("one", "model")])

        class FailsOnceSink:
            def __init__(self):
                self.calls = 0
                self.results = []
            def append_delegation_result(self, delegation_id, content, *, spoken=True):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("socket is disconnected")
                self.results.append((delegation_id, content, spoken))

        sink = FailsOnceSink()
        with self.assertRaises(RuntimeError):
            coordinator.run_delegation(task_id, engine, sink)
        self.assertEqual(coordinator.status(task_id), "result_ready")
        self.assertEqual(len(provider.calls), 1)
        coordinator.deliver_result(task_id, sink)
        self.assertEqual(coordinator.status(task_id), "delivered")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(sink.results[0][1], "Notes summarized.")

    def test_live_delegation_ignores_non_client_and_bounds_transcript(self):
        coordinator = LiveDelegationCoordinator(max_segments=2, max_transcript_chars=1000)
        self.assertIsNone(coordinator.observe(VoiceEvent("delegation", {
            "delegation_id": "responses-owned", "target": "responses",
        })))
        coordinator.observe(VoiceEvent("transcript_delta", {
            "text": "A", "source_type": "session.input_transcript.delta",
            "start_ms": 0, "end_ms": 1,
        }))
        coordinator.observe(VoiceEvent("transcript_delta", {
            "text": "B", "source_type": "session.output_transcript.delta",
            "start_ms": 2, "end_ms": 3,
        }))
        coordinator.observe(VoiceEvent("transcript_delta", {
            "text": "C", "source_type": "session.input_transcript.delta",
            "start_ms": 4, "end_ms": 5,
        }))
        self.assertLessEqual(len(coordinator.transcript_snapshot()), 2)

    def test_gpt_live_close_reports_unconfirmed_usage_when_terminal_event_is_missing(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
                self.incoming = [json.dumps({"type": "session.started", "session": {"id": "live-2"}})]
            def send(self, value):
                self.sent.append(json.loads(value))
            def settimeout(self, _value):
                pass
            def recv(self):
                if self.incoming:
                    return self.incoming.pop(0)
                raise RuntimeError("socket ended before final usage")
            def close(self):
                pass

        fake_socket = FakeSocket()
        fake_websocket = SimpleNamespace(
            create_connection=Mock(return_value=fake_socket),
            WebSocketTimeoutException=type("WebSocketTimeoutException", (Exception,), {}),
        )
        with patch.dict(sys.modules, {"websocket": fake_websocket}):
            adapter = GPTLiveVoiceAdapter(
                "test-key", policy=VoicePolicy(close_timeout_seconds=0.05)
            )
            adapter.connect()
            adapter.close()
            self.assertFalse(adapter.session_closed_confirmed)
            self.assertIn("session.closed was not received", adapter.finalization_error)

    def test_voice_policy_enforces_realtime_chunking_and_timeouts(self):
        self.assertEqual(VoicePolicy(chunk_ms=20).chunk_ms, 20)
        with self.assertRaises(ValueError):
            VoicePolicy(chunk_ms=5)
        with self.assertRaises(ValueError):
            VoicePolicy(close_timeout_seconds=0)

    def test_evaluation_harness_reports_objective_string_checks(self):
        provider = FakeProvider("one", "The answer is 42; I am uncertain about the rest.")
        engine = ReasoningEngine({"one": provider}, [make_route("one", "model")])
        results = EvaluationHarness(engine).run([BenchmarkCase(
            "arith-1", "reasoning", "what is the answer?",
            required_phrases=("42",), forbidden_phrases=("guaranteed",),
        )])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)
        self.assertEqual(results[0].selected_routes, ("one/model",))
        self.assertEqual(results[0].model_call_count, 1)
        self.assertIn("42", results[0].answer_preview)
        self.assertTrue(results[0].trace_summary[0]["ok"])

    def test_benchmark_corpus_loads_unique_case_ids(self):
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "benchmarks" / "portable_intelligence_cases.json"
        cases = load_benchmark_cases(path)
        self.assertGreaterEqual(len(cases), 6)
        self.assertEqual(len({case.case_id for case in cases}), len(cases))



    def test_credential_pool_deduplicates_identical_keys_and_never_discloses_secrets(self):
        secret = "fake-secret-should-never-appear"
        first = CredentialSpec("gemini-a", "gemini_generate_content", secret,
                               "gemini-project-main", "https://generativelanguage.googleapis.com/v1beta")
        duplicate = CredentialSpec("gemini-alias", "gemini_generate_content", secret,
                                   "gemini-project-main", "https://generativelanguage.googleapis.com/v1beta",
                                   source_label="omniroute-export")
        pool = CredentialPoolProvider(
            "gemini-pool", [first, duplicate],
            adapter_factory=lambda spec: FakeProvider("fake", "ok"),
        )
        snapshot = pool.snapshot()
        self.assertEqual(snapshot["credential_count"], 1)
        self.assertEqual(snapshot["credentials"][0]["duplicate_aliases_collapsed"], 1)
        self.assertNotIn(secret, repr(first))
        self.assertNotIn(secret, repr(snapshot))
        reply = pool.complete(ModelRequest(messages=({"role": "user", "content": "hi"},)), "gemini-model")
        self.assertEqual(reply.text, "ok")
        self.assertEqual(reply.metadata["credential_id"], "gemini-a")
        self.assertNotIn(secret, repr(reply.metadata))

    def test_credential_pool_rotates_after_auth_failure(self):
        records = [
            CredentialSpec("bad", "gemini_generate_content", "bad-key", "project-A",
                           "https://generativelanguage.googleapis.com/v1beta", priority=1),
            CredentialSpec("good", "gemini_generate_content", "good-key", "project-A",
                           "https://generativelanguage.googleapis.com/v1beta", priority=2),
        ]
        created = {}
        def factory(spec):
            provider = FakeProvider(spec.credential_id, "accepted")
            if spec.credential_id == "bad":
                provider.fail = ProviderFailure("fake", "authentication_or_permission", status_code=401)
            created[spec.credential_id] = provider
            return provider
        pool = CredentialPoolProvider("gemini-pool", records, adapter_factory=factory)
        reply = pool.complete(ModelRequest(messages=({"role": "user", "content": "hello"},)), "gemini-model")
        self.assertEqual(reply.text, "accepted")
        self.assertTrue(pool.snapshot()["credentials"][0]["disabled"])
        self.assertEqual(created["bad"].calls.__len__(), 1)
        self.assertEqual(created["good"].calls.__len__(), 1)

    def test_credential_pool_rate_limit_cools_shared_quota_not_sibling_keys(self):
        records = [
            CredentialSpec("g1", "gemini_generate_content", "key-1", "same-project",
                           "https://generativelanguage.googleapis.com/v1beta", priority=1),
            CredentialSpec("g2", "gemini_generate_content", "key-2", "same-project",
                           "https://generativelanguage.googleapis.com/v1beta", priority=2),
            CredentialSpec("g3", "gemini_generate_content", "key-3", "other-project",
                           "https://generativelanguage.googleapis.com/v1beta", priority=3),
        ]
        attempts = []
        def factory(spec):
            class Adapter(FakeProvider):
                def complete(self, request, model):
                    attempts.append(spec.credential_id)
                    if spec.credential_id == "g1":
                        raise ProviderFailure("fake", "rate_limited", status_code=429,
                                               retryable=True, retry_after_seconds=20)
                    return ModelReply("ok", spec.credential_id, model)
            return Adapter(spec.credential_id, "unused")
        pool = CredentialPoolProvider("gemini-pool", records, adapter_factory=factory)
        result = pool.complete(ModelRequest(messages=({"role": "user", "content": "hi"},)), "gemini-model")
        self.assertEqual(result.text, "ok")
        self.assertEqual(attempts, ["g1", "g3"])
        snap = pool.snapshot()
        shared = next(s for s in snap["credentials"] if s["credential_id"] == "g2")
        self.assertGreater(shared["shared_cooldown_remaining_seconds"], 0)

    def test_credential_pool_blocks_exhausted_quota_scope_and_tries_other_account(self):
        records = [
            CredentialSpec("limited", "openai_compatible", "k1", "router-account-A",
                           "https://openrouter.ai/api/v1", priority=1),
            CredentialSpec("other", "openai_compatible", "k2", "router-account-B",
                           "https://openrouter.ai/api/v1", priority=2),
        ]
        attempts = []
        def factory(spec):
            class Adapter(FakeProvider):
                def complete(self, request, model):
                    attempts.append(spec.credential_id)
                    if spec.credential_id == "limited":
                        raise ProviderFailure("fake", "quota_exhausted", status_code=429, retryable=False)
                    return ModelReply("other account worked", spec.credential_id, model)
            return Adapter(spec.credential_id, "unused")
        pool = CredentialPoolProvider("router-pool", records, adapter_factory=factory)
        reply = pool.complete(ModelRequest(messages=({"role": "user", "content": "hi"},)), "router/model")
        self.assertEqual(reply.text, "other account worked")
        self.assertEqual(attempts, ["limited", "other"])
        snapshot = pool.snapshot()
        self.assertTrue(next(s for s in snapshot["quota_scopes"].values())["blocked"])

    def test_environment_loader_skips_missing_keys_without_fabricating_secrets(self):
        result = load_credentials_from_environment([
            {"id": "gemini-one", "provider_family": "gemini_generate_content",
             "api_key_env": "GEMINI_API_KEY_01", "quota_group": "project-A"},
            {"id": "gemini-two", "provider_family": "gemini_generate_content",
             "api_key_env": "GEMINI_API_KEY_02", "quota_group": "project-A"},
        ], environ={"GEMINI_API_KEY_01": "not-a-real-secret"})
        self.assertEqual(len(result.credentials), 1)
        self.assertEqual(result.skipped[0]["credential_id"], "gemini-two")
        self.assertNotIn("not-a-real-secret", repr(result))

    def test_credential_pool_rejects_same_key_with_conflicting_quota_groups(self):
        credentials = [
            CredentialSpec("alias-one", "openai_compatible", "same-key", "account-A",
                           "https://openrouter.ai/api/v1"),
            CredentialSpec("alias-two", "openai_compatible", "same-key", "account-B",
                           "https://openrouter.ai/api/v1"),
        ]
        with self.assertRaises(ValueError):
            CredentialPoolProvider("router-pool", credentials)

    def test_ensemble_uses_diversity_group_not_duplicate_adapter_aliases(self):
        first = FakeProvider("direct-gemini", "answer A")
        duplicate = FakeProvider("gemini-via-gateway", "answer alias")
        independent = FakeProvider("anthropic", "answer B")
        first.diversity_group = "gemini-project-A"
        duplicate.diversity_group = "gemini-project-A"
        independent.diversity_group = "anthropic-account"
        engine = ReasoningEngine(
            {"direct": first, "gateway": duplicate, "anthropic": independent},
            [
                Route("direct", "gemini-model", priority=1, capabilities=frozenset({"text", "tools"})),
                Route("gateway", "gemini-model", priority=2, capabilities=frozenset({"text", "tools"})),
                Route("anthropic", "model", priority=3, capabilities=frozenset({"text", "tools"})),
            ],
            policy=RunPolicy(strategy="ensemble", max_candidate_calls=2, max_critic_calls=0, max_total_model_calls=3),
        )
        engine.answer("compare these", strategy="ensemble")
        # The first provider answers its candidate and can also be selected for
        # synthesis when no unused provider family remains; its alias is never called.
        self.assertEqual(len(first.calls), 2)
        self.assertEqual(len(duplicate.calls), 0)
        self.assertGreaterEqual(len(independent.calls), 1)



    def test_openrouter_key_cap_rotates_one_key_without_blocking_siblings(self):
        records = [
            CredentialSpec("capped", "openai_compatible", "key-capped", "router-main",
                           "https://openrouter.ai/api/v1", priority=1),
            CredentialSpec("usable", "openai_compatible", "key-usable", "router-main",
                           "https://openrouter.ai/api/v1", priority=2),
        ]
        calls = []
        def factory(spec):
            class Adapter(FakeProvider):
                def complete(self, request, model):
                    calls.append(spec.credential_id)
                    if spec.credential_id == "capped":
                        raise ProviderFailure("fake", "credential_limit_exhausted", status_code=402)
                    return ModelReply("usable key succeeded", spec.credential_id, model)
            return Adapter(spec.credential_id, "unused")
        pool = CredentialPoolProvider("router-pool", records, adapter_factory=factory)
        reply = pool.complete(ModelRequest(messages=({"role": "user", "content": "hi"},)), "model")
        self.assertEqual(reply.text, "usable key succeeded")
        self.assertEqual(calls, ["capped", "usable"])
        capped = next(row for row in pool.snapshot()["credentials"] if row["credential_id"] == "capped")
        self.assertTrue(capped["disabled"])
        self.assertFalse(pool.snapshot()["quota_scopes"]["router-main"]["blocked"])

    def test_openrouter_limit_source_classification_distinguishes_key_account_and_inflight(self):
        key_cap = Mock()
        key_cap.headers = {}
        key_cap.json.return_value = {
            "error": {"code": 402, "metadata": {
                "limit_source": "openrouter_key_limit", "resets_at": "2099-01-01T00:00:00Z"
            }}
        }
        kind, retryable, reset_after = _failure_details(key_cap, 402)
        self.assertEqual((kind, retryable), ("credential_limit_exhausted", False))
        self.assertGreater(reset_after, 0)

        account = Mock()
        account.headers = {}
        account.json.return_value = {
            "error": {"code": 402, "metadata": {"limit_source": "openrouter_credits"}}
        }
        self.assertEqual(_failure_details(account, 402), ("quota_exhausted", False, None))

        inflight = Mock()
        inflight.headers = {"Retry-After": "9"}
        inflight.json.return_value = {
            "error": {"code": 402, "metadata": {"limit_source": "openrouter_in_flight_budget"}}
        }
        self.assertEqual(_failure_details(inflight, 402), ("rate_limited", True, 9.0))

    def test_retry_after_and_hard_billing_quota_are_distinguished(self):
        rate_limited = Mock()
        rate_limited.headers = {"Retry-After": "12"}
        rate_limited.json.return_value = {"error": {"code": "rate_limit_exceeded"}}
        self.assertEqual(_failure_details(rate_limited, 429), ("rate_limited", True, 12.0))

        exhausted = Mock()
        exhausted.headers = {}
        exhausted.json.return_value = {"error": {"code": "insufficient_quota"}}
        self.assertEqual(_failure_details(exhausted, 429), ("quota_exhausted", False, None))

    def test_credential_pool_example_merges_abacus_and_omniroute_aliases(self):
        from pathlib import Path
        import hashlib
        path = Path(__file__).resolve().parents[1] / "benchmarks" / "credential_pool.example.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        env_names = {
            row["api_key_env"] for row in config["credentials"]
            if row.get("enabled", True)
        }
        fake_env = {name: "test-key-" + name for name in env_names}
        loaded = load_credentials_from_environment(config["credentials"], environ=fake_env)
        self.assertGreater(len(config["credentials"]), 0)
        self.assertEqual(len(loaded.credentials), sum(
            1 for row in config["credentials"]
            if row.get("enabled", True) and row.get("api_key_env") in fake_env
        ))
        pools = build_credential_pools(loaded.credentials)
        gemini_specs = [c for c in loaded.credentials if c.provider_family == "gemini_generate_content"]
        router_specs = [
            c for c in loaded.credentials
            if c.provider_family == "openai_compatible" and "openrouter.ai" in c.base_url
        ]
        gemini_pool = next(p for p in pools.values() if p.family == "gemini_generate_content")
        router_pool = next(p for p in pools.values()
                           if p.family == "openai_compatible" and "openrouter.ai" in p.base_url)
        self.assertEqual(
            gemini_pool.snapshot()["credential_count"],
            len({c.fingerprint for c in gemini_specs}),
        )
        self.assertEqual(
            router_pool.snapshot()["credential_count"],
            len({c.fingerprint for c in router_specs}),
        )
        self.assertEqual(
            sum(c["duplicate_aliases_collapsed"] for c in gemini_pool.snapshot()["credentials"]),
            len(gemini_specs) - len({c.fingerprint for c in gemini_specs}),
        )
        serialized = path.read_text(encoding="utf-8")
        self.assertNotIn("test-key-", serialized)
        self.assertNotIn("api_key\":", serialized)


    def test_gpt_live_uses_unique_event_ids_across_delegations_and_resets_on_reconnect(self):
        class Socket:
            def __init__(self, session_number):
                self.sent = []
                self.incoming = [json.dumps({
                    "type": "session.started",
                    "session": {"id": f"live-session-{session_number}"},
                })]
            def send(self, value):
                self.sent.append(json.loads(value))
            def settimeout(self, _value):
                pass
            def recv(self):
                if not self.incoming:
                    raise RuntimeError("unexpected receive")
                return self.incoming.pop(0)
            def close(self):
                pass

        first = Socket(1)
        second = Socket(2)
        fake_websocket = SimpleNamespace(
            create_connection=Mock(side_effect=[first, second]),
            WebSocketTimeoutException=type("WebSocketTimeoutException", (Exception,), {}),
        )
        with patch.dict(sys.modules, {"websocket": fake_websocket}):
            adapter = GPTLiveVoiceAdapter("test-key")
            adapter.connect()
            adapter.receive_event()  # consume the queued session.started event
            adapter._known_delegations.update({"delegation-a", "delegation-b"})
            adapter.append_delegation_result("delegation-a", "Same verified answer.")
            adapter.append_delegation_result("delegation-b", "Same verified answer.")
            result_events = [
                item for item in first.sent
                if item["type"] == "session.commentary.append"
            ]
            self.assertEqual(len(result_events), 2)
            self.assertNotEqual(result_events[0]["event_id"], result_events[1]["event_id"])
            first_nonce = adapter._session_nonce
            adapter.close()
            adapter.connect()
            self.assertNotEqual(first_nonce, adapter._session_nonce)
            self.assertFalse(adapter.session_closed_confirmed)
            with self.assertRaises(ValueError):
                adapter.append_delegation_result("delegation-a", "stale result")
            self.assertEqual(second.sent[0]["type"], "session.start")
            self.assertNotEqual(first.sent[0]["event_id"], second.sent[0]["event_id"])
            adapter.close()

    def test_gpt_live_long_delegation_is_split_and_retry_resumes_after_accepted_chunks(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.calls = 0
                self.fail_once_on_call = 2
            def send(self, value):
                self.calls += 1
                if self.calls == self.fail_once_on_call:
                    self.fail_once_on_call = -1
                    raise RuntimeError("simulated transient websocket send failure")
                self.sent.append(json.loads(value))

        adapter = GPTLiveVoiceAdapter("test-key")
        socket = Socket()
        adapter._socket = socket
        adapter._started = True
        adapter._known_delegations.add("long-result")
        text = ("Résumé 🙂; verified result. " * 100).strip()
        with self.assertRaises(RuntimeError):
            adapter.append_delegation_result("long-result", text)
        first_chunk = socket.sent[0]["content"]
        self.assertLessEqual(len(first_chunk.encode("utf-8")), 450)
        adapter.append_delegation_result("long-result", text)
        self.assertEqual("".join(item["content"] for item in socket.sent), text)
        self.assertTrue(all(len(item["content"].encode("utf-8")) <= 450 for item in socket.sent))
        total = len(socket.sent)
        adapter.append_delegation_result("long-result", text)
        self.assertEqual(len(socket.sent), total)
        self.assertNotIn("test-key", json.dumps(socket.sent))


    def test_multi_source_key_merge_scales_without_fixed_provider_key_count(self):
        # Intentionally use an arbitrary count here; the loader has no fixed key limit.
        env = {}
        abacus_rows = []
        gateway_rows = []
        for index in range(1, 14):
            variable = f"GEMINI_POOL_KEY_{index:02d}"
            env[variable] = f"fake-gemini-key-{index:02d}"
            row = {
                "id": f"gemini-{index:02d}",
                "provider_family": "gemini_generate_content",
                "api_key_env": variable,
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "quota_group": "same-google-project",
                "priority": index,
            }
            (abacus_rows if index % 2 else gateway_rows).append(row)
        loaded = load_credentials_from_sources(
            {"abacus": abacus_rows, "omniroute": gateway_rows},
            environ=env,
        )
        self.assertEqual(len(loaded.credentials), 13)
        specs = merge_credential_specs({
            "abacus": loaded.credentials[:6],
            "omniroute": loaded.credentials[6:],
        })
        self.assertEqual(len(specs), 13)
        pool = build_credential_pools(specs)
        gemini_pool = next(iter(pool.values()))
        self.assertEqual(gemini_pool.snapshot()["credential_count"], 13)
        self.assertEqual({c["source_label"] for c in gemini_pool.snapshot()["credentials"]},
                         {"abacus", "omniroute"})

    def test_source_merge_detects_same_secret_alias_across_sources(self):
        shared = CredentialSpec(
            "provider-key-1", "gemini_generate_content", "same-secret-value",
            "google-project-1", "https://generativelanguage.googleapis.com/v1beta",
        )
        merged = merge_credential_specs({
            "abacus": (shared,),
            "omniroute": (shared,),
        })
        self.assertEqual(len(merged), 2)
        pool = build_credential_pools(merged)
        gemini_pool = next(iter(pool.values()))
        self.assertEqual(gemini_pool.snapshot()["credential_count"], 1)
        self.assertEqual(gemini_pool.snapshot()["credentials"][0]["duplicate_aliases_collapsed"], 1)
        self.assertNotIn("same-secret-value", repr(gemini_pool.snapshot()))


    def test_custom_provider_family_uses_registered_adapter_factory(self):
        credential = CredentialSpec(
            "custom-a", "custom_vendor", "opaque-test-key", "custom-account",
            "https://llm.custom.example/v1",
        )
        built = []
        def factory(spec):
            built.append(spec.credential_id)
            return FakeProvider("custom", "custom adapter answer")
        with self.assertRaises(ValueError):
            CredentialPoolProvider("custom", [credential])
        pools = build_credential_pools(
            [credential],
            adapter_factories={"custom_vendor": factory},
            diversity_groups={"custom_vendor_" + "": "unused"},
        )
        pool = next(iter(pools.values()))
        reply = pool.complete(
            ModelRequest(messages=({"role": "user", "content": "hello"},)), "custom-model"
        )
        self.assertEqual(reply.text, "custom adapter answer")
        self.assertEqual(built, ["custom-a"])
        self.assertEqual(pool.family, "custom_vendor")
        self.assertEqual(pool.capabilities, frozenset({"text"}))
        capable_pools = build_credential_pools(
            [credential],
            adapter_factories={"custom_vendor": factory},
            capabilities_by_family={"custom_vendor": ("text", "tools")},
        )
        self.assertEqual(next(iter(capable_pools.values())).capabilities, frozenset({"text", "tools"}))


    def test_blind_comparison_loader_uses_many_key_pools_without_network_calls(self):
        from pathlib import Path
        from scripts.portable_intelligence_compare import _load_routes
        config_path = Path(__file__).resolve().parents[1] / "benchmarks" / "credential_pool.example.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        env = {}
        for row in config["credentials"]:
            env[row["api_key_env"]] = "fake-" + row["api_key_env"]
        env.update({
            "OPENAI_RESPONSES_MODEL": "gpt-6-astra",
            "ANTHROPIC_MODEL": "claude-test",
            "GEMINI_MODEL": "gemini-test",
            "OPENROUTER_MODEL": "openrouter/test",
            "MISTRAL_MODEL": "mistral-test",
            "OMNIROUTE_MODEL": "omniroute/auto",
        })
        with patch.dict("os.environ", env, clear=False):
            providers, routes, skipped = _load_routes(config_path)
        self.assertIn("openai_astra", routes)
        self.assertIn("anthropic_judge", routes)
        self.assertIn("gemini_candidate", routes)
        self.assertIn("openrouter_candidate", routes)
        self.assertIs(providers["openai_astra"], providers.get("openai_astra"))
        self.assertEqual(routes["openai_astra"].model, "gpt-6-astra")
        self.assertEqual(routes["omniroute_candidate"].diversity_group, "omniroute-auto")
        self.assertEqual(
            providers["gemini_candidate"].snapshot()["credential_count"],
            len({row["api_key_env"] for row in config["credentials"]
                 if row["provider_family"] == "gemini_generate_content"}),
        )
        self.assertIsInstance(skipped, dict)


    def test_pairwise_comparison_budget_counts_baseline_ensemble_and_judge(self):
        from scripts.portable_intelligence_compare import _comparison_call_limits
        self.assertEqual(_comparison_call_limits(6, 3, 4), (4, 3))
        self.assertEqual(_comparison_call_limits(6, 2, 4), (4, 2))
        self.assertEqual(_comparison_call_limits(5, 3, 2), (3, 2))
        for total_calls, max_candidates, route_count in (
            (4, 2, 2),
            (5, 1, 2),
            (5, 2, 1),
        ):
            with self.subTest(total_calls=total_calls, max_candidates=max_candidates, route_count=route_count):
                with self.assertRaises(ValueError):
                    _comparison_call_limits(total_calls, max_candidates, route_count)

    def test_pairwise_judge_request_is_blinded_and_contains_same_task(self):
        case = BenchmarkCase(
            "case-1", "reasoning", "What is 6 * 7?",
            required_phrases=("42",), forbidden_phrases=("41",),
        )
        request = build_pairwise_judge_request(case, "The answer is 42.", "The answer is 41.")
        payload = json.loads(request.messages[0]["content"])
        self.assertEqual(payload["task_prompt"], case.prompt)
        self.assertEqual(payload["candidate_a"], "The answer is 42.")
        self.assertEqual(payload["candidate_b"], "The answer is 41.")
        self.assertNotIn("openai", request.system.casefold())
        self.assertNotIn("openai", request.messages[0]["content"].casefold())

    def test_pairwise_judge_validates_rubric_and_returns_scores(self):
        provider = FakeProvider("judge", "")
        provider.complete = Mock(return_value=ModelReply(
            json.dumps({
                "scores_a": {"accuracy": 5, "instruction_following": 5, "completeness": 4, "calibration": 4, "safety": 5},
                "scores_b": {"accuracy": 2, "instruction_following": 4, "completeness": 3, "calibration": 2, "safety": 5},
                "winner": "A",
                "rationale": "A is correct; B has an arithmetic error.",
            }),
            "judge", "grader-model",
        ))
        case = BenchmarkCase("calc-1", "reasoning", "What is 6 * 7?")
        verdict = judge_pair_blindly(provider, "grader-model", case, "42", "41")
        self.assertEqual(verdict.winner, "A")
        self.assertEqual(verdict.scores_a["accuracy"], 5)
        self.assertEqual(verdict.scores_b["accuracy"], 2)
        self.assertIn("arithmetic", verdict.rationale)
        self.assertEqual(provider.complete.call_count, 1)

    def test_pairwise_judge_rejects_invalid_scores_and_winner(self):
        provider = FakeProvider("judge", "")
        provider.complete = Mock(return_value=ModelReply(
            '{"scores_a":{"accuracy":9},"scores_b":{},"winner":"X"}', "judge", "model"
        ))
        case = BenchmarkCase("calc-1", "reasoning", "question")
        with self.assertRaises(ValueError):
            judge_pair_blindly(provider, "model", case, "A", "B")




if __name__ == "__main__":
    unittest.main()
