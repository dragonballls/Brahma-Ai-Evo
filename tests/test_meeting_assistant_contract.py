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



def test_meeting_audio_loop_normalizes_and_emits_transcription(monkeypatch):
    import actions.meeting_assistant as meeting_assistant
    import threading

    updates = []
    assistant = object.__new__(MeetingAssistant)
    assistant._on_update = updates.append
    assistant._on_state = None
    assistant._audio_stop = threading.Event()
    assistant._audio_lock = threading.Lock()
    assistant._audio_buf = bytearray()
    assistant._last_audio_hash = ""
    assistant._last_speech = ""
    assistant._speech_gen = 0
    assistant._title = "Meeting mode"
    assistant._audio_source = {
        "device": 0,
        "channels": 1,
        "samplerate": 8000,
        "loopback": False,
        "label": "Test input",
    }

    monkeypatch.setattr(
        provider_policy,
        "require_provider",
        lambda provider, capability: provider_policy.GEMINI,
    )
    monkeypatch.setattr(meeting_assistant, "_get_api_key", lambda: "test-key")
    monkeypatch.setattr(
        meeting_assistant.genai,
        "Client",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        assistant,
        "_transcribe_audio",
        lambda client, wav_bytes: "  hello   meeting  world  ",
    )

    class FakeInputStream:
        def __init__(self, **kwargs):
            kwargs["callback"](SimpleNamespace(tobytes=lambda: b"\\x00" * 80000), 40000, None, None)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(meeting_assistant.sd, "InputStream", FakeInputStream)
    monkeypatch.setattr(meeting_assistant.time, "sleep", lambda _seconds: assistant._audio_stop.set())
    monkeypatch.setattr(meeting_assistant.time, "time", lambda: 100.0)

    assistant._audio_loop()

    assert updates
    assert updates[0]["status"] == "speech"
    assert updates[0]["speech"] == "hello meeting world"
    assert assistant._last_speech == "hello meeting world"
    assert assistant._speech_gen == 1
