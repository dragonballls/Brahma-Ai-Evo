"""Stable contracts for a portable multi-provider reasoning engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

JSONMap = Mapping[str, Any]


class ProviderFailure(RuntimeError):
    """Provider failure that is safe to log without exposing request secrets."""
    def __init__(self, provider_id: str, category: str, message: str = "", *,
                 status_code: int | None = None, retryable: bool = False,
                 retry_after_seconds: float | None = None) -> None:
        self.provider_id = str(provider_id or "unknown")
        self.category = str(category or "provider_error")
        self.status_code = status_code
        self.retryable = bool(retryable)
        try:
            value = float(retry_after_seconds) if retry_after_seconds is not None else None
        except (TypeError, ValueError):
            value = None
        self.retry_after_seconds = max(0.0, min(value, 3600.0)) if value is not None else None
        safe_message = str(message or self.category).replace("\n", " ")[:240]
        super().__init__(f"{self.provider_id}: {self.category}: {safe_message}")


class BudgetExceeded(RuntimeError):
    """A configured model-call or estimated-spend budget would be exceeded."""


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FunctionTool:
    """Model-visible tool schema plus host-owned execution and risk policy."""
    name: str
    description: str
    parameters: Mapping[str, Any]
    executor: Callable[[Mapping[str, Any]], Any] = field(compare=False, repr=False)
    risk: str = "read"
    requires_approval: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("Tool names must be non-empty identifiers.")
        if self.risk not in {"read", "write", "external", "dangerous"}:
            raise ValueError("Tool risk must be read, write, external, or dangerous.")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("Tool parameters must be a JSON schema mapping.")

    def openai_chat_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": dict(self.parameters)
        }}

    def openai_responses_schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name, "description": self.description,
                "parameters": dict(self.parameters), "strict": False}

    def anthropic_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": dict(self.parameters)}

    def gemini_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": dict(self.parameters)}


@dataclass(frozen=True)
class ModelRequest:
    """Canonical conversation request. Credentials are passed separately to adapters."""
    messages: Sequence[JSONMap]
    system: str = ""
    max_output_tokens: int = 4096
    temperature: float = 0.2
    reasoning_effort: str | None = None
    tools: Sequence[FunctionTool] = ()

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("At least one conversation message is required.")
        if not 1 <= int(self.max_output_tokens) <= 131072:
            raise ValueError("max_output_tokens is outside the supported safety range.")
        if not 0 <= float(self.temperature) <= 2:
            raise ValueError("temperature must be between 0 and 2.")
        for message in self.messages:
            if message.get("role") not in {"user", "assistant", "tool"}:
                raise ValueError("Messages must use user, assistant, or tool roles.")


@dataclass(frozen=True)
class ModelReply:
    text: str
    provider_id: str
    model: str
    tool_calls: Sequence[ToolCall] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    """One model route; optional rates are USD per million tokens."""
    provider_id: str
    model: str
    priority: int = 100
    capabilities: frozenset[str] = frozenset({"text"})
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    diversity_group: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.model.strip():
            raise ValueError("Routes require provider_id and model.")
        if self.priority < 0:
            raise ValueError("Route priority cannot be negative.")
        if self.diversity_group is not None and not str(self.diversity_group).strip():
            raise ValueError("diversity_group must be non-empty when provided.")
        for rate in (self.input_usd_per_million, self.output_usd_per_million):
            if rate is not None and rate < 0:
                raise ValueError("Token prices cannot be negative.")

    def estimate_cost(self, request: ModelRequest) -> float | None:
        """Conservative preflight estimate, never represented as actual billing."""
        if self.input_usd_per_million is None or self.output_usd_per_million is None:
            return None
        chars = len(request.system)
        for item in request.messages:
            content = item.get("content", "")
            chars += len(content) if isinstance(content, str) else len(str(content))
            if item.get("tool_calls"):
                chars += len(str(item.get("tool_calls")))
        estimated_input_tokens = max(1, (chars + 3) // 4)
        return (
            estimated_input_tokens * self.input_usd_per_million / 1_000_000
            + int(request.max_output_tokens) * self.output_usd_per_million / 1_000_000
        )


@dataclass(frozen=True)
class RunPolicy:
    strategy: str = "auto"
    max_candidate_calls: int = 3
    max_critic_calls: int = 2
    max_total_model_calls: int = 6
    max_parallel_calls: int = 3
    max_tool_rounds: int = 4
    max_tool_calls: int = 8
    max_cost_usd: float | None = None
    retry_attempts: int = 2

    def __post_init__(self) -> None:
        if self.strategy not in {"auto", "single", "ensemble"}:
            raise ValueError("strategy must be auto, single, or ensemble.")
        if self.max_candidate_calls < 1 or self.max_total_model_calls < 1:
            raise ValueError("Model-call limits must be positive.")
        if self.max_parallel_calls < 1 or self.max_tool_rounds < 0 or self.max_tool_calls < 0:
            raise ValueError("Parallelism and tool limits are invalid.")
        if self.max_critic_calls < 0 or self.retry_attempts < 1:
            raise ValueError("Critic and retry limits are invalid.")
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd cannot be negative.")


@dataclass(frozen=True)
class CallTrace:
    provider_id: str
    model: str
    stage: str
    elapsed_ms: int
    ok: bool
    category: str = ""
    estimated_cost_usd: float | None = None


@dataclass(frozen=True)
class TaskResult:
    text: str
    strategy: str
    selected_routes: Sequence[str]
    traces: Sequence[CallTrace]
    estimated_cost_usd: float | None
    warnings: Sequence[str] = ()


class ProviderAdapter(Protocol):
    provider_id: str
    capabilities: frozenset[str]

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        """Complete one model turn or return declared function calls."""
