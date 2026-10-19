"""Bounded, evidence-oriented reasoning and host-authorized tool execution."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .contracts import BudgetExceeded, CallTrace, FunctionTool, ModelReply, ModelRequest, ProviderAdapter, ProviderFailure, Route, RunPolicy, TaskResult


@dataclass
class _Meter:
    policy: RunPolicy
    calls: int = 0
    estimated_spend: float = 0.0
    cost_unknown: bool = False
    traces: list[CallTrace] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve(self, route: Route, request: ModelRequest) -> float | None:
        estimate = route.estimate_cost(request)
        with self.lock:
            if self.calls >= self.policy.max_total_model_calls:
                raise BudgetExceeded("Maximum model-call budget reached.")
            if self.policy.max_cost_usd is not None:
                if estimate is None:
                    raise BudgetExceeded(
                        f"Route {route.provider_id}/{route.model} has no configured price; "
                        "a hard spend budget cannot safely estimate its cost."
                    )
                if self.estimated_spend + estimate > self.policy.max_cost_usd:
                    raise BudgetExceeded("Estimated spend would exceed the configured hard budget.")
            self.calls += 1
            if estimate is None:
                self.cost_unknown = True
            else:
                self.estimated_spend += estimate
        return estimate

    def add_trace(self, trace: CallTrace) -> None:
        with self.lock:
            self.traces.append(trace)


class ReasoningEngine:
    """Provider-neutral engine. Host applications provide settings and credentials."""

    def __init__(self, providers: Mapping[str, ProviderAdapter], routes: Sequence[Route], *,
                 policy: RunPolicy | None = None) -> None:
        self.providers = dict(providers)
        self.routes = tuple(sorted(routes, key=lambda item: item.priority))
        self.policy = policy or RunPolicy()
        if not self.routes:
            raise ValueError("At least one model route is required.")
        missing = {route.provider_id for route in self.routes} - set(self.providers)
        if missing:
            raise ValueError(f"Routes reference unregistered providers: {', '.join(sorted(missing))}")
        identities = [(route.provider_id, route.model) for route in self.routes]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate provider/model routes are not allowed.")

    def _eligible(self, *, capability: str = "text", tools: bool = False) -> list[Route]:
        required = {capability}
        if tools:
            required.add("tools")
        return [
            route for route in self.routes
            if required.issubset(route.capabilities)
            and required.issubset(getattr(self.providers[route.provider_id], "capabilities", frozenset()))
        ]

    @staticmethod
    def _label(route: Route) -> str:
        return f"{route.provider_id}/{route.model}"

    def _call_one(self, route: Route, request: ModelRequest, meter: _Meter, stage: str) -> ModelReply:
        estimate = meter.reserve(route, request)
        started = time.monotonic()
        try:
            reply = self.providers[route.provider_id].complete(request, route.model)
            if not isinstance(reply, ModelReply):
                raise ProviderFailure(route.provider_id, "adapter_contract_violation")
            meter.add_trace(CallTrace(
                route.provider_id, route.model, stage,
                int((time.monotonic() - started) * 1000), True,
                estimated_cost_usd=estimate,
            ))
            return reply
        except BudgetExceeded:
            raise
        except Exception as exc:
            category = exc.category if isinstance(exc, ProviderFailure) else type(exc).__name__
            meter.add_trace(CallTrace(
                route.provider_id, route.model, stage,
                int((time.monotonic() - started) * 1000), False,
                category=category, estimated_cost_usd=estimate,
            ))
            raise

    def _ask(self, routes: Sequence[Route], request: ModelRequest, meter: _Meter,
             stage: str) -> tuple[ModelReply, Route]:
        errors: list[Exception] = []
        for route in routes:
            for attempt in range(self.policy.retry_attempts):
                try:
                    return self._call_one(route, request, meter, stage), route
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    errors.append(exc)
                    retryable = isinstance(exc, ProviderFailure) and exc.retryable
                    if not retryable or attempt + 1 >= self.policy.retry_attempts:
                        break
        if errors:
            raise RuntimeError(f"No configured model route succeeded ({type(errors[-1]).__name__}).") from errors[-1]
        raise RuntimeError("No eligible model route is configured.")

    @staticmethod
    def _request(messages: Sequence[Mapping[str, Any]], system: str,
                 tools: Sequence[FunctionTool], max_output_tokens: int,
                 temperature: float, reasoning_effort: str | None) -> ModelRequest:
        return ModelRequest(
            messages=tuple(dict(item) for item in messages), system=system,
            max_output_tokens=max_output_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tools=tuple(tools),
        )

    @staticmethod
    def _format_tool_result(value: Any) -> str:
        try:
            rendered = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(value)
        return rendered[:20000]

    def answer(
        self,
        prompt: str,
        *,
        system: str = "Be precise and useful. State uncertainty and never claim unverified actions succeeded.",
        history: Sequence[Mapping[str, Any]] = (),
        context: str = "",
        strategy: str | None = None,
        complexity: str = "normal",
        tools: Sequence[FunctionTool] = (),
        authorize_tool: Callable[[FunctionTool, Mapping[str, Any]], bool] | None = None,
        max_output_tokens: int = 4096,
        temperature: float = 0.2,
        reasoning_effort: str | None = None,
    ) -> TaskResult:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("A prompt is required.")
        if complexity not in {"simple", "normal", "complex"}:
            raise ValueError("complexity must be simple, normal, or complex.")
        selected_strategy = strategy or self.policy.strategy
        if selected_strategy not in {"auto", "single", "ensemble"}:
            raise ValueError("strategy must be auto, single, or ensemble.")
        messages = [dict(item) for item in history if item.get("role") in {"user", "assistant", "tool"}]
        if context:
            messages.append({"role": "user", "content": "Context for the next request:\n" + str(context)})
        messages.append({"role": "user", "content": prompt})
        meter = _Meter(self.policy)
        route_list = self._eligible(tools=bool(tools))
        if not route_list:
            raise RuntimeError("No registered route supports the requested text/tool capabilities.")
        use_ensemble = not tools and (
            selected_strategy == "ensemble"
            or (selected_strategy == "auto" and complexity == "complex")
        )
        if use_ensemble:
            text, selected, warnings = self._ensemble(
                messages, system, route_list, meter,
                max_output_tokens=max_output_tokens, temperature=temperature,
                reasoning_effort=reasoning_effort,
            )
            return TaskResult(
                text, "ensemble", tuple(selected), tuple(meter.traces),
                None if meter.cost_unknown else meter.estimated_spend, tuple(warnings),
            )
        text, selected = self._single_with_tools(
            messages, system, route_list, meter, tools,
            authorize_tool=authorize_tool, max_output_tokens=max_output_tokens,
            temperature=temperature, reasoning_effort=reasoning_effort,
        )
        warnings = (
            ("Ensemble was skipped for tool-enabled requests to avoid duplicate side effects.",)
            if selected_strategy == "ensemble" and tools else ()
        )
        return TaskResult(
            text, "single", tuple(selected), tuple(meter.traces),
            None if meter.cost_unknown else meter.estimated_spend, warnings,
        )

    def _single_with_tools(
        self, messages: list[dict[str, Any]], system: str, routes: Sequence[Route],
        meter: _Meter, tools: Sequence[FunctionTool], *,
        authorize_tool: Callable[[FunctionTool, Mapping[str, Any]], bool] | None,
        max_output_tokens: int, temperature: float, reasoning_effort: str | None,
    ) -> tuple[str, list[str]]:
        selected: list[str] = []
        tool_map = {tool.name: tool for tool in tools}
        if len(tool_map) != len(tools):
            raise ValueError("Tool names must be unique within a request.")
        total_tool_calls = 0
        seen_calls: set[str] = set()
        for round_number in range(self.policy.max_tool_rounds + 1):
            request = self._request(messages, system, tools, max_output_tokens, temperature, reasoning_effort)
            reply, route = self._ask(routes, request, meter, "primary" if round_number == 0 else "tool_followup")
            selected.append(self._label(route))
            if not reply.tool_calls:
                if reply.text.strip() or reply.text:
                    return reply.text.strip(), list(dict.fromkeys(selected))
                raise RuntimeError("The selected model returned neither text nor a tool call.")
            if round_number >= self.policy.max_tool_rounds:
                raise RuntimeError("Maximum tool-call rounds reached; refusing unbounded execution.")
            assistant_calls = []
            for call in reply.tool_calls:
                if call.call_id and call.call_id in seen_calls:
                    raise RuntimeError("Duplicate tool call ID detected; refusing repeated execution.")
                if call.call_id:
                    seen_calls.add(call.call_id)
                assistant_calls.append({
                    "id": call.call_id, "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments), ensure_ascii=False)},
                })
            messages.append({"role": "assistant", "content": reply.text, "tool_calls": assistant_calls})
            for call in reply.tool_calls:
                total_tool_calls += 1
                if total_tool_calls > self.policy.max_tool_calls:
                    raise RuntimeError("Maximum tool-call budget reached.")
                tool = tool_map.get(call.name)
                if tool is None:
                    outcome: Any = {"ok": False, "error": "Tool is not registered for this request."}
                else:
                    authorized = (
                        authorize_tool(tool, call.arguments)
                        if authorize_tool is not None
                        else (tool.risk == "read" and not tool.requires_approval)
                    )
                    if not authorized or tool.risk == "dangerous":
                        outcome = {"ok": False, "error": "Tool execution was not authorized."}
                    else:
                        try:
                            outcome = {"ok": True, "result": tool.executor(dict(call.arguments))}
                        except Exception as exc:
                            outcome = {"ok": False, "error": f"Tool failed: {type(exc).__name__}"}
                messages.append({
                    "role": "tool", "tool_call_id": call.call_id, "name": call.name,
                    "content": self._format_tool_result(outcome),
                })
        raise RuntimeError("Tool execution stopped without a final assistant response.")

    def _ensemble(
        self, messages: Sequence[Mapping[str, Any]], system: str, routes: Sequence[Route],
        meter: _Meter, *, max_output_tokens: int, temperature: float,
        reasoning_effort: str | None,
    ) -> tuple[str, list[str], list[str]]:
        # One candidate per provider: multiple aliases for a single vendor are not diversity.
        candidates: list[Route] = []
        seen_providers: set[str] = set()
        for route in routes:
            adapter = self.providers[route.provider_id]
            if "text" not in route.capabilities or "text" not in adapter.capabilities:
                continue
            if route.provider_id in seen_providers:
                continue
            candidates.append(route)
            seen_providers.add(route.provider_id)
            if len(candidates) >= min(self.policy.max_candidate_calls, self.policy.max_total_model_calls - 1):
                break
        if len(candidates) < 2:
            reply, route = self._ask(
                routes, self._request(messages, system, (), max_output_tokens, temperature, reasoning_effort),
                meter, "primary",
            )
            return reply.text, [self._label(route)], ["Fewer than two distinct providers are configured; used one route."]
        critic_budget = min(
            self.policy.max_critic_calls,
            max(0, self.policy.max_total_model_calls - len(candidates) - 1),
        )
        base_request = self._request(
            messages,
            system + "\n\nWork independently. Do not assume other models are correct. State uncertainty.",
            (), max_output_tokens, temperature, reasoning_effort,
        )
        warnings: list[str] = []
        candidate_answers: list[tuple[Route, str]] = []
        with ThreadPoolExecutor(
            max_workers=min(self.policy.max_parallel_calls, len(candidates)),
            thread_name_prefix="PortableReasoner",
        ) as executor:
            future_to_route = {
                executor.submit(self._call_one, route, base_request, meter, "candidate"): route
                for route in candidates
            }
            for future in as_completed(future_to_route):
                route = future_to_route[future]
                try:
                    reply = future.result()
                    if reply.text.strip():
                        candidate_answers.append((route, reply.text.strip()))
                    else:
                        warnings.append(f"{route.provider_id} returned no candidate text.")
                except Exception as exc:
                    warnings.append(f"{route.provider_id} candidate failed ({type(exc).__name__}).")
        candidate_answers.sort(key=lambda item: candidates.index(item[0]))
        if not candidate_answers:
            reply, route = self._ask(routes, base_request, meter, "fallback")
            return reply.text, [self._label(route)], warnings + ["All ensemble candidates failed; used fallback."]

        candidate_provider_ids = {route.provider_id for route, _ in candidate_answers}
        critic_routes = [route for route in routes if route.provider_id not in candidate_provider_ids]
        critiques: list[tuple[Route, str]] = []
        if critic_budget and critic_routes:
            candidate_block = "\n\n".join(
                f"Candidate {index}:\n{answer}"
                for index, (_route, answer) in enumerate(candidate_answers, 1)
            )
            critique_request = self._request(
                [*messages, {"role": "user", "content":
                    "Audit the following independent answers. Find contradictions, unsupported claims, missing constraints, and high-value checks. Do not majority-vote.\n\n" + candidate_block}],
                system + "\n\nYou are a skeptical, evidence-focused reviewer.",
                (), max(1024, max_output_tokens // 2), 0.1, reasoning_effort,
            )
            with ThreadPoolExecutor(
                max_workers=min(self.policy.max_parallel_calls, critic_budget, len(critic_routes)),
                thread_name_prefix="PortableCritic",
            ) as executor:
                futures = {
                    executor.submit(self._call_one, route, critique_request, meter, "critic"): route
                    for route in critic_routes[:critic_budget]
                }
                for future in as_completed(futures):
                    route = futures[future]
                    try:
                        answer = future.result().text.strip()
                        if answer:
                            critiques.append((route, answer))
                    except Exception as exc:
                        warnings.append(f"{route.provider_id} critic failed ({type(exc).__name__}).")
        if meter.calls >= self.policy.max_total_model_calls:
            route, answer = candidate_answers[0]
            return answer, [self._label(r) for r, _ in candidate_answers], warnings + [
                "Call budget reached before final synthesis; returned a candidate answer."
            ]
        reviews = "\n\n".join(
            f"Review {index}:\n{answer}" for index, (_route, answer) in enumerate(critiques, 1)
        ) or "No independent critic response was available."
        synthesis_prompt = (
            "Synthesize one answer to the original request using the independent candidate analyses and reviews. "
            "Resolve contradictions through evidence, not majority vote. Never claim checks were run if they were not. "
            "Clearly label remaining uncertainty.\n\nCandidate analyses:\n"
            + "\n\n".join(
                f"Candidate {index}:\n{answer}" for index, (_route, answer) in enumerate(candidate_answers, 1)
            )
            + "\n\nCritic reviews:\n" + reviews
            + "\n\nOriginal conversation:\n" + json.dumps(list(messages), ensure_ascii=False, default=str)[:30000]
        )
        synthesis_request = self._request(
            [{"role": "user", "content": synthesis_prompt}],
            system + "\n\nYou are the final synthesis model. Prioritize accuracy and transparent uncertainty.",
            (), max_output_tokens, 0.1, reasoning_effort,
        )
        try:
            reply, route = self._ask(routes, synthesis_request, meter, "synthesis")
            selected = [self._label(r) for r, _ in candidate_answers]
            selected.extend(self._label(r) for r, _ in critiques)
            selected.append(self._label(route))
            return reply.text, list(dict.fromkeys(selected)), warnings
        except Exception as exc:
            warnings.append(f"Synthesis failed ({type(exc).__name__}); returned best available candidate.")
            route, answer = candidate_answers[0]
            return answer, [self._label(r) for r, _ in candidate_answers], warnings
