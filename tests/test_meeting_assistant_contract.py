from __future__ import annotations

from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
import importlib
import sys
import threading
from unittest.mock import patch

from core import provider_policy


@contextmanager
def _meeting_module():
    """Load the meeting module without requiring optional native audio/video packages."""
    original = sys.modules.pop("actions.meeting_assistant", None)
    fake_mss = ModuleType("mss")
    fake_mss.__path__ = []
    fake_mss_tools = ModuleType("mss.tools")
    fake_mss.tools = fake_mss_tools
    fake_sd = ModuleType("sounddevice")
    fake_google = ModuleType("google")
    fake_google.__path__ = []
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("unexpected Gemini client")
    )
    fake_genai_types = ModuleType("google.genai.types")
    fake_genai.types = fake_genai_types
    fake_google.genai = fake_genai
    try:
        with patch.dict(
            sys.modules,
            {
                "mss": fake_mss,
                "mss.tools": fake_mss_tools,
                "sounddevice": fake_sd,
                "google": fake_google,
                "google.genai": fake_genai,
                "google.genai.types": fake_genai_types,
            },
        ):
            yield importlib.import_module("actions.meeting_assistant")
    finally:
        sys.modules.pop("actions.meeting_assistant", None)
        if original is not None:
            sys.modules["actions.meeting_assistant"] = original


def test_meeting_empty_gemini_analysis_is_not_reported_as_live(monkeypatch):
    with _meeting_module() as meeting_assistant:
        assistant = object.__new__(meeting_assistant.MeetingAssistant)
        updates = []
        assistant._on_update = updates.append
        assistant._on_state = None
        assistant._interval = 4.0
        assistant._running = True
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
            meeting_assistant,
            "_capture_screen",
            lambda: b"screen",
        )
        fake_models = SimpleNamespace(generate_content=lambda **kwargs: object())
        monkeypatch.setattr(
            meeting_assistant.genai,
            "Client",
            lambda **kwargs: SimpleNamespace(models=fake_models),
        )
        monkeypatch.setattr(
            meeting_assistant.time,
            "sleep",
            lambda _seconds: assistant._stop_event.set(),
        )

        assistant._loop()

        assert updates
        assert updates[0]["status"] == "error"
        assert updates[0]["active"] is True
        assert "empty" in updates[0]["answer"].casefold()


def test_meeting_audio_loop_normalizes_and_emits_transcription(monkeypatch):
    with _meeting_module() as meeting_assistant:
        assistant = object.__new__(meeting_assistant.MeetingAssistant)
        updates = []
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
                kwargs["callback"](
                    SimpleNamespace(tobytes=lambda: b"\x00" * 80000),
                    40000,
                    None,
                    None,
                )

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(meeting_assistant.sd, "InputStream", FakeInputStream)
        monkeypatch.setattr(
            meeting_assistant.time,
            "sleep",
            lambda _seconds: assistant._audio_stop.set(),
        )
        monkeypatch.setattr(meeting_assistant.time, "time", lambda: 100.0)

        assistant._audio_loop()

        assert updates
        assert updates[0]["status"] == "speech"
        assert updates[0]["speech"] == "hello meeting world"
        assert assistant._last_speech == "hello meeting world"
        assert assistant._speech_gen == 1
