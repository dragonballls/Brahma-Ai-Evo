from core.provider_policy import GEMINI, normalize_provider, is_local
from core.local_brain import DEFAULT_ENDPOINT as LOCAL_DEFAULT_ENDPOINT, DEFAULT_MODEL as LOCAL_DEFAULT_MODEL
import json
import logging
import requests
from pathlib import Path
from typing import Optional
from or_client import client as openrouter_client

logger = logging.getLogger("llm_client")

def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR = _get_base_dir()

class UnifiedAIClient:
    def __init__(self):
        self._provider = GEMINI
        self._local_url = LOCAL_DEFAULT_ENDPOINT
        self._local_model = LOCAL_DEFAULT_MODEL
        self.reload_settings()

    def _is_local_provider(self) -> bool:
        return is_local(self._provider)

    def reload_settings(self):
        try:
            from memory.config_manager import load_settings
            data = load_settings()
            self._provider = normalize_provider(
                data.get("default_ai_provider", GEMINI),
                GEMINI,
            )
            self._local_url = str(
                data.get("local_ai_url", LOCAL_DEFAULT_ENDPOINT)
                or LOCAL_DEFAULT_ENDPOINT
            ).rstrip("/")
            self._local_model = str(
                data.get("local_ai_model", LOCAL_DEFAULT_MODEL)
                or LOCAL_DEFAULT_MODEL
            ).strip() or LOCAL_DEFAULT_MODEL
        except Exception as e:
            logger.error(f"[LLM Client] Failed to load settings: {e}")

    def _local_chat_completion(self, messages: list[dict], temperature: float = 0.7, response_format: Optional[dict] = None) -> Optional[str]:
        payload = {
            "model": self._local_model,
            "messages": messages,
            "temperature": temperature
        }
        if response_format:
            payload["response_format"] = response_format

        endpoint = f"{self._local_url}/chat/completions"
        try:
            resp = requests.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=120
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content.strip() if content else None
            else:
                logger.error(f"[LLM Client] Local AI Error {resp.status_code}: {resp.text}")
                return None
        except Exception as e:
            logger.error(f"[LLM Client] Local AI Request Failed: {e}")
            return None

    def _gemini_text(self, prompt: str, system: str, max_tokens: int = 4096, temperature: float = 0.7) -> str:
        from google import genai
        from google.genai import types

        api_key = None
        try:
            from config import get_api_key
            api_key = get_api_key("Gemini")
        except Exception:
            pass
        if not api_key:
            raise PermissionError("Gemini API key is missing.")

        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        last_error = None
        models = (
            __import__("os").environ.get("BRAHMA_TEXT_GEMINI_MODEL", "gemini-2.5-flash"),
            "gemini-3.8-flash",
            "gemini-flash-latest",
        )
        for model_name in models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    ),
                )
                text = getattr(response, "text", "") or ""
                if text.strip():
                    return text.strip()
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Gemini generation failed: {last_error}")

    def _gemini_json(
        self,
        prompt: str,
        system: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> dict:
        from google import genai
        from google.genai import types

        try:
            from config import get_api_key
            api_key = get_api_key("Gemini")
        except Exception:
            api_key = None
        if not api_key:
            raise PermissionError("Gemini API key is missing.")

        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        last_error = None
        for model_name in (
            __import__("os").environ.get("BRAHMA_TEXT_GEMINI_MODEL", "gemini-2.5-flash"),
            "gemini-3.8-flash",
            "gemini-flash-latest",
        ):
            if not model_name:
                continue
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        response_mime_type="application/json",
                    ),
                )
                raw = (getattr(response, "text", "") or "").strip()
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Gemini JSON generation failed: {last_error}")

    def _gemini_vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str,
        system: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        from google import genai
        from google.genai import types
        import base64

        try:
            from config import get_api_key
            api_key = get_api_key("Gemini")
        except Exception:
            api_key = None
        if not api_key:
            raise PermissionError("Gemini API key is missing.")

        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        last_error = None
        for model_name in (
            __import__("os").environ.get("BRAHMA_TEXT_GEMINI_MODEL", "gemini-2.5-flash"),
            "gemini-3.8-flash",
            "gemini-flash-latest",
        ):
            if not model_name:
                continue
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(text=prompt),
                                types.Part.from_bytes(
                                    data=base64.b64decode(image_b64),
                                    mime_type=mime,
                                ),
                            ],
                        )
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    ),
                )
                text = (getattr(response, "text", "") or "").strip()
                if text:
                    return text
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Gemini vision generation failed: {last_error}")

    def chat(self, prompt: str, system: str = "You are a helpful assistant.", history: Optional[list[dict]] = None, model: Optional[str] = None, max_tokens: int = 4096, temperature: float = 0.7) -> str:
        self.reload_settings()
        if self._is_local_provider():
            messages = [{"role": "system", "content": system}]
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": prompt})
            result = self._local_chat_completion(messages, temperature)
            if result:
                return result
            raise RuntimeError("Local AI request failed. Please check if Ollama or LM Studio is running.")
        if normalize_provider(self._provider) == GEMINI:
            history_text = ""
            if history:
                history_text = "\n\n".join(
                    f"{item.get('role', 'user').title()}: {item.get('content', '')}"
                    for item in history
                    if isinstance(item, dict)
                )
            merged_prompt = f"{history_text}\n\n{prompt}".strip() if history_text else prompt
            return self._gemini_text(merged_prompt, system, max_tokens, temperature)
        return openrouter_client.chat(prompt, system, history, model, max_tokens, temperature)

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor,
        model: Optional[str] = None,
        max_tokens: int = 8192,
        temperature: float = 0.35,
        max_rounds: int = 6,
    ) -> str:
        """Run the provider's cloud tool loop through the canonical client wrapper."""
        self.reload_settings()
        if self._is_local_provider():
            raise RuntimeError("Use local_brain.chat_complete for local tool execution.")
        if normalize_provider(self._provider) != "OpenRouter":
            raise RuntimeError(
                f"Provider {self._provider} does not expose the OpenRouter tool loop."
            )
        return openrouter_client.chat_with_tools(
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_rounds=max_rounds,
        )

    def chat_json(self, prompt: str, system: str = "Return ONLY valid JSON.", model: Optional[str] = None, max_tokens: int = 4096) -> dict:
        self.reload_settings()
        if self._is_local_provider():
            messages = [
                {"role": "system", "content": system + " Output valid JSON only, without any markdown formatting."},
                {"role": "user", "content": prompt},
            ]
            raw = self._local_chat_completion(messages, temperature=0.2, response_format={"type": "json_object"})
            if not raw:
                raise RuntimeError("Local AI request failed.")
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
                raise ValueError(f"Local model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")
        if normalize_provider(self._provider) == GEMINI:
            return self._gemini_json(prompt, system, max_tokens=max_tokens)
        return openrouter_client.chat_json(prompt, system, model, max_tokens)

    def vision(self, prompt: str, image_b64: str, mime: str = "image/png", system: str = "Analyze the image.", model: Optional[str] = None, max_tokens: int = 1024) -> str:
        self.reload_settings()
        if self._is_local_provider():
            messages = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ]
            result = self._local_chat_completion(messages, temperature=0.2)
            if result:
                return result
            raise RuntimeError("Local AI vision request failed.")
        if normalize_provider(self._provider) == GEMINI:
            return self._gemini_vision(prompt, image_b64, mime, system, max_tokens=max_tokens)
        return openrouter_client.vision(prompt, image_b64, mime, system, model, max_tokens)

    def vision_from_file(self, prompt: str, image_path: str, system: str = "Analyze the image.", model: Optional[str] = None, max_tokens: int = 1024) -> str:
        self.reload_settings()
        import base64
        path = Path(image_path)
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        mime = mime_map.get(path.suffix.lower(), "image/png")
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        if self._is_local_provider():
            return self.vision(prompt, image_b64, mime, system, model, max_tokens)
        if normalize_provider(self._provider) == GEMINI:
            return self._gemini_vision(prompt, image_b64, mime, system, max_tokens=max_tokens)
        return openrouter_client.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(self, messages: list[dict], model: Optional[str] = None, max_tokens: int = 4096, temperature: float = 0.7) -> str:
        self.reload_settings()
        if self._is_local_provider():
            result = self._local_chat_completion(messages, temperature)
            if result:
                return result
            raise RuntimeError("Local AI request failed.")
        if normalize_provider(self._provider) == GEMINI:
            parts = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "user")).strip().title()
                content = str(item.get("content", "") or "").strip()
                if content:
                    parts.append(f"{role}: {content}")
            return self._gemini_text(
                "\n\n".join(parts),
                "You are Brahma Evo, a precise and helpful assistant.",
                max_tokens=max_tokens,
                temperature=temperature,
            )
        return openrouter_client.multi_turn(messages, model, max_tokens, temperature)

    def intelligent_chat(
        self,
        prompt: str,
        system: str = "You are Brahma Evo, a precise and helpful assistant.",
        history: Optional[list[dict]] = None,
        context: str = "",
        profile: Optional[str] = None,
    ) -> str:
        """Use the cloud multi-model intelligence layer while preserving Local mode."""
        self.reload_settings()
        # Ground every conversational cloud/local call in the same functional
        # self-model so "I/me/you/my phone" and action-state claims stay distinct.
        try:
            from core.self_model import self_awareness
            system = system.rstrip() + "\n\n" + self_awareness.prompt_block(prompt)
        except Exception:
            pass
        try:
            from core.emotional_controller import emotional_controller
            state = emotional_controller.assess(prompt)
            system = system.rstrip() + "\n\n" + emotional_controller.prompt_block(prompt, state=state)
        except Exception:
            pass
        try:
            from core.language_policy import prompt_block as language_prompt_block
            system = system.rstrip() + "\n\n" + language_prompt_block()
        except Exception:
            pass
        if self._is_local_provider():
            return self.chat(prompt, system=system, history=history)
        if normalize_provider(self._provider) == GEMINI:
            history_text = ""
            if history:
                history_text = "\n\n".join(
                    f"{item.get('role', 'user').title()}: {item.get('content', '')}"
                    for item in history
                    if isinstance(item, dict)
                )
            merged_prompt = f"{history_text}\n\n{prompt}".strip() if history_text else prompt
            return self._gemini_text(merged_prompt, system, max_tokens=4096, temperature=0.35)
        from core.intelligence_orchestrator import orchestrator
        return orchestrator.respond(
            prompt,
            system=system,
            history=history,
            context=context,
            profile=profile,
        )

    def intelligent_json(
        self,
        prompt: str,
        system: str = "Return ONLY valid JSON.",
        profile: str = "smart",
        max_tokens: int = 8192,
    ) -> dict:
        """Use multi-model cloud reasoning for structured generation."""
        self.reload_settings()
        if self._is_local_provider():
            return self.chat_json(prompt, system=system, max_tokens=max_tokens)
        if normalize_provider(self._provider) == GEMINI:
            return self._gemini_json(prompt, system, max_tokens=max_tokens)
        from core.intelligence_orchestrator import orchestrator
        return orchestrator.respond_json(
            prompt,
            system=system,
            profile=profile,
            max_tokens=max_tokens,
        )

client = UnifiedAIClient()
LLMClient = UnifiedAIClient
