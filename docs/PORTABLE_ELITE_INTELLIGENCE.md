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
- Include a bounded generation-aware audio queue, a separate OpenAI Realtime adapter, and a GPT-Live session adapter using client delegation. The host handles microphone capture, resampling, output playback, and tool approvals. For both voice adapters, `receive_event()` queues audio deltas in the adapter's bounded playback buffer and returns an event with a byte count/queued flag; the host should play `playback.pop()` data, not play the same event payload a second time. GPT-Live accepts results only for delegation IDs actually observed in the active session. Check `session_closed_confirmed` and `finalization_error` during shutdown: a closed socket without `session.closed` means usage is not confirmed.
- Evaluate behavior with repeatable tasks rather than calling it Astra-level based on model count.


## Multi-key provider pools

CredentialSpec and CredentialPoolProvider let a host manage many provider keys. Real key values must stay in environment variables or an approved secret manager. The checked-in `benchmarks/credential_pool.example.json` contains only environment variable names and public endpoint metadata. It models 10 Gemini keys and 4 OpenRouter keys, plus duplicate source aliases to demonstrate deduplication.

Example host bootstrap:

```python
import json
from pathlib import Path
from core.portable_intelligence.key_pool import CredentialPoolState, build_credential_pools, load_credentials_from_environment

config = json.loads(Path('benchmarks/credential_pool.example.json').read_text('utf-8'))
loaded = load_credentials_from_environment(config['credentials'])
shared_state = CredentialPoolState()
providers = build_credential_pools(loaded.credentials, state=shared_state)
for provider_id, pool in providers.items():
    print(provider_id, pool.snapshot()['credential_count'], 'deduplicated credentials')
print('Missing optional environment variables:', len(loaded.skipped))
```

Operational behavior:

- Identical key + provider family + normalized endpoint tuples collapse to one credential. Duplicate labels are counted but never treated as extra capacity.
- Keys sharing a quota_group share cooldown and in-flight limits. Rate-limit responses honor Retry-After where available; hard quota/billing errors block that scope until the host operator calls shared_state.reset_scope(scope) after the underlying provider issue is resolved.
- Invalid/unauthorized keys are disabled until corrected and explicitly re-enabled with reset_credential(credential_id). Transient errors cool down the affected credential and may fail over.
- snapshot() returns labels, fingerprints, token counters, cooldowns, failures, and shared-scope state—never raw API keys. Token totals and estimated route costs are not provider billing records.
- Merge credential records from all sources the host is permitted to use before build_credential_pools() so duplicate keys can be found in one pass. Reuse one CredentialPoolState anywhere quota scopes must be shared.
- An opaque OmniRoute client key only authenticates to the local gateway; it does not reveal upstream provider keys. If OmniRoute owns upstream rotation, register the gateway as one route. For a direct route and gateway alias serving the same underlying model/quota, assign the same Route.diversity_group so they are not counted as independent ensemble evidence.
- More keys do not automatically increase quota. Gemini quotas are project-scoped, and OpenRouter documents that extra keys do not increase its account-level capacity. Rotation is for safe recovery, failover, diagnostics, and genuinely independent provider projects/accounts where permitted—not quota bypass.
## Provider interfaces

OpenAIResponsesProvider: native OpenAI Responses API with reasoning effort and function tools.

OpenAICompatibleProvider: OpenAI-compatible Chat Completions route, for OmniRoute or other compatible gateways. Feature parity depends on gateway and upstream provider.

AnthropicMessagesProvider: native Anthropic Messages API with tool conversion.

GeminiContentProvider: native Gemini generateContent API for text and tools. It is not the Gemini Live streaming-audio adapter.

CallableProvider lets a host wrap an existing LLM client with a small callback accepting (ModelRequest, model_name). Custom providers can instead implement ProviderAdapter and register a Route. Both approaches let a later Abacus adapter wrap its native LLM choices, direct API options, OmniRoute, or another compatible model service without hard-coding those choices into this package. Never store credentials in route objects or repository files.

## Voice

OpenAIRealtimeVoiceAdapter targets the distinct OpenAI Realtime WebSocket protocol, currently defaulting to gpt-realtime-2.1.

GPTLiveVoiceAdapter targets the GPT-Live primary WebSocket contract at /v1/live/sessions and defaults to gpt-live-1 with client delegation. It waits for session.started, sends raw mono PCM16 frames and receives audio/transcript events, surfaces client delegation IDs, returns results through session.commentary.append or session.thinking.append, and checks whether session.closed confirmed final usage during shutdown.

LiveDelegationCoordinator consumes each original transcript fragment with timestamps, retains a bounded recent history, and bridges client delegations to any ReasoningEngine. It blocks duplicate backend execution for the same delegation and saves each result before delivery; if delivery fails, it can retry the saved result without rerunning model/tool calls. The host still decides when the transcript is sufficient, enforces permissions and idempotency for side effects, and handles backend cancellation.

GPT-Live append updates are conservatively capped to 450 UTF-8 bytes to stay below the API's 500-token-per-append limit. The host supplies microphone capture and speaker playback, continuously sends mono PCM16 at the configured rate, and plays queued output in order. This separation allows a future Abacus port to route backend work through OmniRoute or any registered LLM without integrating it into Abacus now.

Brahma currently has its existing Gemini Live loop. Both new adapters are optional and isolated; this code does not change the runtime's active voice provider. Live audio requires correctly paced PCM16 and a real device-to-session test.

Provider availability and model/API fields can change. An API key is necessary but not sufficient: a real microphone-to-model-to-speaker test on the user's account and region is required.

## Quality and safety gates

- Unit tests cover adapter payloads, tool parsing, retry/fallback, cost budget checks, approval gating, bounded audio, and benchmark reporting without paid provider calls.
- A real-provider smoke suite must be opt-in and use valid keys. It should measure latency, completion rate, interruptions, missing audio, tool accuracy, token use, estimated versus actual cost, and unsupported success claims.
- Compare against GPT-6 Astra and GPT-Live on the same tasks, context, permissions, and tool access. Deterministic phrase matching is only a harness sanity check, not an intelligence benchmark.
- Do not wire this into the default runtime until CI passes, live-provider checks are recorded, and performance/cost regressions are reviewed.
