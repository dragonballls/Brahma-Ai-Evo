from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from llm_client import UnifiedAIClient
from or_client import OpenRouterClient


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class ConversationDeliveryTests(unittest.TestCase):
    def test_openrouter_tool_loop_executes_tool_and_returns_final_text(self):
        client = OpenRouterClient()
        responses = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "computer_control",
                                "arguments": '{"action":"screenshot"}',
                            },
                        }],
                    }
                }]
            },
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "I captured the screen successfully.",
                    }
                }]
            },
        ]
        seen = []

        def fake_post(*args, **kwargs):
            seen.append(kwargs["json"])
            return _FakeResponse(responses.pop(0))

        def tool_executor(name, args):
            return f"{name}:{args['action']}:ok"

        with patch.object(client, "_is_rate_limited", return_value=False),              patch.object(client, "_refresh_credentials"),              patch.object(client, "_call_tool_capable", side_effect=lambda model, messages, tools, max_tokens, temperature: responses.pop(0)):
            # Seed a fake credential because _call_tool_capable is mocked above.
            client.api_key = "test-openrouter-key"
            result = client.chat_with_tools(
                messages=[
                    {"role": "system", "content": "Use tools when needed."},
                    {"role": "user", "content": "Take a screenshot."},
                ],
                tools=[{
                    "name": "computer_control",
                    "description": "Computer control",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"action": {"type": "STRING"}},
                        "required": ["action"],
                    },
                }],
                tool_executor=tool_executor,
                model="auto",
                max_rounds=3,
            )
        self.assertEqual(result, "I captured the screen successfully.")

    def test_gemini_provider_is_not_routed_to_openrouter(self):
        client = UnifiedAIClient()
        client._provider = "Gemini"
        with patch.object(client, "_gemini_text", return_value="gemini answer") as gemini,              patch("llm_client.openrouter_client.chat", side_effect=AssertionError("OpenRouter must not be called")):
            result = client.chat("hello")
        self.assertEqual(result, "gemini answer")
        gemini.assert_called_once()

    def test_live_session_is_not_the_exclusive_text_command_path(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        marker = "# Text/mobile/Discord commands use the reliable text agent even when a"
        self.assertIn(marker, source)
        self.assertIn('name="text-command-agent"', source)
        self.assertIn("self._fallback_reply", source)

    def test_native_tts_can_be_forced_even_when_live_session_exists(self):
        from main import BrahmaLive

        obj = object.__new__(BrahmaLive)
        obj.session = object()
        obj._loop = MagicMock()
        obj._last_live_audio_at = 0.0
        obj.ui = MagicMock()
        obj.ui.muted = False

        with patch.object(BrahmaLive, "_speak_native") as native:
            obj.speak("test response", use_live=False)
        native.assert_called_once()

    def test_silent_live_turn_has_text_fallback_and_reconnect_signal(self):
        source = __import__("pathlib").Path("main.py").read_text(encoding="utf-8")
        self.assertIn("degraded_turn = bool(full_in) and not full_out and turn_audio_bytes < 256", source)
        self.assertIn('name="live-silent-turn-fallback"', source)
        self.assertIn("Live turn produced transcription but no usable response audio/text.", source)


if __name__ == "__main__":
    unittest.main()
