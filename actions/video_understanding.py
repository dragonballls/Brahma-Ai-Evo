from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir

API_CONFIG_PATH = get_user_data_dir() / "config" / "api_keys.json"
DEFAULT_VIDEO_MODEL = os.environ.get("BRAHMA_VIDEO_MODEL", "gemini-2.5-flash")


def _gemini_api_key() -> str:
    try:
        data = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not load Brahma API configuration: {exc}") from exc
    key = str(data.get("gemini_api_key") or "").strip()
    if not key:
        raise RuntimeError("No Gemini API key is configured.")
    return key


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _result_text(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            candidates.append(str(part.get("text") or ""))
                elif isinstance(content, str):
                    candidates.append(content)
    elif isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    candidates.append(str(part.get("text") or ""))
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            content = step.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        candidates.append(str(part.get("text") or ""))
            elif isinstance(content, str):
                candidates.append(content)
    return _clean_text(" ".join(x for x in candidates if x))


def _build_prompt(question: str = "", start_time: str = "", end_time: str = "") -> str:
    scope = ""
    if start_time or end_time:
        scope = (
            f"Focus on the interval from {start_time or 'the beginning'} "
            f"to {end_time or 'the end'}. Do not rely only on the transcript; inspect the "
            f"visuals and audio in that interval."
        )
    task = question or "Watch this video and give me a clear, timestamp-aware summary of what matters."
    return (
        "You are JARVIS performing video understanding for the user. "
        "Inspect the video's visual frames, spoken dialogue, on-screen text, and audio cues. "
        "Answer the user's request precisely and distinguish observed facts from uncertainty. "
        "When the request concerns a code, symbol, message, or sequence, transcribe it before interpreting it. "
        "When appropriate, give timestamps so the user can jump to the relevant section. "
        f"{scope}\nUser request: {task}"
    ).strip()


def analyze_youtube(
    url: str,
    *,
    question: str = "",
    start_time: str = "",
    end_time: str = "",
    model: str | None = None,
) -> str:
    url = str(url or "").strip()
    if not re.search(r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/", url, re.I):
        raise ValueError("A public YouTube URL is required for YouTube video analysis.")

    key = _gemini_api_key()
    endpoint = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": model or DEFAULT_VIDEO_MODEL,
        "input": [
            {"type": "text", "text": _build_prompt(question, start_time, end_time)},
            {"type": "video", "uri": url},
        ],
    }

    import requests

    response = requests.post(
        endpoint,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=body,
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(
            f"Gemini video analysis failed ({response.status_code}): "
            f"{response.text[:1000]}"
        )

    payload = response.json()
    result = _result_text(payload)
    if not result:
        raise RuntimeError("Gemini returned no video-analysis text.")
    return result


def analyze_local_video(
    path: str,
    *,
    question: str = "",
    start_time: str = "",
    end_time: str = "",
    model: str | None = None,
) -> str:
    video_path = Path(str(path or "")).expanduser()
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=_gemini_api_key())
    uploaded = client.files.upload(file=str(video_path))
    try:
        for _ in range(60):
            state_name = str(getattr(getattr(uploaded, "state", None), "name", "") or "")
            if state_name in {"ACTIVE", "FAILED"}:
                break
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
        state_name = str(getattr(getattr(uploaded, "state", None), "name", "") or "")
        if state_name == "FAILED":
            raise RuntimeError("Gemini failed to process the uploaded video.")
        if state_name != "ACTIVE":
            raise RuntimeError("Gemini video processing did not become active.")

        mime = str(getattr(uploaded, "mime_type", "") or mimetypes.guess_type(video_path.name)[0] or "video/mp4")
        response = client.models.generate_content(
            model=model or DEFAULT_VIDEO_MODEL,
            contents=[
                types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime),
                _build_prompt(question, start_time, end_time),
            ],
        )
        text = _clean_text(getattr(response, "text", "") or "")
        if not text:
            raise RuntimeError("Gemini returned no local-video analysis text.")
        return text
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
