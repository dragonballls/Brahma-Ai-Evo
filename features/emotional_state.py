"""Dynamic feature wrapper for Brahma Evo emotional state."""

from core.emotional_controller import emotional_controller

FEATURE_METADATA = {
    "name": "emotional_state",
    "aliases": ["emotional_state", "emotion", "mood", "tone"],
    "description": "Inspect or evaluate the current conversational emotion/tone selected by Brahma.",
    "triggers": [
        "what mood are you in",
        "what emotion are you using",
        "what tone are you using",
        "get emotional state",
    ],
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "current | assess"},
            "text": {"type": "STRING", "description": "Text to evaluate for contextual tone"},
            "task_failed": {"type": "BOOLEAN", "description": "Whether the active task failed"},
            "user_requested_toughness": {"type": "BOOLEAN", "description": "Whether the user requested blunt/tough-love feedback"},
        },
    },
}


def execute(**kwargs):
    action = str(kwargs.get("action", "current") or "current").strip().lower()
    if action == "current":
        state = emotional_controller.current()
        return {
            "success": True,
            "state": state.name,
            "intensity": state.intensity,
            "reason": state.reason,
        }
    if action == "assess":
        return emotional_controller.evaluate(
            str(kwargs.get("text", "") or ""),
            task_failed=bool(kwargs.get("task_failed", False)),
            user_requested_toughness=bool(kwargs.get("user_requested_toughness", False)),
        )
    return {"success": False, "error": "Choose current or assess."}
