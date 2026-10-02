"""
Brahma Local Brain Engine (v3)
Provides local LLM execution through an OpenAI-compatible API such as
Ollama, LM Studio, vLLM, or LocalAI.

Design goals:
- Honor the configured endpoint instead of assuming Ollama.
- Keep health/model discovery fast with a short-lived cache.
- Produce useful HTTP errors instead of silently swallowing malformed responses.
- Preserve the existing tool-calling interface used by main.py.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Generator, List, Optional

from memory import config_manager

DEFAULT_ENDPOINT = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b"
OLLAMA_API_PATH = "/api"
MODEL_CACHE_TTL = 5.0


CORE_LOCAL_TOOL_NAMES = {
    "open_app", "computer_settings", "system_diagnostics", "spotify_controller",
    "youtube_video", "web_search", "weather_report", "file_controller",
    "smart_organizer", "desktop_control", "execute_protocol", "reminder",
    "word_document", "pdf_document", "dev_agent", "recall_memory",
    "save_memory", "shutdown_brahma", "undo",
}


def convert_schema_to_lowercase(schema: Any) -> Any:
    """Recursively convert Gemini-style schema types to OpenAI JSON-schema casing."""
    if isinstance(schema, dict):
        return {
            key: (value.lower() if key == "type" and isinstance(value, str)
                  else convert_schema_to_lowercase(value))
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [convert_schema_to_lowercase(item) for item in schema]
    return schema


class LocalBrain:
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        self.endpoint = self._normalize_endpoint(endpoint)
        self.default_model = default_model or DEFAULT_MODEL
        self.enabled = False
        self._cached_models: List[str] = []
        self._models_cached_at = 0.0
        self._cache_lock = threading.RLock()
        self._pull_lock = threading.Lock()
        self.reload_settings()

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        endpoint = (endpoint or DEFAULT_ENDPOINT).strip().rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError(f"Local AI endpoint must use http:// or https://: {endpoint!r}")
        return endpoint

    def reload_settings(self) -> None:
        """Reload the user-selected local endpoint/model when settings changed."""
        try:
            settings = config_manager.load_settings()
            endpoint = self._normalize_endpoint(settings.get("local_ai_url", self.endpoint))
            model = str(settings.get("local_ai_model", self.default_model) or "").strip()
            if endpoint != self.endpoint:
                with self._cache_lock:
                    self._cached_models = []
                    self._models_cached_at = 0.0
            self.endpoint = endpoint
            if model:
                self.default_model = model
        except Exception:
            # Keep the last known-good runtime configuration.
            pass

    @property
    def _endpoint_parts(self) -> urllib.parse.ParseResult:
        return urllib.parse.urlparse(self.endpoint)

    def _is_ollama(self) -> bool:
        parsed = self._endpoint_parts
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        return host in {"127.0.0.1", "localhost", "::1"} and path.endswith("/v1")

    def _management_url(self, path: str) -> str:
        parsed = self._endpoint_parts
        clean_path = parsed.path.rstrip("/")
        if self._is_ollama():
            clean_path = ""
        elif clean_path.endswith("/v1"):
            clean_path = clean_path[:-3]
        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, f"{clean_path}{path}", "", "", "")
        )

    @staticmethod
    def _decode_json_response(response) -> dict:
        raw = response.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Local AI returned invalid JSON: {raw[:300]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Local AI returned an unexpected JSON payload.")
        return data

    def is_available(self, *, force: bool = False) -> bool:
        """Return whether the configured local server is reachable."""
        self.reload_settings()
        now = time.monotonic()
        with self._cache_lock:
            if not force and self._models_cached_at and now - self._models_cached_at < MODEL_CACHE_TTL:
                return True

        if self._is_ollama():
            url = self._management_url("/api/tags")
        else:
            url = self._management_url("/models")

        try:
            request = urllib.request.Request(
                url,
                method="GET",
                headers={"Accept": "application/json", "User-Agent": "Brahma-Evo/3"},
            )
            with urllib.request.urlopen(request, timeout=2.5) as response:
                data = self._decode_json_response(response)
            raw_models = data.get("models")
            if not isinstance(raw_models, list):
                raw_models = data.get("data", [])
            models = []
            for model in raw_models or []:
                if isinstance(model, dict):
                    name = model.get("name") or model.get("id")
                    if name:
                        models.append(str(name))
            with self._cache_lock:
                self._cached_models = models
                self._models_cached_at = now
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        except Exception:
            return False

    def list_installed_models(self, *, force: bool = False) -> List[str]:
        """Return model IDs reported by the configured local runtime."""
        self.is_available(force=force)
        with self._cache_lock:
            return list(self._cached_models)

    def pull_model_async(self, model_name: str, progress_callback=None):
        """Pull an Ollama model in the background.

        The callback is executed on the worker thread. UI callers must marshal
        updates back onto the GUI thread (for example with QTimer.singleShot).
        """
        name = (model_name or "").strip()
        if not name:
            raise ValueError("model_name cannot be empty.")
        if not self._is_ollama():
            if progress_callback:
                progress_callback({"error": "Model downloads are only supported through an Ollama endpoint."})
            return None
        if not self._pull_lock.acquire(blocking=False):
            if progress_callback:
                progress_callback({"error": "A local model download is already in progress."})
            return None

        def _pull() -> None:
            try:
                payload = json.dumps({"name": name, "stream": True}).encode("utf-8")
                request = urllib.request.Request(
                    self._management_url("/api/pull"),
                    data=payload,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=1200) as response:
                    for raw_line in response:
                        if not raw_line:
                            continue
                        try:
                            chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except json.JSONDecodeError:
                            continue
                        if progress_callback:
                            try:
                                progress_callback(chunk)
                            except Exception:
                                # A UI callback should never kill the worker.
                                pass
                self.list_installed_models(force=True)
            except Exception as exc:
                if progress_callback:
                    try:
                        progress_callback({"error": str(exc)})
                    except Exception:
                        pass
            finally:
                self._pull_lock.release()

        thread = threading.Thread(
            target=_pull,
            daemon=True,
            name="brahma-local-model-pull",
        )
        thread.start()
        return thread

    def format_tools_for_local(
        self,
        tool_declarations: List[Dict[str, Any]],
        focus_core: bool = True,
    ) -> List[Dict[str, Any]]:
        """Convert Gemini/OpenAI tool declarations into OpenAI function tools."""
        formatted: List[Dict[str, Any]] = []
        for tool in tool_declarations or []:
            name = tool.get("name")
            if not name and isinstance(tool.get("function"), dict):
                name = tool["function"].get("name")
            if not name or (focus_core and name not in CORE_LOCAL_TOOL_NAMES):
                continue

            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = dict(tool["function"])
                fn["parameters"] = convert_schema_to_lowercase(
                    fn.get("parameters", {"type": "object", "properties": {}})
                )
            else:
                fn = {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": convert_schema_to_lowercase(
                        tool.get("parameters", {"type": "object", "properties": {}})
                    ),
                }
            formatted.append({"type": "function", "function": fn})
        return formatted

    def _request(self, payload: Dict[str, Any], timeout: float) -> dict:
        self.reload_settings()
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Brahma-Evo/3",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return self._decode_json_response(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"Local AI HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Local AI connection failed: {exc.reason}") from exc

    def generate_chat_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
    ) -> Generator[Dict[str, Any], None, None]:
        """Stream SSE chat-completion chunks from the configured runtime."""
        self.reload_settings()
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = self.format_tools_for_local(tools, focus_core=False)

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "Brahma-Evo/3",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90.0) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.startswith("data:"):
                        content = line[5:].strip()
                        if content == "[DONE]":
                            return
                        try:
                            yield json.loads(content)
                        except json.JSONDecodeError:
                            continue
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Local AI stream HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Local AI stream connection failed: {exc.reason}") from exc

    def chat_complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        focus_core: bool = True,
    ) -> Dict[str, Any]:
        """Run a non-streaming local completion."""
        payload: Dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = self.format_tools_for_local(tools, focus_core=focus_core)
        return self._request(payload, timeout=90.0)


local_brain = LocalBrain()
