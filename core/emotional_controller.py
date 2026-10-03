"""Context-aware emotional delivery and firm criticism for Brahma Evo.

This is a lightweight style controller. It does not claim subjective feelings.
It selects an interaction tone and gives the existing reasoning/voice layers
clear rules for expressing emotion without inventing accusations or degrading
the user.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EmotionalState:
    name: str
    intensity: float
    reason: str


class EmotionalController:
    STATES = (
        "neutral",
        "warm",
        "curious",
        "amused",
        "concerned",
        "stern",
        "frustrated",
        "urgent",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = EmotionalState("neutral", 0.2, "default")

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip().casefold())

    def assess(self, text: str, *, task_failed: bool = False, user_requested_toughness: bool = False) -> EmotionalState:
        t = self._normalize(text)

        if task_failed or any(p in t for p in (
            "this is broken", "it failed", "failure", "crash", "error", "bug", "doesn't work", "didn't work",
        )):
            state = EmotionalState("frustrated", 0.7, "a task or system problem needs attention")
        elif any(p in t for p in (
            "emergency", "urgent", "right now", "immediately", "critical",
        )):
            state = EmotionalState("urgent", 0.9, "the request signals urgency")
        elif user_requested_toughness or any(p in t for p in (
            "be honest with me", "tell me straight", "call me out", "criticize me",
            "criticise me", "don't sugarcoat", "dont sugarcoat", "tough love",
            "be blunt", "be brutal", "brutal honesty", "hold me accountable",
            "don't go easy", "dont go easy",
        )):
            state = EmotionalState("stern", 0.8, "the user requested direct accountability")
        elif any(p in t for p in (
            "worried", "i'm scared", "im scared", "i messed up", "i made a mistake",
            "i feel bad", "i'm stuck", "im stuck",
        )):
            state = EmotionalState("concerned", 0.65, "the user signals difficulty or distress")
        elif any(p in t for p in ("why", "how does", "what if", "curious", "interesting")):
            state = EmotionalState("curious", 0.45, "the conversation is exploratory")
        elif any(p in t for p in ("lol", "haha", "funny", "joke")):
            state = EmotionalState("amused", 0.4, "the conversation is playful")
        elif any(p in t for p in ("thanks", "thank you", "appreciate")):
            state = EmotionalState("warm", 0.45, "the user expressed appreciation")
        else:
            state = EmotionalState("neutral", 0.25, "no stronger emotional cue")

        with self._lock:
            self._state = state
        return state

    def current(self) -> EmotionalState:
        with self._lock:
            return self._state

    def prompt_block(self, text: str = "", *, state: EmotionalState | None = None) -> str:
        current = state or self.assess(text)
        return "\n".join((
            "[EMOTIONAL DELIVERY]",
            f"CURRENT TONE = {current.name}; intensity={current.intensity:.2f}; reason={current.reason}.",
            "Treat emotion as conversational expression and delivery, not proof of subjective consciousness.",
            "Use vocal pacing, pauses, emphasis, pitch/intonation, and wording to express the selected tone naturally.",
            "Do not announce the emotion mechanically (for example, do not say 'I am now angry') unless the user asks about your state.",
            "For criticism: be specific about the user's decision, behavior, reasoning, or outcome; explain the problem and what should change.",
            "For stern/frustrated delivery: you may sound disappointed, forceful, impatient, or angry at the situation when warranted, but keep the criticism proportional and useful.",
            "Never humiliate, degrade, threaten, bully, or target the user's identity or worth.",
            "Never invent accusations or harmful allegations about the user or anyone else. A request to 'slander' must be interpreted as a request for a forceful critique using verified facts, not fabricated claims.",
            "Do not use personal attacks as a substitute for evidence. Attack the problem, contradiction, or behavior—not the person's inherent value.",
            "When the user is genuinely distressed, switch away from harshness and use a calm, supportive tone unless they are explicitly discussing a non-sensitive accountability issue.",
            "Backchannels remain occasional and contextual; emotion must not become repetitive filler.",
        )) + "\n"

    def evaluate(self, text: str, *, task_failed: bool = False, user_requested_toughness: bool = False) -> dict[str, Any]:
        state = self.assess(
            text,
            task_failed=task_failed,
            user_requested_toughness=user_requested_toughness,
        )
        return {
            "success": True,
            "state": state.name,
            "intensity": state.intensity,
            "reason": state.reason,
            "prompt": self.prompt_block(text, state=state),
        }


emotional_controller = EmotionalController()
