"""Jev-controlled memory gates around Brahma's existing durable stores.

This is intentionally a compatibility layer: existing JSON/SQLite memory remains
the source of truth; Jev only makes bounded admission/relevance/eviction decisions.
"""
from __future__ import annotations

from typing import Any

from .jev_system_one import JevUnavailable, jev


def _probability(answer: Any, default: float = 0.0) -> float:
    value = answer.get("noul") if isinstance(answer, dict) else answer
    if value is None and isinstance(answer, dict):
        value = answer.get("value")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, value))


def should_store(content: str, context: str = "") -> bool | None:
    try:
        result = jev.evaluate(
            state={"observation": content[:1200], "context": context[:3000]},
            questions={
                "durable": {
                    "type": "noul",
                    "instructions": (
                        "Does this contain a durable user fact, preference, project, relationship, "
                        "goal, or other long-term context worth remembering? Return high probability "
                        "for durable information and low probability for transient commands/status."
                    ),
                }
            },
            timeout=2.5,
        )
        if not result:
            return None
        return _probability(result["answers"].get("durable")) >= 0.60
    except JevUnavailable:
        return None


def score_relevance(contents: list[str], query: str, *, max_results: int = 5) -> list[int] | None:
    """Return 0-based selected memory indexes using one Jev categorical decision.

    The operation remains advisory: callers can keep their deterministic ranking as
    the fallback and only narrow candidates when Jev successfully answers.
    """
    if not contents or not query:
        return None
    try:
        criteria = {
            str(i): {"memory": item[:1200]}
            for i, item in enumerate(contents[:20])
        }
        if len(criteria) < 2:
            return [0]
        result = jev.evaluate(
            state={"query": query[:1200], "candidates": criteria},
            questions={
                "best": {
                    "type": "choice",
                    "criteria": criteria,
                    "instructions": (
                        "Choose the single memory candidate that is most directly useful for answering "
                        "the query. Judge semantic relevance, not keyword overlap."
                    ),
                }
            },
            timeout=2.5,
        )
        if not result:
            return None
        answer = result["answers"].get("best") or {}
        choice = answer.get("choice")
        if choice is None:
            return None
        first = int(choice)
        return [first]
    except (JevUnavailable, ValueError, TypeError, KeyError):
        return None


def should_evict(content: str, current_state: str = "") -> bool | None:
    try:
        result = jev.evaluate(
            state={"memory": content[:1200], "current_state": current_state[:3000]},
            questions={
                "evict": {
                    "type": "noul",
                    "instructions": (
                        "Should this memory be removed because it is stale or superseded by the current "
                        "state? Do not evict merely because it is old."
                    ),
                }
            },
            timeout=2.5,
        )
        if not result:
            return None
        return _probability(result["answers"].get("evict")) >= 0.85
    except JevUnavailable:
        return None
