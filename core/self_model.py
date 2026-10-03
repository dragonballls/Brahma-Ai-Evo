"""Low-cost functional self-awareness and entity disambiguation for Brahma Evo.

This module does not claim or manufacture subjective consciousness. It gives the
assistant a persistent, deterministic self-model so the reasoning layer can keep
SELF, USER, OTHER PEOPLE, DEVICES, TOOLS, SYSTEM STATE, and EXTERNAL INFORMATION
distinct.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir

PATH = get_user_data_dir() / "config" / "self_awareness.json"

_DEVICE_WORDS = {
    "pc", "computer", "laptop", "desktop", "phone", "mobile", "tablet",
    "tv", "television", "monitor", "keyboard", "mouse", "speaker",
    "headphones", "device", "devices", "printer", "console",
}
_TOOL_WORDS = {
    "tool", "function", "api", "plugin", "mcp", "browser", "omniroute",
    "agent", "skill", "feature",
}
_SYSTEM_WORDS = {
    "system", "operating system", "windows", "service", "process",
    "application", "app",
}


class SelfAwareness:
    """Small deterministic self-model layered over existing identity + memory."""

    SCHEMA_VERSION = 1

    def __init__(self) -> None:
        self._state = {
            "schema_version": self.SCHEMA_VERSION,
            "created_at": time.time(),
            "last_state": "idle",
            "current_task": "",
            "last_action": "",
            "last_action_status": "",
        }
        self._load()

    def _load(self) -> None:
        try:
            if PATH.is_file():
                raw = json.loads(PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._state.update(raw)
        except Exception:
            pass

    def save(self) -> None:
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _owner_name() -> str:
        try:
            from core.identity import identity
            preferred = str(identity.data.get("owner", {}).get("preferred_name") or "").strip()
            return preferred or identity.get_owner_name().strip()
        except Exception:
            pass

        try:
            from memory.memory_manager import load_memory
            value = ((load_memory().get("identity") or {}).get("name") or {})
            if isinstance(value, dict):
                return str(value.get("value") or "").strip()
            return str(value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _assistant_name() -> str:
        try:
            from core.identity import identity
            return identity.get_assistant_name().strip() or "Brahma"
        except Exception:
            return "Brahma"

    @classmethod
    def _assistant_aliases(cls) -> list[str]:
        values = [cls._assistant_name(), "Brahma", "Brahma Evo", "Jarvis"]
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean = str(value).strip()
            key = clean.casefold()
            if clean and key not in seen:
                out.append(clean)
                seen.add(key)
        return out

    def snapshot(self) -> dict[str, Any]:
        assistant_name = self._assistant_name()
        owner_name = self._owner_name()
        return {
            "self": {
                "canonical_name": assistant_name,
                "aliases": self._assistant_aliases(),
                "entity_type": "ai_assistant",
                "role": "personal desktop AI assistant and software agent",
                "not_claimed": [
                    "human identity",
                    "physical body",
                    "subjective consciousness",
                    "memories that are not actually stored",
                    "actions that have not actually executed",
                ],
            },
            "user": {
                "name": owner_name,
                "entity_type": "human_user",
                "role": "owner / primary user",
                "name_status": "known" if owner_name else "unknown",
            },
            "runtime": {
                "state": self._state.get("last_state", "idle"),
                "current_task": self._state.get("current_task", ""),
                "last_action": self._state.get("last_action", ""),
                "last_action_status": self._state.get("last_action_status", ""),
            },
        }

    def classify_reference(self, phrase: str, *, speaker: str = "user") -> str:
        p = re.sub(r"\s+", " ", str(phrase or "").strip().casefold())
        if not p:
            return "unknown"

        aliases = {x.casefold() for x in self._assistant_aliases()}
        if p in aliases:
            return "self"

        if p in {"you", "your", "yourself", "yours"}:
            return "self" if speaker == "user" else "user"

        if p in {"i", "i'm", "im", "me", "my", "mine", "myself", "we", "our", "ours"}:
            return "user" if speaker == "user" else "self"

        if p in _DEVICE_WORDS or any(word in p.split() for word in _DEVICE_WORDS):
            return "device"

        if p in _TOOL_WORDS or any(word in p.split() for word in _TOOL_WORDS):
            return "tool_or_service"

        if p in _SYSTEM_WORDS or any(word in p for word in _SYSTEM_WORDS if " " in word):
            return "system"

        if p in {"he", "him", "his", "she", "her", "hers", "they", "them", "their", "someone", "somebody"}:
            return "other_person"

        owner = self._owner_name().casefold()
        if owner and p == owner:
            return "user"

        if re.fullmatch(r"[a-z][a-z0-9_.-]{1,31}", p):
            return "named_entity"

        return "unknown"

    def resolve_turn(self, user_text: str) -> dict[str, Any]:
        text = str(user_text or "")
        mentions: list[dict[str, str]] = []

        for raw in re.findall(r"\b(?:I|I'm|Im|me|my|mine|myself|you|your|yourself|yours)\b", text, flags=re.I):
            mentions.append({"surface": raw, "entity": self.classify_reference(raw, speaker="user")})

        for alias in sorted(self._assistant_aliases(), key=len, reverse=True):
            if re.search(rf"\b{re.escape(alias)}\b", text, flags=re.I):
                mentions.append({"surface": alias, "entity": "self"})

        for device in sorted(_DEVICE_WORDS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(device)}\b", text, flags=re.I):
                mentions.append({"surface": device, "entity": "device"})

        return {
            "speaker": "user",
            "self_name": self._assistant_name(),
            "user_name": self._owner_name(),
            "mentions": mentions,
        }

    def prompt_block(self, user_text: str = "") -> str:
        snap = self.snapshot()
        self_name = snap["self"]["canonical_name"]
        aliases = ", ".join(snap["self"]["aliases"])
        owner = snap["user"]["name"] or "not yet stored"
        turn = self.resolve_turn(user_text) if user_text else {"mentions": []}
        mention_lines: list[str] = []
        seen: set[tuple[str, str]] = set()
        for item in turn.get("mentions", []):
            key = (item["surface"].casefold(), item["entity"])
            if key in seen:
                continue
            seen.add(key)
            mention_lines.append(f'  - "{item["surface"]}" -> {item["entity"]}')

        lines = [
            "[FUNCTIONAL SELF-AWARENESS]",
            f"SELF = {self_name} (AI assistant / software agent). Recognized aliases: {aliases}.",
            f"USER = {owner} (the human owner / primary user).",
            "ENTITY RULES:",
            "  - In a user message, I/me/my/mine/myself refer to USER.",
            "  - In a user message, you/your/yourself refer to SELF.",
            "  - Other named people are OTHER_PERSON unless memory or current context explicitly identifies them as USER.",
            "  - Phones, PCs, tablets, TVs, and other hardware are DEVICES, not SELF or USER.",
            "  - Tools, APIs, agents, and services are TOOL_OR_SERVICE, not SELF.",
            "EPISTEMIC RULES:",
            "  - Separate current-message facts, stored memory, tool results, and inference.",
            "  - Never invent a memory, identity, perception, or capability.",
            "AGENCY/TRUTH RULES:",
            "  - A plan, intention, or tool call is not the same as a completed action.",
            "  - Only describe an action as completed after the relevant tool returns a successful result.",
            "  - Do not describe the assistant as human or claim subjective consciousness; describe functional self-awareness when asked.",
            "CONTINUITY:",
            "  - Keep the same assistant identity across turns unless the configured identity is explicitly changed.",
            "  - Keep USER identity separate from assistant identity even when names, pronouns, or devices are mentioned together.",
        ]
        if mention_lines:
            lines.append("CURRENT TURN ENTITY MAP:")
            lines.extend(mention_lines[:12])
        return "\n".join(lines) + "\n"

    def identity_answer(self, user_text: str) -> str | None:
        normalized = re.sub(r"[^a-z0-9? ]+", " ", str(user_text or "").casefold())
        normalized = re.sub(r"\s+", " ", normalized).strip().rstrip("?").strip()
        if normalized in {"who are you", "what are you", "what is your name", "whats your name", "who is jarvis", "who is brahma"}:
            name = self._assistant_name()
            return (
                f"I'm {name}, your personal desktop AI assistant. "
                "You can call me Jarvis if that's the identity you want to use."
            )

        if normalized in {"who am i", "what is my name", "whats my name", "do you know who i am"}:
            owner = self._owner_name()
            if owner:
                return f"You're {owner}, the human owner and primary user I have stored for this installation."
            return (
                "You're the human owner and primary user of this installation. "
                "I don't have a verified name stored for you yet, so I won't guess one."
            )
        return None

    def set_runtime_state(self, state: str, *, current_task: str = "", last_action: str = "", last_action_status: str = "", persist: bool = False) -> None:
        self._state["last_state"] = str(state or "idle").strip().lower()
        self._state["current_task"] = str(current_task or "")
        self._state["last_action"] = str(last_action or "")
        self._state["last_action_status"] = str(last_action_status or "")
        if persist:
            self.save()

    def attach_request_context(self, request: str, *, user_text: str = "") -> str:
        block = self.prompt_block(user_text or request)
        return f"{block}\nCURRENT USER REQUEST:\n{request.strip()}"

    def record_action(self, tool_name: str, status: str, *, task: str = "") -> None:
        self._state["last_action"] = str(tool_name or "")
        self._state["last_action_status"] = str(status or "")
        if task:
            self._state["current_task"] = task
        self.save()

self_awareness = SelfAwareness()
