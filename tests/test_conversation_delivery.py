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

    def test_multi_model_reasoning_is_available_only_as_text_recovery(self):
        from pathlib import Path

        main = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("def _looks_like_action_request(text: str) -> bool:", main)
        self.assertIn("from core.intelligence_orchestrator import orchestrator", main)
        self.assertIn("Multi-model Intelligence", main)
        self.assertIn("not _looks_like_action_request(text)", main)
        self.assertIn('"intelligence_orchestration_enabled"', Path("core/intelligence_orchestrator.py").read_text(encoding="utf-8"))

    def test_action_requests_are_kept_on_tool_capable_path(self):
        from pathlib import Path

        main = Path("main.py").read_text(encoding="utf-8")
        helper = main.split("def _looks_like_action_request", 1)[1].split("def _build_task_plan", 1)[0]
        for phrase in ("open ", "run ", "send ", "delete ", "control ", "fix "):
            self.assertIn(phrase, helper)

    def test_main_cloud_tool_route_uses_unified_omniroute_client(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        block = source.split("def _cloud_tool_reply(", 1)[1].split("def _ig_gemini_reply(", 1)[0]
        self.assertIn("from llm_client import client as unified_cloud_client", block)
        self.assertIn("return unified_cloud_client.chat_with_tools(", block)
        self.assertIn('model="auto"', block)

    def test_cloud_recovery_order_is_primary_secondary_multimodel_then_local(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        primary = source.index("# Run the configured cloud provider first")
        secondary = source.index("# Secondary cloud routing is controlled", primary)
        multimodel = source.index("# Multi-model intelligence is a text-only recovery layer.", secondary)
        local = source.index("# 3. If user explicitly configured Local AI", multimodel)
        self.assertLess(primary, secondary)
        self.assertLess(secondary, multimodel)
        self.assertLess(multimodel, local)

    def test_local_brain_escalates_to_full_tool_registry_only_when_focused_path_is_empty(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("focus_core=True", source)
        self.assertIn("and _looks_like_action_request(text)", source)
        self.assertIn("focus_core=False", source)
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

        with patch.object(client, "reload_settings"),              patch("llm_client.openrouter_client._call_omniroute_tool_capable", return_value=None),              patch("llm_client.openrouter_client._call_tool_capable", side_effect=fake_call):
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

    def test_gemini_configured_provider_uses_omniroute_cloud_router(self):
        client = UnifiedAIClient()
        client._provider = "Gemini"
        with patch("llm_client.openrouter_client.chat", return_value="OmniRoute answer") as routed:
            result = client.chat("hello")
        self.assertEqual(result, "OmniRoute answer")
        routed.assert_called_once()

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

    def test_legacy_log_error_bridge_requires_pending_command_state(self):
        from pathlib import Path

        source = Path("ui.py").read_text(encoding="utf-8")
        self.assertIn('low.startswith("err:")', source)
        self.assertIn("and self._chat_source_queue", source)
        self.assertIn("Only command-scoped errors belong in the conversation stream.", source)
    def test_legacy_log_chat_bridge_requires_pending_command_state(self):
        from pathlib import Path

        source = Path("ui.py").read_text(encoding="utf-8")
        self.assertIn('and self._chat_source_queue', source)
        self.assertIn("Proactive Brahma messages", source)

    def test_text_fallback_uses_canonical_assistant_delivery(self):
        from pathlib import Path

        main = Path("main.py").read_text(encoding="utf-8")
        ui = Path("ui.py").read_text(encoding="utf-8")
        self.assertIn("def _deliver_assistant_reply(", main)
        self.assertIn('self.ui.record_chat_event({', main)
        self.assertIn('"role": "assistant"', main)
        self.assertIn('args=(text, memory_ctx, source or "local")', main)
        self.assertIn("def acknowledge_chat_response(self):", ui)
    def test_failed_text_request_has_canonical_chat_and_voice_response(self):
        from pathlib import Path

        main = Path("main.py").read_text(encoding="utf-8")
        block = main.split("def _fallback_reply", 1)[1]
        self.assertIn("failure_reply = (", block)
        self.assertIn('self.ui.record_chat_event({', block)
        self.assertIn('"role": "assistant"', block)
        self.assertIn('self.speak(failure_reply, proactive=True, use_live=False)', block)
        self.assertIn('self.ui.finish_task_workspace(failure_reply, "Reply failed.", 0)', block)

    def test_multi_model_recovery_does_not_cross_from_gemini_when_auto_switch_is_off(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("(is_cloud_openrouter or auto_provider_switch)", source)
    def test_all_tool_capable_paths_use_runtime_dynamic_tool_surface(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        gemini_block = source.split("def _gemini_tool_reply(", 1)[1].split("def _cloud_tool_reply(", 1)[0]
        cloud_block = source.split("def _cloud_tool_reply(", 1)[1].split("def _ig_gemini_reply(", 1)[0]
        self.assertIn("tools=_runtime_tool_declarations()", gemini_block)
        self.assertIn("tools=_runtime_tool_declarations()", cloud_block)

        fallback_block = source.split("def _fallback_reply(", 1)[1]
        self.assertGreaterEqual(fallback_block.count("tools=_runtime_tool_declarations()"), 4)

    def test_cloud_action_router_does_not_override_provider_switch_setting(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        block = source.split("def _cloud_tool_reply(", 1)[1].split("def _ig_gemini_reply(", 1)[0]
        self.assertIn("Route all online tool-capable requests through the local OmniRoute gateway.", block)
        self.assertNotIn("alternate = ", block)
        self.assertNotIn("for candidate in ordered:", block)
    def test_native_tts_playback_has_a_real_failure_fallback(self):
        from pathlib import Path

        source = Path("actions/attention_monitor.py").read_text(encoding="utf-8")
        self.assertIn("$deadline = (Get-Date).AddSeconds(8)", source)
        self.assertIn("Media duration metadata did not load.", source)
        self.assertIn("Media playback did not complete.", source)
        self.assertIn("if return_code != 0:", source)
    def test_live_voice_turns_use_canonical_chat_event_delivery(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        block = source.split("if sc.turn_complete:", 1)[1].split("if full_in and len(full_in) > 5:", 1)[0]
        self.assertIn('self.ui.record_chat_event({', block)
        self.assertIn('"role": "assistant"', block)
        self.assertIn('"source": "mic"', block)
        self.assertIn('args=(fallback_text, _memory_context_for_request(fallback_text), "mic")', block)

    def test_silent_live_turn_has_text_fallback_and_reconnect_signal(self):
        source = __import__("pathlib").Path("main.py").read_text(encoding="utf-8")
        self.assertIn("degraded_turn = bool(full_in) and not full_out and not had_usable_audio", source)
        self.assertIn('name="live-silent-turn-fallback"', source)
        self.assertIn("Live turn produced transcription but no usable response audio/text.", source)


    def test_local_or_offline_voice_skips_gemini_live_startup(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("voice_fallback_mode =", source)
        self.assertIn("if voice_fallback_mode:", source)
        block = source.split("if voice_fallback_mode:", 1)[1].split("client = genai.Client(", 1)[0]
        self.assertIn("await self._run_text_voice_fallback_loop(reason=reason)", block)

    def test_phone_audio_has_one_relay_consumer(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("asyncio.create_task(self._relay_phone_audio())"), 1)
        self.assertEqual(source.count("tg.create_task(self._relay_phone_audio())"), 0)

    def test_live_text_without_audio_uses_native_tts_and_reconnects(self):
        from pathlib import Path

        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("text_only_live_turn = bool(full_out) and not had_usable_audio", source)
        self.assertIn("self.speak(full_out, proactive=True, use_live=False)", source)
        self.assertIn("Live turn produced response text but no usable response audio.", source)

if __name__ == "__main__":
    unittest.main()
