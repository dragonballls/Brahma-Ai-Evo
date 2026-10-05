"""Optional Jev Ultrafast browser-agent adapter.

Jev Ultrafast is used for goal-driven browser work when installed and configured.
Brahma's existing browser-control stack remains the fallback.
"""
from __future__ import annotations

import os
from typing import Any


class JevBrowserUnavailable(RuntimeError):
    pass


def available() -> bool:
    return (
        os.getenv("BRAHMA_JEV_BROWSER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        and bool(os.getenv("TYPESAFE_API_KEY", "").strip())
    )


def run_goal(url: str, goal: str, *, max_steps: int = 60) -> dict[str, Any]:
    if not available():
        raise JevBrowserUnavailable("Jev browser layer is disabled or has no TYPESAFE_API_KEY")

    try:
        from jev_ultrafast import Agent
    except Exception as exc:
        raise JevBrowserUnavailable("jev-ultrafast is not installed") from exc

    if not url or not goal:
        raise ValueError("url and goal are required")

    states: list[dict[str, Any]] = []
    with Agent(url, goal) as agent:
        for state in agent.run():
            states.append({
                "status": state.get("status"),
                "elapsed_ms": state.get("elapsed_ms"),
                "decision": state.get("decision"),
            })
            if len(states) >= max(1, int(max_steps)):
                break

    final = states[-1] if states else {}
    return {
        "ok": final.get("status") in {"done", "completed"},
        "status": final.get("status"),
        "elapsed_ms": final.get("elapsed_ms"),
        "states": states[-10:],
    }
