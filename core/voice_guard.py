from __future__ import annotations

import json
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any


_WAKE_PHRASES = {
    "brahma evo",
    "hey brahma evo",
    "hi brahma evo",
    "hello brahma evo",
}


def normalize_voice_text(text: str) -> str:
    """Normalize speech transcripts for duplicate/noise checks."""
    text = re.sub(r"[^a-z0-9\s']+", " ", (text or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_wake_phrase_only(text: str) -> bool:
    return normalize_voice_text(text) in _WAKE_PHRASES


class VoiceCommandGate:
    """Reject duplicate/empty microphone transcripts before they reach command routing."""

    def __init__(
        self,
        *,
        duplicate_window_s: float = 7.0,
        similarity_threshold: float = 0.94,
        min_text_length: int = 2,
    ) -> None:
        self.duplicate_window_s = max(0.5, float(duplicate_window_s))
        self.similarity_threshold = max(0.80, min(1.0, float(similarity_threshold)))
        self.min_text_length = max(1, int(min_text_length))
        self._lock = threading.Lock()
        self._last_text = ""
        self._last_at = 0.0

    def accept(self, text: str) -> bool:
        normalized = normalize_voice_text(text)
        if len(normalized) < self.min_text_length:
            return False

        now = time.monotonic()
        with self._lock:
            if self._last_text and (now - self._last_at) < self.duplicate_window_s:
                same = normalized == self._last_text
                similar = (
                    len(normalized) >= 8
                    and len(self._last_text) >= 8
                    and SequenceMatcher(None, normalized, self._last_text).ratio()
                    >= self.similarity_threshold
                )
                if same or similar:
                    return False
            self._last_text = normalized
            self._last_at = now
        return True


def _stable_signature(tool_name: str, args: dict[str, Any]) -> str:
    try:
        payload = json.dumps(
            {"name": tool_name, "args": args or {}},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
    except Exception:
        payload = f"{tool_name}:{args}"
    return payload


class VoiceToolExecutionGate:
    """Prevent duplicate side-effecting tool calls from one voice turn or rapid repeats."""

    _RAPID_REPEAT_TOOLS = {
        "computer_control",
        "connect_execute",
        "google_workspace",
        "workspace",
        "skill_forge",
        "dynamic_skill",
        "auto_heal",
        "rollback",
        "shutdown_brahma",
        "test_action",
        "upload_video",
    }

    def __init__(self, *, rapid_repeat_window_s: float = 6.0) -> None:
        self.rapid_repeat_window_s = max(1.0, float(rapid_repeat_window_s))
        self._lock = threading.Lock()
        self._turn_id = 0
        self._turn_text_parts: list[str] = []
        self._used_this_turn: set[str] = set()
        self._recent: dict[str, float] = {}

    @property
    def has_active_input(self) -> bool:
        with self._lock:
            return bool(" ".join(self._turn_text_parts).strip())

    @property
    def current_text(self) -> str:
        with self._lock:
            return " ".join(self._turn_text_parts).strip()

    @property
    def turn_id(self) -> int:
        with self._lock:
            return self._turn_id

    def add_input_fragment(self, text: str) -> None:
        normalized = normalize_voice_text(text)
        if not normalized:
            return
        with self._lock:
            if not self._turn_text_parts:
                self._turn_id += 1
                self._used_this_turn.clear()
            # Live transcription can send partial/cumulative fragments. Do not
            # turn "open chrome" + "open chrome now" into one duplicated command.
            if self._turn_text_parts:
                last = self._turn_text_parts[-1]
                if normalized == last:
                    return
                if normalized.startswith(last + " ") or last.startswith(normalized + " "):
                    self._turn_text_parts[-1] = max((last, normalized), key=len)
                    return
            self._turn_text_parts.append(normalized)

    def allow(self, tool_name: str, args: dict[str, Any] | None = None) -> tuple[bool, str]:
        tool_name = str(tool_name or "").strip()
        if not tool_name:
            return False, "missing tool name"

        with self._lock:
            transcript = " ".join(self._turn_text_parts).strip()
            if not transcript:
                return False, "no fresh voice transcript is associated with this action"

            if is_wake_phrase_only(transcript):
                return False, "wake phrase detected without a command"

            signature = _stable_signature(tool_name, args or {})
            if signature in self._used_this_turn:
                return False, "duplicate tool call in the same voice turn"

            now = time.monotonic()
            previous = self._recent.get(signature)
            if (
                previous is not None
                and (now - previous) < self.rapid_repeat_window_s
                and tool_name in self._RAPID_REPEAT_TOOLS
            ):
                return False, "rapid duplicate of a recent side-effecting voice action"

            self._used_this_turn.add(signature)
            self._recent[signature] = now

            cutoff = now - max(self.rapid_repeat_window_s, 10.0)
            self._recent = {key: stamp for key, stamp in self._recent.items() if stamp >= cutoff}
            return True, "ok"

    def finish_turn(self) -> None:
        with self._lock:
            self._turn_text_parts.clear()
            self._used_this_turn.clear()
