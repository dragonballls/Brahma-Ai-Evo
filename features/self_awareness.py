"""Functional self-awareness tool surface for Brahma Evo."""

from core.self_model import self_awareness

FEATURE_METADATA = {
    "name": "self_awareness",
    "aliases": ["self_awareness", "identity", "who_am_i", "who_are_you"],
    "description": (
        "Inspect Brahma's functional self-model, the configured user identity, "
        "entity classifications, and runtime action state without claiming literal consciousness."
    ),
    "triggers": [
        "who are you",
        "who am i",
        "what do you know about me",
        "what is your identity",
        "what is my identity",
        "differentiate me from you",
        "what is my phone",
    ],
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "snapshot | classify | resolve | answer"},
            "text": {"type": "STRING", "description": "Reference or user text to classify/resolve"},
            "speaker": {"type": "STRING", "description": "user | assistant (default user)"},
        },
    },
}


def execute(**kwargs):
    action = str(kwargs.get("action", "snapshot") or "snapshot").strip().lower()

    if action == "snapshot":
        return {"success": True, **self_awareness.snapshot()}

    if action == "classify":
        text = str(kwargs.get("text", "") or "").strip()
        speaker = str(kwargs.get("speaker", "user") or "user").strip().lower()
        return {
            "success": bool(text),
            "text": text,
            "entity": self_awareness.classify_reference(text, speaker=speaker),
        }

    if action == "resolve":
        text = str(kwargs.get("text", "") or "").strip()
        return {"success": bool(text), **self_awareness.resolve_turn(text)}

    if action == "answer":
        text = str(kwargs.get("text", "") or "").strip()
        answer = self_awareness.identity_answer(text)
        return {
            "success": bool(answer),
            "answer": answer or "That is not a direct identity question.",
        }

    return {
        "success": False,
        "error": "Choose snapshot, classify, resolve, or answer.",
    }
