"""Low-cost reflective memory layer inspired by Hindsight.

This is intentionally adapter-shaped: Brahma keeps its existing JSON memory
store while gaining explicit retain/recall/reflect semantics without adding a
database or network service.
"""

from __future__ import annotations

import re
from typing import Any

_MAX_CHARS = 2200
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you",
    "are", "was", "were", "have", "has", "had", "into", "about", "what",
    "when", "where", "how", "can", "please", "brahma", "me", "my", "i",
}


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9_+-]{2,}", str(text or "").casefold())
        if token not in _STOPWORDS
    }


def _entry_rows(memory: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for category, values in memory.items():
        if category == "sessions":
            continue
        if not isinstance(values, dict):
            continue
        for key, entry in values.items():
            if isinstance(entry, dict):
                value = str(entry.get("value") or entry.get("text") or "").strip()
            else:
                value = str(entry).strip()
            if value:
                rows.append((str(category), str(key), value))
    return rows


class ReflectiveMemory:
    def recall(self, query: str, *, limit: int = 6) -> list[dict[str, str]]:
        from memory.memory_manager import load_memory
        q = _tokens(query)
        scored: list[tuple[int, dict[str, str]]] = []
        for category, key, value in _entry_rows(load_memory()):
            hay = _tokens(f"{category} {key} {value}")
            score = len(q & hay)
            if score:
                scored.append((score, {"category": category, "key": key, "value": value}))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[: max(1, min(int(limit), 12))]]

    def reflect(self, query: str, *, limit: int = 6, max_chars: int = _MAX_CHARS) -> str:
        rows = self.recall(query, limit=limit)
        if not rows:
            return ""
        lines = [
            "REFLECTIVE MEMORY CONTEXT",
            "Use relevant remembered facts for continuity; do not invent facts.",
        ]
        for row in rows:
            lines.append(f"- {row['category']}/{row['key']}: {row['value']}")
        text = "\n".join(lines)
        return text[:max_chars]

    def retain(self, user_text: str, assistant_text: str = "") -> dict[str, Any]:
        try:
            from memory.memory_manager import auto_learn_interaction
            return dict(auto_learn_interaction(user_text, assistant_text) or {})
        except Exception:
            return {}


reflective_memory = ReflectiveMemory()
