from core.user_paths import get_user_data_dir
import json
import sys
import time
import base64
import logging
import threading
from pathlib import Path
from typing import Optional

import requests
from core.omniroute import gateway as _omniroute_gateway
from core.runtime_paths import API_CONFIG_PATH
from config import get_api_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("openrouter_client")

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR     = _get_base_dir()
API_KEY_PATH = API_CONFIG_PATH

def _load_api_key() -> str:
    try:
        return get_api_key("OpenRouter")
    except Exception as e:
        logger.warning(f"[OpenRouter] Failed to load API key: {e}")
        return ""

TEXT_MODELS: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "minimax/minimax-m2.5:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "qwen/qwen3-coder:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-3-27b-it:free",
    "arcee-ai/trinity-large-preview:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "google/gemma-3-12b-it:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "google/gemma-3-4b-it:free",
    "google/gemma-3n-e4b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "google/gemma-3n-e2b-it:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "liquid/lfm-2.5-1.2b-thinking:free",
    # OpenRouter maintains this router alias and dynamically selects a current
    # free model compatible with the requested capabilities.
    "openrouter/free",
]

VISION_MODELS: list[str] = [
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/llama-nemotron-embed-vl-1b-v2:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-3n-e4b-it:free",
    "google/gemma-3n-e2b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
]

API_URL               = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MAX_TOKENS    = 4096
DEFAULT_TEMPERATURE   = 0.7
REQUEST_TIMEOUT       = 60   # seconds per request
MAX_RETRIES_PER_MODEL = 2    # attempts before moving to next model
RETRY_DELAY           = 2    # seconds between retries
RATE_LIMIT_COOLDOWN   = 60   # seconds before retrying a rate-limited model
FAILED_MODEL_COOLDOWN = 30   # seconds before retrying a transiently unavailable model
MAX_LLM_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_LLM_ERROR_BYTES = 64 * 1024
_RESPONSE_CHUNK_BYTES = 16 * 1024

_rate_limited: dict[str, float] = {}
_failed_until: dict[str, float] = {}
_model_state_lock = threading.Lock()


def _read_bounded_json(response: requests.Response, *, max_bytes: int = MAX_LLM_RESPONSE_BYTES) -> dict:
    """Read and parse a successful HTTP JSON body without unbounded buffering."""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=_RESPONSE_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"LLM response exceeds the {max_bytes // (1024 * 1024)} MiB safety limit.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("LLM response JSON must be an object.")
        return data
    finally:
        try:
            response.close()
        except Exception:
            pass


