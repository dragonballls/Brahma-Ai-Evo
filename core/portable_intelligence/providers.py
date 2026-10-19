"""Native and OpenAI-compatible provider adapters with safe HTTP handling."""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from .contracts import FunctionTool, ModelReply, ModelRequest, ProviderFailure, ToolCall


def validate_base_url(base_url: str) -> str:
    """Require TLS except loopback; reject credentials and query-string secrets."""
    value = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider base_url must be an absolute http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provider base_url cannot contain credentials, query, or fragment.")
    loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("Non-local provider endpoints must use HTTPS.")
    return value


def _status_category(status: int) -> tuple[str, bool]:
    if status in {401, 403}:
        return "authentication_or_permission", False
    if status == 429:
        return "rate_limited", True
    if status in {408, 425} or status >= 500:
        return "transient_http_error", True
    if 300 <= status < 400:
        return "redirect_refused", False
    return "invalid_request_or_provider_error", False


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            part["text"] for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _parse_arguments(value: Any, provider_id: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise ProviderFailure(provider_id, "malformed_tool_arguments")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderFailure(provider_id, "malformed_tool_arguments") from exc
    if not isinstance(parsed, dict):
        raise ProviderFailure(provider_id, "tool_arguments_not_object")
    return parsed


def _tool_by_name(request: ModelRequest, name: str) -> FunctionTool | None:
    return next((tool for tool in request.tools if tool.name == name), None)


_GPT6_SAMPLING_RESTRICTED = frozenset({"gpt-6-astra", "gpt-6.1-sol"})
_GPT6_RESPONSES_REQUIRED_FOR_TOOLS = frozenset({"gpt-6-astra", "gpt-6.1-sol"})
_GPT6_CHAT_TOOLS_REQUIRE_NONE = frozenset({"gpt-6-sol", "gpt-6-luna"})


def _model_leaf(model: str) -> str:
    return str(model or "").rsplit("/", 1)[-1].casefold()


def _validate_gpt6_request(
    provider_id: str,
    request: ModelRequest,
    model: str,
    *,
    uses_responses_api: bool = False,
) -> bool:
    """Return whether sampling parameters are restricted for this known model."""
    leaf = _model_leaf(model)
    restricted_sampling = leaf in _GPT6_SAMPLING_RESTRICTED
    if restricted_sampling and request.reasoning_effort in {"none", "minimal"}:
        raise ProviderFailure(
            provider_id, "unsupported_reasoning_effort",
            f"{leaf} supports low/medium/high/xhigh/max reasoning effort, not {request.reasoning_effort!r}.",
        )
    if request.tools and not uses_responses_api and leaf in _GPT6_RESPONSES_REQUIRED_FOR_TOOLS:
        raise ProviderFailure(
            provider_id, "native_responses_required",
            f"{leaf} tool calling requires the native OpenAI Responses API.",
        )
    if (
        request.tools
        and not uses_responses_api
        and leaf in _GPT6_CHAT_TOOLS_REQUIRE_NONE
        and request.reasoning_effort != "none"
    ):
        raise ProviderFailure(
            provider_id, "chat_tool_calling_requires_none",
            f"{leaf} Chat Completions function calling requires reasoning_effort='none'; use Responses otherwise.",
        )
    return restricted_sampling


class HTTPProvider:
    provider_id = "http_provider"
    capabilities = frozenset({"text", "tools"})

    def __init__(self, api_key: str, base_url: str, *,
                 timeout: tuple[float, float] = (5.0, 60.0), session: Any = None) -> None:
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("A provider API key is required.")
        self.base_url = validate_base_url(base_url)
        if len(timeout) != 2 or timeout[0] <= 0 or timeout[1] <= 0:
            raise ValueError("HTTP timeouts must be positive connect/read values.")
        self.timeout = timeout
        self._session = session or requests

    def _post(self, url: str, payload: Mapping[str, Any],
              headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        request_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        extra_headers = dict(headers or {})
        if any(str(key).casefold() == "x-api-key" for key in extra_headers):
            request_headers.pop("Authorization", None)
        request_headers.update(extra_headers)
        try:
            response = self._session.post(
                url, headers=request_headers, json=dict(payload),
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ProviderFailure(
                self.provider_id, "network_error", type(exc).__name__, retryable=True
            ) from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            category, retryable = _status_category(status)
            raise ProviderFailure(self.provider_id, category, status_code=status, retryable=retryable)
        try:
            data = response.json()
        except (ValueError, AttributeError) as exc:
            raise ProviderFailure(self.provider_id, "invalid_json_response") from exc
        if not isinstance(data, dict):
            raise ProviderFailure(self.provider_id, "unexpected_response_schema")
        return data


class CallableProvider:
    """Bridge an existing host/Abacus LLM callback into the portable contract.

    callback receives (ModelRequest, model_name) and may return ModelReply or text.
    Provider-specific clients and credentials remain owned by the host application.
    """
    def __init__(self, provider_id: str, callback, *,
                 capabilities: frozenset[str] = frozenset({"text"})) -> None:
        if not str(provider_id or "").strip():
            raise ValueError("provider_id is required.")
        if not callable(callback):
            raise TypeError("callback must be callable.")
        self.provider_id = str(provider_id).strip()
        self._callback = callback
        self.capabilities = frozenset(capabilities)

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        try:
            result = self._callback(request, model)
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(self.provider_id, type(exc).__name__) from exc
        if isinstance(result, ModelReply):
            return ModelReply(
                text=result.text, provider_id=self.provider_id, model=result.model or model,
                tool_calls=result.tool_calls, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, response_id=result.response_id,
                metadata=result.metadata,
            )
        if isinstance(result, str):
            return ModelReply(text=result, provider_id=self.provider_id, model=model)
        raise ProviderFailure(self.provider_id, "callback_contract_violation")


class OpenAICompatibleProvider(HTTPProvider):
    """OpenAI-compatible /chat/completions adapter for OmniRoute and similar gateways."""
    capabilities = frozenset({"text", "tools", "vision"})

    def __init__(self, api_key: str, base_url: str, **kwargs: Any) -> None:
        super().__init__(api_key, base_url, **kwargs)
        self.provider_id = "openai_compatible"

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        restricted_sampling = _validate_gpt6_request(self.provider_id, request, model)
        messages = [{"role": "system", "content": request.system}] if request.system else []
        for item in request.messages:
            message: dict[str, Any] = {"role": item["role"], "content": item.get("content", "")}
            for field in ("name", "tool_call_id", "tool_calls"):
                if item.get(field):
                    message[field] = item[field]
            messages.append(message)
        payload: dict[str, Any] = {
            "model": model, "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if not restricted_sampling and (not request.reasoning_effort or request.reasoning_effort == "none"):
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [tool.openai_chat_schema() for tool in request.tools]
            payload["tool_choice"] = "auto"
        if request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        data = self._post(self.base_url + "/chat/completions", payload)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderFailure(self.provider_id, "empty_choices")
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise ProviderFailure(self.provider_id, "unexpected_message_schema")
        calls = []
        for raw in message.get("tool_calls") or []:
            if not isinstance(raw, dict):
                continue
            fn = raw.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "")
            if not _tool_by_name(request, name):
                raise ProviderFailure(self.provider_id, "undeclared_tool_requested")
            calls.append(ToolCall(str(raw.get("id") or ""), name,
                                  _parse_arguments(fn.get("arguments", "{}"), self.provider_id)))
        usage = data.get("usage") or {}
        return ModelReply(
            text=_content_text(message.get("content")), provider_id=self.provider_id,
            model=str(data.get("model") or model), tool_calls=tuple(calls),
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
            response_id=data.get("id"),
        )


class OpenAIResponsesProvider(HTTPProvider):
    """Native OpenAI Responses API adapter for current reasoning and tool-capable models."""
    capabilities = frozenset({"text", "tools", "vision"})

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", **kwargs: Any) -> None:
        super().__init__(api_key, base_url, **kwargs)
        self.provider_id = "openai_responses"

    @staticmethod
    def _input_items(request: ModelRequest) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in request.messages:
            if message.get("role") == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                })
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                items.append({
                    "type": "function_call", "call_id": str(call.get("id") or ""),
                    "name": str(fn.get("name") or ""), "arguments": str(fn.get("arguments") or "{}"),
                })
            if message.get("content"):
                items.append({"role": message.get("role"), "content": str(message["content"])})
        return items

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        restricted_sampling = _validate_gpt6_request(
            self.provider_id, request, model, uses_responses_api=True
        )
        payload: dict[str, Any] = {
            "model": model, "input": self._input_items(request),
            "max_output_tokens": request.max_output_tokens,
        }
        if request.system:
            payload["instructions"] = request.system
        if request.reasoning_effort:
            payload["reasoning"] = {"effort": request.reasoning_effort}
        if not restricted_sampling and (not request.reasoning_effort or request.reasoning_effort == "none"):
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [tool.openai_responses_schema() for tool in request.tools]
        data = self._post(self.base_url + "/responses", payload)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        if isinstance(part.get("text"), str):
                            text_parts.append(part["text"])
            elif item.get("type") == "function_call":
                name = str(item.get("name") or "")
                if not _tool_by_name(request, name):
                    raise ProviderFailure(self.provider_id, "undeclared_tool_requested")
                calls.append(ToolCall(
                    str(item.get("call_id") or item.get("id") or ""), name,
                    _parse_arguments(item.get("arguments", "{}"), self.provider_id),
                ))
        usage = data.get("usage") or {}
        return ModelReply(
            text="".join(text_parts).strip(), provider_id=self.provider_id,
            model=str(data.get("model") or model), tool_calls=tuple(calls),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            response_id=data.get("id"),
        )


class AnthropicMessagesProvider(HTTPProvider):
    """Anthropic Messages API adapter, including tool-result conversion."""
    capabilities = frozenset({"text", "tools", "vision"})

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com/v1", **kwargs: Any) -> None:
        super().__init__(api_key, base_url, **kwargs)
        self.provider_id = "anthropic_messages"

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        messages: list[dict[str, Any]] = []
        for item in request.messages:
            role = item.get("role")
            if role == "tool":
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result", "tool_use_id": str(item.get("tool_call_id") or ""),
                        "content": str(item.get("content") or ""),
                    }],
                })
                continue
            content = []
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    content.append({
                        "type": "tool_use", "id": str(call.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": _parse_arguments(fn.get("arguments", "{}"), self.provider_id),
                    })
            if item.get("content"):
                content.append({"type": "text", "text": str(item.get("content"))})
            if content:
                messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
        payload: dict[str, Any] = {
            "model": model, "messages": messages,
            "max_tokens": request.max_output_tokens, "temperature": request.temperature,
        }
        if request.system:
            payload["system"] = request.system
        if request.tools:
            payload["tools"] = [tool.anthropic_schema() for tool in request.tools]
        data = self._post(
            self.base_url + "/messages", payload,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
        )
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                if not _tool_by_name(request, name):
                    raise ProviderFailure(self.provider_id, "undeclared_tool_requested")
                calls.append(ToolCall(
                    str(block.get("id") or ""), name, _parse_arguments(block.get("input", {}), self.provider_id)
                ))
        usage = data.get("usage") or {}
        return ModelReply(
            text="".join(text_parts).strip(), provider_id=self.provider_id,
            model=str(data.get("model") or model), tool_calls=tuple(calls),
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            response_id=data.get("id"),
        )


class GeminiContentProvider(HTTPProvider):
    """Google Gemini generateContent adapter for text and declared function calls."""
    capabilities = frozenset({"text", "tools", "vision"})

    def __init__(self, api_key: str, base_url: str = "https://generativelanguage.googleapis.com/v1beta", **kwargs: Any) -> None:
        super().__init__(api_key, base_url, **kwargs)
        self.provider_id = "gemini_generate_content"

    def _post(self, url: str, payload: Mapping[str, Any],
              headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        try:
            response = self._session.post(
                url, headers={
                    "x-goog-api-key": self.api_key, "Content-Type": "application/json",
                    "Accept": "application/json", **dict(headers or {}),
                }, json=dict(payload), timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ProviderFailure(self.provider_id, "network_error", type(exc).__name__, retryable=True) from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            category, retryable = _status_category(status)
            raise ProviderFailure(self.provider_id, category, status_code=status, retryable=retryable)
        try:
            data = response.json()
        except (ValueError, AttributeError) as exc:
            raise ProviderFailure(self.provider_id, "invalid_json_response") from exc
        if not isinstance(data, dict):
            raise ProviderFailure(self.provider_id, "unexpected_response_schema")
        return data

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        contents: list[dict[str, Any]] = []
        for item in request.messages:
            role = item.get("role")
            if role == "tool":
                contents.append({
                    "role": "user", "parts": [{
                        "functionResponse": {
                            "name": str(item.get("name") or "tool"),
                            "response": {"result": str(item.get("content") or "")},
                        }
                    }],
                })
                continue
            parts = []
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    parts.append({"functionCall": {
                        "name": str(fn.get("name") or ""),
                        "args": _parse_arguments(fn.get("arguments", "{}"), self.provider_id),
                    }})
            if item.get("content"):
                parts.append({"text": str(item.get("content"))})
            if parts:
                contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": request.max_output_tokens, "temperature": request.temperature},
        }
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            payload["tools"] = [{"functionDeclarations": [tool.gemini_schema() for tool in request.tools]}]
        data = self._post(self.base_url + f"/models/{model}:generateContent", payload)
        candidates = data.get("candidates") or []
        if not candidates or not isinstance(candidates[0], dict):
            raise ProviderFailure(self.provider_id, "empty_candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            fn = part.get("functionCall")
            if isinstance(fn, dict):
                name = str(fn.get("name") or "")
                if not _tool_by_name(request, name):
                    raise ProviderFailure(self.provider_id, "undeclared_tool_requested")
                calls.append(ToolCall(
                    str(fn.get("id") or f"gemini-{len(calls)+1}"), name,
                    _parse_arguments(fn.get("args", {}), self.provider_id),
                ))
        usage = data.get("usageMetadata") or {}
        return ModelReply(
            text="".join(text_parts).strip(), provider_id=self.provider_id, model=model,
            tool_calls=tuple(calls), input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
