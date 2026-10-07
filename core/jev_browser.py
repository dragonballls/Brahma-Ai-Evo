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
    # The current Jev Ultrafast API does not expose a Brahma-controlled
    # request interception/redirect policy. Keep the integration discoverable
    # but fail closed rather than running an unguarded browser session.
    return False


def run_goal(url: str, goal: str, *, max_steps: int = 60) -> dict[str, Any]:
    if not available():
        raise JevBrowserUnavailable(
            "Jev browser layer is disabled because its current API cannot enforce Brahma's "
            "request-level network and redirect policy."
        )

    from actions.playwright_mcp_client import validate_browser_url
    validated_url = validate_browser_url(url)

    try:
        from jev_ultrafast import Agent
    except Exception as exc:
        raise JevBrowserUnavailable("jev-ultrafast is not installed") from exc

    if not goal:
        raise ValueError("goal is required")

    states: list[dict[str, Any]] = []
    with Agent(validated_url, goal) as agent:
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
