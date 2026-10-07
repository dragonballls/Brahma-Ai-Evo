import pytest
"""Regression tests for the Live-style duplex voice contract."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from memory import config_manager
from core.duplex_voice import BargeInGate, PlaybackGeneration


ROOT = Path(__file__).resolve().parents[1]


class LiveVoiceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_text = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.echo_text = (ROOT / "core" / "echo.py").read_text(encoding="utf-8")
        cls.language_policy_text = (ROOT / "core" / "language_policy.py").read_text(encoding="utf-8")

    def test_input_and_output_chunk_is_within_live_latency_range(self):
        tree = ast.parse(self.main_text)
        found = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "CHUNK_SIZE":
                        found = ast.literal_eval(node.value)
        self.assertEqual(found, 640)
        # 640 samples = 40 ms at 16 kHz input, and 26.7 ms at 24 kHz output.

    def test_live_affective_dialog_legacy_field_is_not_sent(self):
        # Gemini Live removed enable_affective_dialog; sending the legacy field
        # can reject the Live session during config validation.
        self.assertNotIn("enable_affective_dialog=True", self.main_text)
        self.assertNotIn("enable_affective_dialog=", self.main_text)

    def test_live_model_ladder_has_current_primary_and_fallbacks(self):
        self.assertIn('LIVE_MODEL = os.environ.get("BRAHMA_LIVE_MODEL", "gemini-3.8-live")', self.main_text)
        self.assertIn('gemini-3.1-flash-live-preview', self.main_text)
        self.assertIn('gemini-2.5-flash-native-audio-preview-12-2025', self.main_text)
        self.assertIn("LIVE_MODEL_CANDIDATES", self.main_text)
        self.assertIn("_live_model_index", self.main_text)

    def test_live_microphone_input_uses_explicit_pcm_blob(self):
        self.assertIn("types.Blob(", self.main_text)
        self.assertIn('mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}"', self.main_text)
        self.assertIn("send_realtime_input(media=msg)", self.main_text)

    def test_live_tool_calls_are_blocking_for_brahma_executor(self):
        self.assertIn('item["behavior"] = "BLOCKING"', self.main_text)
    def test_silent_audio_recovery_guard_exists(self):
        self.assertIn("tiny_audio_chunks", self.main_text)
        self.assertIn("turn_audio_bytes < 256", self.main_text)
        self.assertIn("Rotating to the next verified voice model.", self.main_text)
        self.assertIn("chunk_size > 8", self.main_text)

    def test_audio_device_failure_falls_back_to_system_device(self):
        self.assertIn("falling back to the Windows system microphone", self.main_text)
        self.assertIn("falling back to the Windows system speaker", self.main_text)
        self.assertIn('device=None', self.main_text)
        self.assertIn("Microphone stream failed", self.main_text)
        self.assertIn("Speaker stream failed", self.main_text)

    def test_language_lock_is_present_in_voice_and_system_prompt(self):
        self.assertIn("language_policy import prompt_block", self.main_text)
        self.assertIn("language_prompt_block()", self.main_text)
        self.assertIn("Change response language only when the user explicitly requests", self.language_policy_text)

    def test_server_vad_and_start_of_activity_interrupts_are_configured(self):
        self.assertIn("realtime_input_config={", self.main_text)
        self.assertIn('"automatic_activity_detection"', self.main_text)
        self.assertIn('"disabled": False', self.main_text)
        self.assertIn('"prefix_padding_ms": 100', self.main_text)
        self.assertIn('"silence_duration_ms": 650', self.main_text)
        self.assertIn('"activity_handling": "START_OF_ACTIVITY_INTERRUPTS"', self.main_text)

    def test_server_interrupted_event_clears_client_playback(self):
        self.assertIn('getattr(sc, "interrupted", False)', self.main_text)
        self.assertIn("self.trigger_barge_in()", self.main_text)
        self.assertIn("self._playback_generation.bump()", self.main_text)
        self.assertIn("generation != self._playback_generation.current()", self.main_text)

    def test_fast_barge_in_path_is_present(self):
        self.assertIn("fast=True", self.main_text)
        self.assertIn("minimum_level=40.0", self.main_text)
        self.assertIn("required_blocks=2", self.main_text)
        self.assertIn("fast: bool = False", self.echo_text)
        self.assertIn("if warming and fast", self.echo_text)

    def test_voice_style_directives_cover_natural_delivery(self):
        expected = (
            "VOICE INTERACTION MODE:",
            "continuous hands-free, full-duplex",
            "natural turn-taking",
            "natural pauses, pacing",
            "calm, intelligent, polished, articulate, confident",
            "mm-hmm",
            "uh-huh",
            "yield immediately",
        )
        for phrase in expected:
            self.assertIn(phrase, self.main_text)

    def test_non_gemini_voice_has_independent_text_tts_fallback(self):
        self.assertIn("async def run_text_voice_fallback(self):", self.main_text)
        self.assertIn("self._text_voice_fallback = True", self.main_text)
        self.assertIn("asyncio.run(brahma_evo.run_text_voice_fallback())", self.main_text)
        self.assertIn('text_voice_fallback = bool(getattr(self, "_text_voice_fallback", False))', self.main_text)

        self.assertIn("Text/TTS voice fallback is active;", self.main_text)
        self.assertIn("speech is transcribed through the text command path.", self.main_text)
    def test_all_non_live_reasoning_paths_include_language_policy(self):
        self.assertIn("from core.language_policy import prompt_block as language_prompt_block", self.main_text)
        self.assertIn("language_directive = language_prompt_block()", self.main_text)
        self.assertIn("f\"{language_directive}\\n\"", self.main_text)
        self.assertIn("safety_language = language_prompt_block()", self.main_text)
    def test_offline_voice_never_calls_network_speech_recognition(self):
        self.assertIn("recognize_sphinx(audio_data)", self.main_text)
        self.assertIn('config_manager.get_boolean_setting("offline_mode_enabled", False)', self.main_text)
        self.assertIn('config_manager.get_boolean_setting(\n            "allow_cloud_transcription", False\n        )', self.main_text)
        self.assertNotIn('bool(app_cfg.get("offline_mode_enabled", False))', self.main_text)
        self.assertIn("recognize_google(audio_data)", self.main_text)
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(requirements, r"(?im)^pocketsphinx(?:>=5\.0\.0,<6|==5\.0\.4)$")
    def test_hands_free_is_the_default_mode(self):
        self.assertFalse(config_manager.get_setting("push_to_talk_enabled", False))

    def test_emotion_linked_prosody_is_wired_into_live_and_fallback_speech(self):
        self.assertIn("from core.prosody import profile_for_text, profile_prompt_block", self.main_text)
        self.assertIn("profile.prompt_directive()", self.main_text)
        self.assertIn("rate=profile.edge_rate", self.main_text)
        self.assertIn("pitch=profile.edge_pitch", self.main_text)
        self.assertIn("sapi_rate=profile.sapi_rate", self.main_text)
        self.assertIn("Vary cadence naturally within the utterance", self.main_text)

    def test_gate_and_generation_primitives_are_functional(self):
        gate = BargeInGate(required_blocks=2, minimum_level=10)
        self.assertFalse(gate.observe(is_user_speech=True, level=20))
        self.assertTrue(gate.observe(is_user_speech=True, level=20))
        generation = PlaybackGeneration()
        first = generation.current()
        self.assertEqual(generation.bump(), first + 1)


if __name__ == "__main__":
    unittest.main()


def test_voice_startup_does_not_replace_corrupt_settings_with_network_defaults():
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    start = main_text.index("voice_settings = config_manager.load_settings()")
    block = main_text[start:main_text.index("client = genai.Client(", start)]
    assert "voice configuration could not be loaded safely" in block
    assert "voice startup halted" in block
    assert "voice_settings = {}" not in block


def test_microphone_transcription_does_not_use_cloud_without_explicit_permission(monkeypatch):
    import sys
    import types
    import main

    calls = {"sphinx": 0, "google": 0}

    class AudioData:
        pass

    class Recognizer:
        def __init__(self):
            pass

        def recognize_sphinx(self, _audio):
            calls["sphinx"] += 1
            raise RuntimeError("local recognizer unavailable")

        def recognize_google(self, _audio):
            calls["google"] += 1
            return "cloud result"

    monkeypatch.setitem(
        sys.modules,
        "speech_recognition",
        types.SimpleNamespace(Recognizer=Recognizer, AudioData=lambda *args: AudioData()),
    )

    with pytest.raises(RuntimeError, match="cloud transcription is disabled"):
        main._transcribe_microphone_pcm(
            b"pcm",
            16000,
            offline_mode=False,
            allow_cloud_transcription=False,
        )
    assert calls == {"sphinx": 1, "google": 0}


def test_microphone_transcription_uses_cloud_only_when_explicitly_enabled(monkeypatch):
    import sys
    import types
    import main

    calls = {"sphinx": 0, "google": 0}

    class Recognizer:
        def recognize_sphinx(self, _audio):
            calls["sphinx"] += 1
            raise RuntimeError("local recognizer unavailable")

        def recognize_google(self, _audio):
            calls["google"] += 1
            return "cloud result"

    monkeypatch.setitem(
        sys.modules,
        "speech_recognition",
        types.SimpleNamespace(
            Recognizer=Recognizer,
            AudioData=lambda *args: object(),
        ),
    )

    result = main._transcribe_microphone_pcm(
        b"pcm",
        16000,
        offline_mode=False,
        allow_cloud_transcription=True,
    )
    assert result == "cloud result"
    assert calls == {"sphinx": 1, "google": 1}
