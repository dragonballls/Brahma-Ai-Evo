"""Lightweight natural-language request routing helpers.

This module intentionally has no GUI, audio, LLM, or desktop dependencies so the
routing contract can be tested and reused without importing the full application.
"""
from __future__ import annotations

import re

from core.capability_catalog import CAPABILITY_ROUTING_TERMS


def _looks_like_action_request(text: str) -> bool:
    """Conservatively detect requests likely to require an actionable capability."""
    low = re.sub(r"\s+", " ", (text or "").casefold()).strip()
    if not low:
        return False
    action_phrases = (
        "open ", "launch ", "start ", "run ", "execute ", "set ", "change ",
        "turn on", "turn off", "mute ", "unmute ", "increase ", "decrease ",
        "send ", "post ", "publish ", "create ", "delete ", "remove ", "move ",
        "copy ", "rename ", "download ", "install ", "search ", "browse ",
        "navigate ", "check ", "diagnose ", "control ", "play ", "pause ", "stop ",
        "schedule ", "remind ", "call ", "message ", "email ", "compose ",
        "write ", "edit ", "fix ", "build ", "implement ", "update ", "connect ",
        "disconnect ", "take a screenshot", "look at my screen",
        "help me with ", "show ", "what can you do with ", "use ",
    )
    if any(low.startswith(phrase) for phrase in action_phrases):
        return True
    if any(
        re.search(rf"\b(?:can|could|would|will|please) you\s+{re.escape(verb)}\b", low)
        for verb in (
            "open", "launch", "start", "run", "execute", "set", "change",
            "turn", "send", "post", "create", "delete", "move", "copy",
            "rename", "download", "install", "search", "browse", "navigate",
            "check", "diagnose", "control", "play", "pause", "stop", "schedule",
            "remind", "call", "message", "email", "write", "edit", "fix",
            "build", "implement", "update", "connect", "disconnect", "show",
        )
    ):
        return True
    if any(
        re.search(rf"\b{re.escape(term.casefold())}\b", low)
        for term in CAPABILITY_ROUTING_TERMS
    ):
        return True
    return False
