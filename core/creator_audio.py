"""Lightweight Creator audio recording and music discovery helpers."""
from __future__ import annotations

import wave
import numpy as np
from pathlib import Path
from typing import Any

def record_microphone(destination: str, duration_seconds: float, sample_rate: int = 44100) -> str:
    if duration_seconds <= 0 or duration_seconds > 7200:
        raise ValueError("Recording duration must be between 0 and 7200 seconds.")
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("Microphone recording requires sounddevice.") from exc
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration_seconds * sample_rate)
    recording = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    pcm = np.clip(recording, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16.tobytes())
    return str(target)

def recommend_music(query: str, mood: str = "", player=None) -> str:
    request = "copyright-safe or properly licensed music " + str(query or mood or "background music")
    from actions.web_search import web_search
    result = web_search(
        {"query": request, "mode": "search"},
        player=player,
    )
    return result
