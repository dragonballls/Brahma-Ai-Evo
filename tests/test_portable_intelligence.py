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
from core.portable_intelligence.evaluation import (
    BenchmarkCase, EvaluationHarness, build_pairwise_judge_request,
    judge_pair_blindly, load_benchmark_cases,
)
from core.portable_intelligence.providers import (
    AnthropicMessagesProvider, CallableProvider, OpenAICompatibleProvider,
    OpenAIResponsesProvider, validate_base_url,
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
