from __future__ import annotations

"""Fast JEV decision layer for Brahma's Windows UI hands.

JEV chooses from an explicit action table. Execution stays entirely in Brahma's
code using pywinauto/UI Automation, so model output never becomes shell code,
selectors, or arbitrary coordinates.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests


logger = logging.getLogger("brahma_jev")

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_CONFIDENCE = 0.72
MAX_CONTROLS = 60
MAX_STEPS = 30


@dataclass
class UIAction:
    action_id: str
    kind: str
    label: str
    control: Any


def _openrouter_key() -> str:
    try:
        from config import get_api_key
        return str(get_api_key("OpenRouter") or "").strip()
    except Exception:
        return ""



def _visible_window_state() -> tuple[Any, list[UIAction]]:
    try:
        from pywinauto import Desktop
    except Exception as exc:
        raise RuntimeError("JEV Hands requires pywinauto.") from exc

    desktop = Desktop(backend="uia")
    windows = []
    try:
        windows = [w for w in desktop.windows() if w.is_visible()]
    except Exception:
        windows = []

    actions: list[UIAction] = []
    seen = set()

    for window in windows:
        try:
            title = (window.window_text() or "").strip()
        except Exception:
            title = ""
        try:
            controls = window.descendants()
        except Exception:
            controls = []

        for control in controls:
            if len(actions) >= MAX_CONTROLS:
                break
            try:
                if not control.is_visible() or not control.is_enabled():
                    continue
                name = (control.window_text() or "").strip()
                info = getattr(control, "element_info", None)
                control_type = str(getattr(info, "control_type", "") or "").strip()
                key = (title, control_type, name)
                if key in seen:
                    continue
                seen.add(key)

                allowed = {
                    "Button", "CheckBox", "ComboBox", "Hyperlink", "ListItem",
                    "MenuItem", "RadioButton", "TabItem", "TreeItem",
                }
                if control_type not in allowed or not name:
                    continue

                aid = f"ui_{len(actions) + 1}"
                actions.append(
                    UIAction(
                        action_id=aid,
                        kind="click",
                        label=f"{control_type} '{name}' in window '{title[:80]}'",
                        control=control,
                    )
                )
            except Exception:
                continue

    return (windows[0] if windows else None), actions


def _decide(goal: str, active_title: str, actions: list[UIAction], history: list[str]) -> dict:
    key = _openrouter_key()
    if not key:
        raise RuntimeError("JEV Hands is unavailable because the Brahma OpenRouter key is missing.")

    criteria = {
        a.action_id: a.label
        for a in actions
    }
    criteria["DONE"] = "The requested goal is visibly complete; do not perform another UI action."
    criteria["BLOCKED"] = "None of the offered controls can safely advance the requested goal."

    state = {
        "goal": goal,
        "active_window": active_title,
        "visible_controls": [
            {"id": a.action_id, "label": a.label}
            for a in actions
        ],
        "recent_actions": history[-8:],
    }

    payload = {
        "model": os.environ.get("BRAHMA_JEV_MODEL", DEFAULT_MODEL),
        "state": state,
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": (
                    "Choose exactly one offered UI action that advances the user's goal. "
                    "Use only the provided controls. Never infer hidden controls. "
                    "Do not repeat a completed action. Choose DONE only when the goal is visibly satisfied."
                ),
                "criteria": criteria,
            }
        },
    }

    started = time.perf_counter()
    response = requests.post(
        DECISIONS_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/brahma-ai",
            "X-Title": "Brahma Evo",
        },
        json=payload,
        timeout=12,
    )
    latency_ms = round((time.perf_counter() - started) * 1000)

    if response.status_code != 200:
        raise RuntimeError(f"JEV request failed with HTTP {response.status_code}.")

    data = response.json()
    answer = (data.get("answers") or {}).get("next_action") or {}
    choice = answer.get("choice")
    probabilities = answer.get("probabilities") or {}
    confidence = float(answer.get("confidence") or 0.0)

    if choice not in criteria:
        raise ValueError("JEV returned an action outside the offered action table.")
    if set(probabilities) != set(criteria):
        raise ValueError("JEV returned an incomplete probability distribution.")
    if confidence < DEFAULT_CONFIDENCE and choice not in {"DONE", "BLOCKED"}:
        return {
            "choice": "BLOCKED",
            "confidence": confidence,
            "latency_ms": latency_ms,
            "model": data.get("model", payload["model"]),
            "reason": "JEV confidence below the configured execution threshold.",
        }

    return {
        "choice": choice,
        "confidence": confidence,
        "latency_ms": latency_ms,
        "model": data.get("model", payload["model"]),
        "reason": "",
    }


def _execute(action: UIAction) -> str:
    control = action.control
    try:
        if hasattr(control, "click_input"):
            control.click_input()
            return f"JEV clicked {action.label}."
        control.click()
        return f"JEV clicked {action.label}."
    except Exception as exc:
        raise RuntimeError(f"JEV action execution failed: {exc}") from exc


def try_click(goal: str, player=None) -> str | None:
    """Fast single-click JEV path. Returns None when the structured UI path cannot run."""
    goal = (goal or "").strip()
    if not goal or not _openrouter_key():
        return None
    try:
        window, actions = _visible_window_state()
        if not actions:
            return None
        active_title = (window.window_text() if window else "") or ""
        decision = _decide(goal, active_title.strip(), actions, [])
        choice = decision["choice"]
        if choice in {"DONE", "BLOCKED"}:
            return None
        selected = next((a for a in actions if a.action_id == choice), None)
        if selected is None:
            return None
        result = _execute(selected)
        if player:
            player.write_log(
                f"[JEV Hands] {selected.label} "
                f"({decision.get('latency_ms', 0)} ms, confidence {decision.get('confidence', 0.0):.2f})"
            )
        return result
    except Exception as exc:
        logger.debug("JEV fast click unavailable: %s", exc)
        return None


def run_task(goal: str, player=None, max_steps: int = MAX_STEPS) -> str:
    goal = (goal or "").strip()
    if not goal:
        return "JEV Hands needs a goal."

    max_steps = max(1, min(int(max_steps), MAX_STEPS))
    history: list[str] = []
    started = time.perf_counter()

    for _ in range(max_steps):
        window, actions = _visible_window_state()
        if not actions:
            return "JEV Hands found no visible actionable Windows controls."

        try:
            active_title = (window.window_text() if window else "") or ""
        except Exception:
            active_title = ""

        decision = _decide(goal, active_title.strip(), actions, history)
        choice = decision["choice"]

        if choice == "DONE":
            return (
                f"JEV completed the UI task in {len(history)} action(s) "
                f"({round((time.perf_counter() - started) * 1000)} ms total)."
            )
        if choice == "BLOCKED":
            return (
                f"JEV stopped safely after {len(history)} action(s) "
                f"(confidence {decision.get('confidence', 0.0):.2f})."
            )

        selected = next((a for a in actions if a.action_id == choice), None)
        if selected is None:
            return "JEV selected a control that is no longer visible; nothing was executed."

        result = _execute(selected)
        history.append(selected.label)

        if player:
            player.write_log(
                f"[JEV Hands] {selected.kind}: {selected.label} "
                f"({decision.get('latency_ms', 0)} ms decision, "
                f"{decision.get('confidence', 0.0):.2f} confidence)"
            )

        # Give Windows UIA one refresh cycle, not an arbitrary long sleep.
        time.sleep(0.03)

    return f"JEV reached the {max_steps}-action safety limit without proving completion."
