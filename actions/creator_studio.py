"""Brahma EVO AI Creator Studio."""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
import textwrap
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from core.user_paths import get_user_data_dir

logger = logging.getLogger("brahma.creator")
CREATOR_ROOT = get_user_data_dir() / "CreatorProjects"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

class CreatorError(RuntimeError):
    """Expected creator-pipeline failure."""

@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    has_audio: bool
    video_codec: str
    audio_codec: str
    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

def _which(name: str) -> str | None:
    return shutil.which(name)

def _run(command: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CreatorError(f"Media command failed to start: {exc}") from exc

def _require_ffmpeg() -> str:
    exe = _which("ffmpeg")
    if not exe:
        raise CreatorError("FFmpeg is not installed or is not on PATH.")
    return exe

def _require_ffprobe() -> str:
    exe = _wich("ffprobe")
    if not exe:
        raise CreatorError("FFprobe is not installed or is not on PATH.")
    return exe

def _json_from_text(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
    start, end = raw.find("{