class OpenRouterClient:

    def __init__(self) -> None:
        self.api_key  = _load_api_key()
        self._headers = {
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://github.com/brahma-ai",
            "X-Title":       "Brahma Evo",
        }
        self._credential_lock = __import__("threading").Lock()
        self._omniroute = _omniroute_gateway()

    def _is_rate_limited(self, model: str) -> bool:
        with _model_state_lock:
            ts = _rate_limited.get(model)
            if ts is None:
                return False
            if time.time() - ts > RATE_LIMIT_COOLDOWN:
                _rate_limited.pop(model, None)
                return False
            return True

    def _mark_rate_limited(self, model: str) -> None:
        with _model_state_lock:
            _rate_limited[model] = time.time()
        logger.warning(
            f"[OpenRouter] Rate limited: {model} — "
            f"cooling down for {RATE_LIMIT_COOLDOWN}s"
        )

    def _is_temporarily_failed(self, model: str) -> bool:
        with _model_state_lock:
            until = _failed_until.get(model)
            if until is None:
                return False
            if time.time() >= until:
                _failed_until.pop(model, None)
                return False
            return True

    def _mark_temporarily_failed(self, model: str) -> None:
        with _model_state_lock:
            _failed_until[model] = time.time() + FAILED_MODEL_COOLDOWN
        logger.warning(
            f"[OpenRouter] Temporarily unavailable: {model} — "
            f"cooling down for {FAILED_MODEL_COOLDOWN}s"
        )

    def _omniroute_enabled(self) -> bool:
        import os
        return os.environ.get("BRAHMA_OMNIROUTE_ENABLED", "1").strip().lower() not in {
            "0", "false", "no", "off"
        }

    def _call_omniroute(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        if not self._omniroute_enabled() or not self._omniroute.ensure_ready():
            return None
        payload: dict = {
            "model": model or "auto",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        import os
        omni_key = os.environ.get("BRAHMA_OMNIROUTE_API_KEY", "").strip()
        if omni_key:
            headers["Authorization"] = f"Bearer {omni_key}"
        try:
            response = requests.post(
                self._omniroute.base_url + "/chat/completions",
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            if 300 <= response.status_code < 400:
                logger.warning("[OmniRoute] Authenticated request was redirected; refusing credential forwarding.")
                return None
            if response.status_code != 200:
                logger.warning(f"[OmniRoute] HTTP {response.status_code}; using direct provider fallback")
                return None
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict)
                )
            return str(content).strip() if content else None
        except Exception as exc:
            logger.warning(f"[OmniRoute] request failed; using direct provider fallback: {exc}")
            return None

    def _refresh_credentials(self) -> str:
        """Refresh the current credential without mutating shared request headers."""
        with self._credential_lock:
            self.api_key = _load_api_key()
            return self.api_key

    def _request_headers(self) -> dict[str, str]:
        """Return an immutable per-request header snapshot so parallel calls cannot race."""
        key = self._refresh_credentials()
        headers = dict(self._headers)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers.pop("Authorization", None)
        return headers

    def _call(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        payload: dict = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        if self._is_rate_limited(model) or self._is_temporarily_failed(model):
            return None

        headers = self._request_headers()
        if not headers.get("Authorization"):
            raise PermissionError(
                f"[OpenRouter] API key is missing. Add a valid sk-or- key in {API_CONFIG_PATH}."
            )

        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                resp = requests.post(
                    API_URL,
                    headers=headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    stream=True,
                )

                if resp.status_code == 401:
                    raise PermissionError(
                        f"[OpenRouter] Authentication failed for model {model}. "
                        "Check your API key in the Brahma Evo provider settings."
                    )

                if resp.status_code == 403:
                    self._mark_temporarily_failed(model)
                    logger.warning(
                        f"[OpenRouter] Access denied for model {model} (HTTP 403); skipping model"
                    )
                    return None

                if resp.status_code == 429:
                    self._mark_rate_limited(model)
                    return None

                if resp.status_code in {400, 404, 422}:
                    self._mark_temporarily_failed(model)
                    logger.warning(
                        f"[OpenRouter] {model} -> non-retryable HTTP {resp.status_code}; skipping model"
                    )
                    return None

                if resp.status_code == 200:
                    data    = _read_bounded_json(resp)
                    content = (
                        data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                    )
                    return content.strip() if content else None

                logger.warning(
                    f"[OpenRouter] {model} → HTTP {resp.status_code} "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )

            except requests.exceptions.Timeout:
                logger.warning(
                    f"[OpenRouter] {model} → Timeout "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )
                if attempt == MAX_RETRIES_PER_MODEL:
                    self._mark_temporarily_failed(model)
            except PermissionError:
                raise
            except Exception as e:
                logger.error(f"[OpenRouter] {model} → Unexpected error: {e}")
                if attempt == MAX_RETRIES_PER_MODEL:
                    self._mark_temporarily_failed(model)

            if attempt < MAX_RETRIES_PER_MODEL:
                time.sleep(RETRY_DELAY)
            else:
                self._mark_temporarily_failed(model)

        return None

    def _call_with_fallback(
        self,
        pool: list[str],
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> str:
        # OmniRoute routing aliases are local-gateway names, not valid direct
        # OpenRouter model IDs. Never send them to the direct fallback pool.
        direct_model = model if model and not model.startswith("auto") else None
        if direct_model and not self._is_rate_limited(direct_model):
            try:
                result = self._call(direct_model, messages, max_tokens, temperature, response_format)
                if result:
                    return result
                logger.info(
                    f"[OpenRouter] Requested model failed, "
                    f"falling back to pool: {direct_model}"
                )
            except PermissionError:
                raise

        for m in pool:
            if self._is_rate_limited(m) or self._is_temporarily_failed(m):
                continue
            logger.info(f"[OpenRouter] Trying: {m}")
            result = self._call(m, messages, max_tokens, temperature, response_format)
            if result:
                logger.info(f"[OpenRouter] ✓ Success: {m}")
                return result

        raise RuntimeError(
            "[OpenRouter] All models failed or are rate-limited. "
            "Check your API key and network connection."
        )


    @staticmethod
    def _normalize_tools(tools: list[dict] | None) -> list[dict]:
        """Convert Brahma/Gemini-style declarations into OpenAI-compatible tool schemas."""
        def lower_types(value):
            if isinstance(value, dict):
                return {
                    key: (str(item).lower() if key == "type" and isinstance(item, str)
                          else lower_types(item))
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [lower_types(item) for item in value]
            return value

        normalized: list[dict] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                fn = tool.get("function")
                if isinstance(fn, dict):
                    name = str(fn.get("name") or "").strip()
                    if name:
                        normalized.append({
                            "type": "function",
                            "function": lower_types(dict(fn)),
                        })
                continue
            normalized.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": lower_types(
                        tool.get("parameters") or {
                            "type": "object",
                            "properties": {},
                        }
                    ),
                },
            })
        return normalized

    def _call_tool_capable(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> dict:
        """Make one OpenRouter request that preserves structured tool calls."""
        if self._is_rate_limited(model) or self._is_temporarily_failed(model):
            return {}
        headers = self._request_headers()
        if not headers.get("Authorization"):
            raise PermissionError(
                f"[OpenRouter] API key is missing. Add it in {API_CONFIG_PATH}."
            )
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": self._normalize_tools(tools),
            "tool_choice": "auto",
        }
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                resp = requests.post(
                    API_URL,
                    headers=headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
                if resp.status_code == 401:
                    raise PermissionError("[OpenRouter] Authentication failed.")
                if resp.status_code == 403:
                    self._mark_temporarily_failed(model)
                    logger.warning(
                        f"[OpenRouter] tool-capable {model} -> HTTP 403; skipping model"
                    )
                    return {}
                if resp.status_code == 429:
                    self._mark_rate_limited(model)
                    return {}
                if resp.status_code in {400, 404, 422}:
                    self._mark_temporarily_failed(model)
                    logger.warning(
                        f"[OpenRouter] tool-capable {model} -> non-retryable HTTP {resp.status_code}; skipping model"
                    )
                    return {}
                if resp.status_code == 200:
                    data = _read_bounded_json(resp)
                    if not isinstance(data, dict):
                        return {}
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        logger.warning("[OpenRouter] tool-capable response had no choices; trying next model")
                        return {}
                    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                    content = message.get("content", "") if isinstance(message, dict) else ""
                    tool_calls = message.get("tool_calls") or [] if isinstance(message, dict) else []
                    if not str(content or "").strip() and not tool_calls:
                        logger.warning("[OpenRouter] tool-capable response had no usable content or tool calls; trying next model")
                        return {}
                    return data
                logger.warning(
                    f"[OpenRouter] tool-capable {model} -> HTTP {resp.status_code} "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )
            except requests.exceptions.Timeout:
                logger.warning(
                    f"[OpenRouter] tool-capable {model} timed out "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )
                if attempt == MAX_RETRIES_PER_MODEL:
                    self._mark_temporarily_failed(model)
            except PermissionError:
                raise
            except Exception as exc:
                logger.error(f"[OpenRouter] tool-capable {model} failed: {exc}")
                if attempt == MAX_RETRIES_PER_MODEL:
                    self._mark_temporarily_failed(model)
            if attempt < MAX_RETRIES_PER_MODEL:
                time.sleep(RETRY_DELAY)
            else:
                self._mark_temporarily_failed(model)
        return {}

    def _call_omniroute_tool_capable(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        max_rounds: int = 6,
    ) -> Optional[str]:
        """Run tool calling through the local OmniRoute gateway before direct fallback."""
        if not self._omniroute_enabled() or not self._omniroute.ensure_ready():
            return None

        normalized_messages = [dict(message) for message in messages]
        normalized_tools = self._normalize_tools(tools)
        if not normalized_tools:
            return None
        declared_names = {
            str(item.get("function", {}).get("name") or "").strip()
            for item in normalized_tools
        }
        declared_names.discard("")

        headers = {"Content-Type": "application/json"}
        import os
        omni_key = os.environ.get("BRAHMA_OMNIROUTE_API_KEY", "").strip()
        if omni_key:
            headers["Authorization"] = f"Bearer {omni_key}"

        for round_index in range(max(1, int(max_rounds))):
            payload = {
                "model": model or "auto",
                "messages": normalized_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "tools": normalized_tools,
                "tool_choice": "auto",
            }
            try:
                response = requests.post(
                    self._omniroute.base_url + "/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except Exception as exc:
                logger.warning(f"[OmniRoute] tool request failed; using direct provider fallback: {exc}")
                return None

            if response.status_code in {401, 403, 404, 429, 500, 502, 503, 504}:
                logger.warning(
                    f"[OmniRoute] tool request HTTP {response.status_code}; "
                    "using direct provider fallback"
                )
                return None
            if response.status_code != 200:
                logger.warning(
                    f"[OmniRoute] tool request unexpected HTTP {response.status_code}; "
                    "using direct provider fallback"
                )
                return None

            try:
                data = _read_bounded_json(response)
            except Exception as exc:
                logger.warning(f"[OmniRoute] invalid/bounded tool response JSON: {exc}")
                return None

            message = data.get("choices", [{}])[0].get("message", {}) or {}
            content = message.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("text")
                )

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                text = str(content or "").strip()
                if text:
                    return text
                finish_reason = str(
                    data.get("choices", [{}])[0].get("finish_reason") or ""
                )
                if finish_reason == "length":
                    continue
                return None

            normalized_messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            for index, call in enumerate(tool_calls):
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                name = str(fn.get("name") or "").strip()
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                        result = None
                    except json.JSONDecodeError as exc:
                        args = {}
                        result = f"Tool arguments were invalid JSON: {exc}"
                elif isinstance(raw_args, dict):
                    args = raw_args
                    result = None
                else:
                    args = {}
                    result = "The model returned invalid tool arguments."

                if not name:
                    result = "The model returned a tool call without a name."
                elif name not in declared_names:
                    result = f"The model requested an undeclared tool: {name}."
                elif result is None:
                    try:
                        result = tool_executor(name, args)
                    except Exception as exc:
                        result = f"Tool '{name}' failed: {exc}"

                call_id = str(call.get("id") or f"omni_tool_{round_index}_{index}")
                normalized_messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(result),
                })

        return None

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        max_rounds: int = 6,
    ) -> str:
        """Run a bounded OmniRoute-first tool-calling conversation with direct fallback."""
        omni_result = self._call_omniroute_tool_capable(
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            model=model or "auto",
            max_tokens=max_tokens,
            temperature=temperature,
            max_rounds=max_rounds,
        )
        if omni_result:
            return omni_result

        normalized_messages = [dict(message) for message in messages]
        normalized_tools = self._normalize_tools(tools)
        declared_names = {
            str(item.get("function", {}).get("name") or "").strip()
            for item in normalized_tools
        }
        declared_names.discard("")
        candidates = []
        if model and not model.startswith("auto"):
            candidates.append(model)
        candidates.extend(TEXT_MODELS)
        seen = set()
        candidates = [m for m in candidates if m and not (m in seen or seen.add(m))]

        last_content = ""
        for round_index in range(max(1, int(max_rounds))):
            response = {}
            for candidate in candidates:
                if self._is_rate_limited(candidate) or self._is_temporarily_failed(candidate):
                    continue
                response = self._call_tool_capable(
                    candidate,
                    normalized_messages,
                    tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if response:
                    break
            if not response:
                raise RuntimeError("[OpenRouter] No tool-capable model returned a response.")

            message = response.get("choices", [{}])[0].get("message", {}) or {}
            content = message.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("text")
                )
            last_content = str(content or "").strip()

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                if last_content:
                    return last_content
                finish_reason = str(response.get("choices", [{}])[0].get("finish_reason") or "")
                if finish_reason == "length":
                    continue
                return "Task completed."

            assistant_message = {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
            normalized_messages.append(assistant_message)

            for index, call in enumerate(tool_calls):
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                name = str(fn.get("name") or "").strip()
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError as exc:
                        result = f"Tool arguments were invalid JSON: {exc}"
                        args = {}
                    else:
                        result = None
                else:
                    args = raw_args if isinstance(raw_args, dict) else {}
                    result = None

                if not name:
                    result = "The model returned an invalid tool call with no tool name."
                elif name not in declared_names:
                    result = f"The model requested an undeclared tool: {name}."

                if result is None:
                    try:
                        result = tool_executor(name, args)
                    except Exception as exc:
                        result = f"Tool '{name}' failed: {exc}"

                call_id = str(call.get("id") or f"call_{round_index}_{index}")
                normalized_messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(result),
                })

        if last_content:
            return last_content
        raise RuntimeError("OpenRouter tool-calling reached its safety round limit without a final response.")

    def chat(
        self,
        prompt: str,
        system: str = (
            "You are a component of Brahma Evo, an autonomous self-evolving personal assistant. "
            "Be concise, helpful, and precise."
        ),
        history: Optional[list[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        omni_result = self._call_omniroute(
            messages,
            model=model or "auto",
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if omni_result:
            return omni_result
        return self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature
        )

    def chat_json(
        self,
        prompt: str,
        system: str = (
            "Return ONLY valid JSON. "
            "No markdown fences, no extra text, no explanation."
        ),
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]
        raw = self._call_omniroute(
            messages,
            model=model or "auto",
            max_tokens=max_tokens,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        if not raw:
            raw = self._call_with_fallback(
                TEXT_MODELS, messages, model, max_tokens, temperature=0.2
            )

        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("`").strip()

        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            logger.error(f"[OpenRouter] JSON parse failed: {e}")
            raise ValueError("Local model returned unparseable JSON.") from e

    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        ]
        omni_result = self._call_omniroute(
            messages,
            model=model or "auto",
            max_tokens=max_tokens,
            temperature=0.2,
        )
        if omni_result:
            return omni_result
        return self._call_with_fallback(
            VISION_MODELS, messages, model, max_tokens, temperature=0.2
        )

    def vision_from_file(
        self,
        prompt: str,
        image_path: str,
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        path = Path(image_path)
        mime_map = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif":  "image/gif",
        }
        mime = mime_map.get(path.suffix.lower(), "image/png")

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
    
        omni_result = self._call_omniroute(
            messages,
            model=model or "auto",
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if omni_result:
            return omni_result
        return self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature
        )

    def available_models(self) -> dict:
        info = {
            "text_models":   TEXT_MODELS,
            "vision_models": VISION_MODELS,
            "rate_limited":  list(_rate_limited.keys()),
            "total_text":    len(TEXT_MODELS),
            "total_vision":  len(VISION_MODELS),
        }
        try:
            info["omniroute"] = self._omniroute.status()
        except Exception:
            info["omniroute"] = {"available": False, "reason": "status unavailable"}
        return info

client = OpenRouterClient()

if __name__ == "__main__":
    print("=" * 55)
    print("  Brahma Evo — OpenRouter Client Self-Test")
    print("=" * 55)

    print("\n[TEST 1] Basic chat...")
    try:
        reply = client.chat("Introduce yourself in one sentence.")
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 2] JSON mode...")
    try:
        data = client.chat_json(
            'List 3 programming languages. Format: {"languages": ["a", "b", "c"]}',
            system="Return only valid JSON. No extra text."
        )
        print(f"  Response : {data}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 3] Multi-turn conversation...")
    try:
        history = [
            {"role": "system",    "content": "You are a helpful assistant. Be brief."},
            {"role": "user",      "content": "My name is User."},
            {"role": "assistant", "content": "Hello User, how can I help you?"},
            {"role": "user",      "content": "What is my name?"},
        ]
        reply = client.multi_turn(history)
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 4] Model pool info...")
    info = client.available_models()
    print(f"  Text models   : {info['total_text']}")
    print(f"  Vision models : {info['total_vision']}")
    print(f"  Rate limited  : {info['rate_limited'] or 'none'}")
    print(f"  Status        : PASS ✓")

    print("\n" + "=" * 55)
    print("  All tests complete.")
    print("=" * 55)
