from __future__ import annotations

import unittest
from unittest.mock import patch

from llm_client import UnifiedAIClient
class ConversationDeliveryTests(unittest.TestCase):
    def test_omniroute_is_first_for_tool_capable_chat(self):
        client = UnifiedAIClient()
        client._provider = "OpenRouter"
        calls = []

        with patch("llm_client.openrouter_client._call_omniroute_tool_capable", return_value="OmniRoute answer") as omni, \
             patch("llm_client.openrouter_client._call_tool_capable", side_effect=AssertionError("Direct OpenRouter should be fallback only")):
            result = client.chat_with_tools(
                messages=[{"role": "user", "content": "Open an app."}],
                tools=[{
                    "name": "open_app",
                    "description": "Open an application",
                    "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}},
                }],
                tool_executor=lambda name, args: calls.append((name, args)),
                model="auto",
                max_rounds=3,
            )

        self.assertEqual(result, "OmniRoute answer")
        omni.assert_called_once()
        self.assertEqual(calls, [])

    def test_tool_call_uses_direct_openrouter_when_omniroute_unavailable(self):
        client = UnifiedAIClient()
        client._provider = "OpenRouter"
        responses = [{
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Direct fallback answer",
                }
            }]
        }]

        def fake_call(*args, **kwargs):
            return responses.pop(0)

        with patch("llm_client.openrouter_client._call_omniroute_tool_capable", return_value=None), \
             patch("llm_client.openrouter_client._call_tool_capable", side_effect=fake_call):
            result = client.chat_with_tools(
                messages=[{"role": "user", "content": "Hello."}],
                tools=[{
                    "name": "noop",
                    "description": "No-op",
                    "parameters": {"type": "object", "properties": {}},
                }],
                tool_executor=lambda name, args: "ok",
                model="auto",
                max_rounds=2,
            )

        self.assertEqual(result, "Direct fallback answer")

    def test_openrouter_tool_loop_executes_tool_and_returns_final_text(self):
        client = UnifiedAIClient()
        client._provider = "OpenRouter"
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

        def fake_call(*args, **kwargs):
            return responses.pop(0)

        with patch.object(client, "reload_settings"),              patch("llm_client.openrouter_client._call_tool_capable", side_effect=fake_call):
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
                tool_executor=lambda name, args: f"{name}:{args['action']}:ok",
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
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("use_live: bool | None = None", source)
        self.assertIn("live_recently_healthy", source)
        self.assertIn("use_live = live_recently_healthy", source)
        self.assertIn("self.speak(reply, proactive=True, use_live=False)", source)
        self.assertIn("def _speak_native(self, text: str, profile)", source)

    def test_text_fallback_uses_canonical_assistant_delivery(self):
        from pathlib import Path

        main = Path("main.py").read_text(encoding="utf-8")
        ui = Path("ui.py").read_text(encoding="utf-8")
        self.assertIn("def _deliver_assistant_reply(", main)
        self.assertIn('self.ui.record_chat_event({', main)
        self.assertIn('"role": "assistant"', main)
        self.assertIn('args=(text, memory_ctx, source or "local")', main)
        self.assertIn("def acknowledge_chat_response(self):", ui)
    def test_native_tts_playback_has_a_real_failure_fallback(self):
        from pathlib import Path

        source = Path("actions/attention_monitor.py").read_text(encoding="utf-8")
        self.assertIn("$deadline = (Get-Date).AddSeconds(8)", source)
        self.assertIn("Media duration metadata did not load.", source)
        self.assertIn("Media playback did not complete.", source)
        self.assertIn("if return_code != 0:", source)
    def test_silent_live_turn_has_text_fallback_and_reconnect_signal(self):
        source = __import__("pathlib").Path("main.py").read_text(encoding="utf-8")
        self.assertIn("degraded_turn = bool(full_in) and not full_out and not had_usable_audio", source)
        self.assertIn('name="live-silent-turn-fallback"', source)
        self.assertIn("Live turn produced transcription but no usable response audio/text.", source)


if __name__ == "__main__":
    unittest.main()
