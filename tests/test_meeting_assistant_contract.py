from __future__ import annotations

from types import SimpleNamespace

from core import provider_policy
from actions.meeting_assistant import MeetingAssistant


def test_meeting_empty_gemini_analysis_is_not_reported_as_live(monkeypatch):
    updates = []
    assistant = object.__new__(MeetingAssistant)
    assistant._on_update = updates.append
    assistant._on_state = None
    assistant._interval = 4.0
    assistant._running = True
    import threading
    assistant._stop_event = threading.Event()
    assistant._audio_stop = threading.Event()
    assistant._last_hash = ""
    assistant._last_speech = ""
    assistant._last_answer = ""
    assistant._title = "Meeting mode"
    assistant._context = ""
    monkeypatch.setattr(
        provider_policy,
        "require_provider",
        lambda provider, capability: provider_policy.GEMINI,
    )
    monkeypatch.setattr(
        "actions.meeting_assistant._capture_screen",
        lambda: b"screen",
    )
    fake_models = SimpleNamespace(
        generate_content=lambda **kwargs: object()
    )
    monkeypatch.setattr(
        "actions.meeting_assistant.genai.Client",
        lambda **kwargs: SimpleNamespace(models=fake_models),
    )
    monkeypatch.setattr(
        "actions.meeting_assistant.time.sleep",
        lambda _seconds: assistant._stop_event.set(),
    )

    assistant._loop()

    assert updates
    assert updates[0]["status"] == "error"
    assert updates[0]["active"] is True
    assert "empty" in updates[0]["answer"].casefold()
