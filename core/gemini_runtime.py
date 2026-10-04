"""Canonical Gemini runtime for Brahma Evo.

All non-Live Gemini features use this module. It owns credential lookup,
the current google-genai SDK, model fallback, and compatibility for old
model.generate_content call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import os
from typing import Any

DEFAULT_TEXT_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-flash-latest",
)


def get_api_key() -> str:
    from config import get_api_key as get_configured_api_key

    key = str(get_configured_api_key("Gemini") or "").strip()
    if not key:
        raise RuntimeError("Gemini API key is not configured.")
    return key


def _models(preferred: str | None = None) -> tuple[str, ...]:
    configured = str(
        preferred
        or os.environ.get("BRAHMA_TEXT_GEMINI_MODEL", "")
        or DEFAULT_TEXT_MODELS[0]
    ).strip()
    values = [configured, *DEFAULT_TEXT_MODELS]
    seen: set[str] = set()
    return tuple(x for x in values if x and not (x in seen or seen.add(x)))


def _to_part(item: Any):
    from google.genai import types

    if isinstance(item, types.Part):
        return item
    if isinstance(item, str):
        return types.Part.from_text(text=item)
    if isinstance(item, bytes):
        return types.Part.from_bytes(data=item, mime_type="application/octet-stream")
    if isinstance(item, dict) and "data" in item:
        data = item.get("data")
        if isinstance(data, str) and item.get("encoding") == "base64":
            import base64
            data = base64.b64decode(data)
        if isinstance(data, (bytes, bytearray)):
            return types.Part.from_bytes(
                data=bytes(data),
                mime_type=str(item.get("mime_type") or "application/octet-stream"),
            )
    if hasattr(item, "save"):
        buffer = BytesIO()
        item.save(buffer, format="PNG")
        return types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png")
    return types.Part.from_text(text=str(item))


def _normalize_contents(contents: Any):
    from google.genai import types

    if isinstance(contents, str):
        return types.Content(role="user", parts=[types.Part.from_text(text=contents)])
    if isinstance(contents, (list, tuple)):
        return types.Content(
            role="user",
            parts=[_to_part(item) for item in contents],
        )
    if isinstance(contents, types.Content):
        return contents
    return types.Content(role="user", parts=[_to_part(contents)])


def _make_config(
    generation_config: Any = None,
    *,
    system_instruction: str | None = None,
    response_mime_type: str | None = None,
):
    from google.genai import types

    values: dict[str, Any] = {}
    if isinstance(generation_config, dict):
        values.update(generation_config)
    elif generation_config is not None:
        return generation_config

    if system_instruction is not None:
        values["system_instruction"] = system_instruction
    if response_mime_type is not None:
        values["response_mime_type"] = response_mime_type

    return types.GenerateContentConfig(**values)


@dataclass
class GeminiTextResponse:
    text: str


class GeminiModelAdapter:
    """Compatibility wrapper with a model.generate_content-style API."""

    def __init__(self, model_name: str | None = None, system_instruction: str | None = None):
        self.model_name = model_name
        self.system_instruction = system_instruction

    def generate_content(
        self,
        contents: Any,
        *,
        generation_config: Any = None,
        config: Any = None,
        **kwargs: Any,
    ) -> GeminiTextResponse:
        from google import genai

        client = genai.Client(
            api_key=get_api_key(),
            http_options={"api_version": "v1beta"},
        )
        config_value = config if config is not None else generation_config
        if kwargs:
            config_dict = dict(config_value or {}) if isinstance(config_value, dict) else {}
            config_dict.update(kwargs)
            config_value = config_dict

        last_error: Exception | None = None
        for model_name in _models(self.model_name):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=_normalize_contents(contents),
                    config=_make_config(
                        config_value,
                        system_instruction=self.system_instruction,
                    ),
                )
                text = str(getattr(response, "text", "") or "").strip()
                if text:
                    return GeminiTextResponse(text=text)
                last_error = RuntimeError(
                    f"Gemini model {model_name} returned an empty response."
                )
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"Gemini generation failed: {last_error}")


def create_model(
    model_name: str | None = None,
    system_instruction: str | None = None,
) -> GeminiModelAdapter:
    return GeminiModelAdapter(
        model_name=model_name,
        system_instruction=system_instruction,
    )


def generate_text(
    prompt: str,
    *,
    system_instruction: str | None = None,
    model_name: str | None = None,
    max_output_tokens: int = 4096,
    temperature: float = 0.2,
    response_mime_type: str | None = None,
) -> str:
    model = create_model(model_name, system_instruction)
    response = model.generate_content(
        prompt,
        generation_config={
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
            **({"response_mime_type": response_mime_type} if response_mime_type else {}),
        },
    )
    return response.text


def generate_json(
    prompt: str,
    *,
    system_instruction: str = "Return ONLY valid JSON.",
    model_name: str | None = None,
    max_output_tokens: int = 8192,
) -> dict[str, Any]:
    raw = generate_text(
        prompt,
        system_instruction=system_instruction,
        model_name=model_name,
        max_output_tokens=max_output_tokens,
        temperature=0.2,
        response_mime_type="application/json",
    )
    return json.loads(raw)
