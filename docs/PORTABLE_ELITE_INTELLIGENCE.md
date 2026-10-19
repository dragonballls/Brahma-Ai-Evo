# Portable Elite Intelligence Layer

Status: isolated development branch. This code is not wired into Brahma's current runtime and is not installed in AbacusAI-Omni.

The package is intended to be movable into another desktop app later. Its core uses the repository's existing Python, requests, and websocket-client dependencies. The host supplies API keys, settings, memory, tools, microphone, and speakers.

## Targets

- Use direct provider APIs, OpenAI-compatible endpoints, or a local gateway such as OmniRoute without coupling the engine to one app's settings or UI.
- Prefer native APIs when full model capabilities require them. GPT-6 Astra and GPT-6.1 Sol function calling must be routed through the OpenAI Responses API, not assumed to work through every Chat Completions gateway. Keep restrictions model-specific: Astra and 6.1 Sol reject `none`/`minimal` reasoning and custom sampling values; GPT-6 Sol and GPT-6 Luna support `none`, while their Chat Completions function-calling mode requires `reasoning_effort="none"`. Recheck the official model guide when new model IDs are added.
- Keep model inference on configured hosted providers where chosen, reducing local model RAM and compute requirements.
- Make latency and spending tradeoffs explicit with bounded parallel calls, model-call limits, operator-supplied token rates, and a hard estimated-spend ceiling.
- Support independent provider diversity, skeptical critic passes, and final synthesis for explicitly complex text tasks. Tool-bearing requests skip ensemble fan-out to avoid duplicated side effects.
- Require host authorization for write/external/dangerous tools. By default, only read-only tools with no approval requirement may execute.
- Preserve call traces and return failures rather than fabricate success.
- Include a bounded generation-aware audio queue, a separate OpenAI Realtime adapter, and a GPT-Live session adapter using client delegation. The host handles microphone capture, resampling, output playback, and tool approvals.
- Evaluate behavior with repeatable tasks rather than calling it Astra-level based on model count.

## Provider interfaces

OpenAIResponsesProvider: native OpenAI Responses API with reasoning effort and function tools.

OpenAICompatibleProvider: OpenAI-compatible Chat Completions route, for OmniRoute or other compatible gateways. Feature parity depends on gateway and upstream provider.

AnthropicMessagesProvider: native Anthropic Messages API with tool conversion.

GeminiContentProvider: native Gemini generateContent API for text and tools. It is not the Gemini Live streaming-audio adapter.

CallableProvider lets a host wrap an existing LLM client with a small callback accepting (ModelRequest, model_name). Custom providers can instead implement ProviderAdapter and register a Route. Both approaches let a later Abacus adapter wrap its native LLM choices, direct API options, OmniRoute, or another compatible model service without hard-coding those choices into this package. Never store credentials in route objects or repository files.

## Voice

OpenAIRealtimeVoiceAdapter targets the distinct OpenAI Realtime WebSocket protocol, currently defaulting to gpt-realtime-2.1.

GPTLiveVoiceAdapter targets the GPT-Live WebSocket session contract at /v1/live/sessions and defaults to gpt-live-1 with client delegation. It waits for session.started, sends/receives PCM16 audio and transcript events, surfaces client delegation IDs to the host, returns verified results with session.commentary.append or session.thinking.append, and waits for session.closed during clean shutdown. The host owns the backend work, transcript history, API/provider routing, task cancellation, permissions, approvals, microphone, and speaker playback. This lets a later Abacus port use OmniRoute or any other registered LLM backend while GPT-Live handles speech.

Brahma currently has its existing Gemini Live loop. Both new adapters are optional and isolated; this code does not change the runtime's active voice provider. Live audio requires correctly paced PCM16 and a real device-to-session test.

Provider availability and model/API fields can change. An API key is necessary but not sufficient: a real microphone-to-model-to-speaker test on the user's account and region is required.

## Quality and safety gates

- Unit tests cover adapter payloads, tool parsing, retry/fallback, cost budget checks, approval gating, bounded audio, and benchmark reporting without paid provider calls.
- A real-provider smoke suite must be opt-in and use valid keys. It should measure latency, completion rate, interruptions, missing audio, tool accuracy, token use, estimated versus actual cost, and unsupported success claims.
- Compare against GPT-6 Astra and GPT-Live on the same tasks, context, permissions, and tool access. Deterministic phrase matching is only a harness sanity check, not an intelligence benchmark.
- Do not wire this into the default runtime until CI passes, live-provider checks are recorded, and performance/cost regressions are reviewed.
