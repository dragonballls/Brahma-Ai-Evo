"""Portable live-voice contract, bounded playback, and OpenAI Realtime adapter.

Audio device acquisition and playback stay under host-app control.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlencode

from .providers import validate_base_url


def _bounded_live_content(value: str, *, max_utf8_bytes: int = 450) -> str:
    """Keep each GPT-Live update conservatively below the API's 500-token limit."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("Live update content must not be empty.")
    encoded = text.encode("utf-8")[:max_utf8_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8").strip()
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    raise ValueError("Live update content has no valid text within the safe payload limit.")


@dataclass(frozen=True)
class VoicePolicy:
    input_sample_rate: int = 24000
    output_sample_rate: int = 24000
    chunk_ms: int = 20
    max_buffered_chunks: int = 48
    max_reconnect_attempts: int = 2
    connect_timeout_seconds: float = 10.0
    close_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.chunk_ms not in {20, 30, 40}:
            raise ValueError("Live audio chunks must be 20, 30, or 40 ms.")
        if self.input_sample_rate <= 0 or self.output_sample_rate <= 0:
            raise ValueError("Audio sample rates must be positive.")
        if self.max_buffered_chunks < 1 or self.max_reconnect_attempts < 0:
            raise ValueError("Voice buffer/reconnect limits are invalid.")
        if self.connect_timeout_seconds <= 0 or self.close_timeout_seconds <= 0:
            raise ValueError("Voice startup and shutdown timeouts must be positive.")


@dataclass(frozen=True)
class VoiceEvent:
    kind: str
    payload: Mapping[str, Any]


class LiveVoiceAdapter(Protocol):
    def connect(self) -> None: ...
    def send_pcm16(self, pcm: bytes) -> None: ...
    def send_text(self, text: str) -> None: ...
    def receive_event(self, timeout_seconds: float = 0.1) -> VoiceEvent | None: ...
    def interrupt(self, *, item_id: str | None = None, audio_end_ms: int | None = None) -> None: ...
    def close(self) -> None: ...


class BoundedPlaybackBuffer:
    """Bounded FIFO that invalidates stale audio when the user interrupts."""
    def __init__(self, max_chunks: int = 48) -> None:
        if max_chunks < 1:
            raise ValueError("max_chunks must be positive.")
        self._max_chunks = max_chunks
        self._items: deque[tuple[int, bytes]] = deque()
        self._generation = 0
        self._lock = threading.Lock()
        self.dropped_chunks = 0

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def push(self, generation: int, chunk: bytes) -> bool:
        data = bytes(chunk)
        if not data:
            return False
        with self._lock:
            if generation != self._generation:
                return False
            while len(self._items) >= self._max_chunks:
                self._items.popleft()
                self.dropped_chunks += 1
            self._items.append((generation, data))
            return True

    def pop(self) -> bytes | None:
        with self._lock:
            while self._items:
                generation, data = self._items.popleft()
                if generation == self._generation:
                    return data
            return None

    def interrupt(self) -> int:
        with self._lock:
            self._generation += 1
            self._items.clear()
            return self._generation

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class GPTLiveVoiceAdapter:
    """GPT-Live voice session with client delegation to a host-owned backend.

    It deliberately uses the GPT-Live /v1/live/sessions event protocol, not the
    separate Realtime /v1/realtime API. The application may route a delegation
    event to ReasoningEngine, OmniRoute, an Abacus LLM wrapper, or any custom
    provider, then return verified results using append_delegation_result().
    """
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-live-1",
        voice: str = "marin",
        instructions: str = "Be helpful, concise, and transparent. Confirm before side effects.",
        policy: VoicePolicy | None = None,
        websocket_url: str = "wss://api.openai.com/v1/live/sessions",
    ) -> None:
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("An OpenAI API key is required.")
        if not str(websocket_url or "").startswith("wss://"):
            raise ValueError("GPT-Live requires a secure WebSocket (wss://) URL.")
        validated = validate_base_url(websocket_url.replace("wss://", "https://", 1))
        if validated != "https://api.openai.com/v1/live/sessions":
            raise ValueError("GPT-Live credentials may only be sent to the official session endpoint.")
        self.websocket_url = websocket_url.rstrip("/")
        self.model = str(model or "").strip()
        self.voice = str(voice or "marin").strip()
        self.instructions = str(instructions or "")
        self.policy = policy or VoicePolicy()
        if self.policy.input_sample_rate != self.policy.output_sample_rate:
            raise ValueError("GPT-Live requires the same configured audio rate for input and output.")
        self._socket: Any = None
        self._started = False
        self._closed = False
        self._final_usage_confirmed = False
        self._finalization_error: str | None = None
        self._known_delegations: set[str] = set()
        self._send_lock = threading.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._last_usage: Mapping[str, Any] | None = None
        self.playback = BoundedPlaybackBuffer(self.policy.max_buffered_chunks)

    @property
    def last_usage(self) -> Mapping[str, Any] | None:
        return dict(self._last_usage) if self._last_usage is not None else None

    @property
    def session_closed_confirmed(self) -> bool:
        """Whether GPT-Live emitted session.closed and finalized usage."""
        return self._final_usage_confirmed

    @property
    def finalization_error(self) -> str | None:
        """Diagnostic when shutdown did not receive session.closed; None when confirmed."""
        return self._finalization_error

    def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is required for GPT-Live voice.") from exc
        sock = websocket.create_connection(
            self.websocket_url,
            header=[f"Authorization: Bearer {self.api_key}"],
            timeout=self.policy.connect_timeout_seconds,
            enable_multithread=True,
        )
        self._socket = sock
        try:
            self._send({
                "type": "session.start",
                "event_id": "brahma_session_start",
                "session": {
                    "model": self.model,
                    "instructions": self.instructions,
                    "audio": {
                        "format": {"type": "audio/pcm", "rate": self.policy.input_sample_rate},
                        "output": {"voice": self.voice},
                    },
                    "delegation": {"type": "client"},
                },
            })
            deadline = time.monotonic() + self.policy.connect_timeout_seconds
            while time.monotonic() < deadline:
                sock.settimeout(max(0.05, deadline - time.monotonic()))
                try:
                    raw = sock.recv()
                except Exception as exc:
                    try:
                        import websocket
                        if isinstance(exc, websocket.WebSocketTimeoutException):
                            continue
                    except ImportError:
                        pass
                    raise RuntimeError(f"GPT-Live session startup failed ({type(exc).__name__}).") from exc
                event = self._parse_event(raw)
                self._pending.append(event)
                event_type = str(event.get("type") or "")
                if event_type == "session.started":
                    self._started = True
                    return
                if event_type == "error":
                    raise RuntimeError("GPT-Live rejected the session.start event.")
            raise TimeoutError("Timed out waiting for GPT-Live session.started.")
        except Exception:
            self.close()
            raise

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self._socket is None or self._closed:
            raise RuntimeError("GPT-Live session is not connected.")
        if not self._started and payload.get("type") != "session.start":
            raise RuntimeError("Wait for session.started before sending GPT-Live events.")
        with self._send_lock:
            self._socket.send(json.dumps(dict(payload), ensure_ascii=False))

    @staticmethod
    def _parse_event(raw: Any) -> dict[str, Any]:
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("GPT-Live returned invalid JSON.") from exc
        if not isinstance(event, dict):
            raise RuntimeError("GPT-Live returned an unexpected event.")
        return event

    def send_pcm16(self, pcm: bytes) -> None:
        if not self._started or self._closed:
            raise RuntimeError("GPT-Live has not emitted session.started.")
        data = bytes(pcm)
        if len(data) % 2:
            raise ValueError("PCM16 audio must contain complete 16-bit samples.")
        if data:
            self._send({
                "type": "session.input_audio.append",
                "audio": base64.b64encode(data).decode("ascii"),
            })

    def send_text(self, text: str) -> None:
        raise NotImplementedError(
            "Route typed text through the host intelligence engine and return the verified "
            "result with append_delegation_result(); this session adapter handles live audio."
        )

    def _event_to_voice_event(self, event: Mapping[str, Any]) -> VoiceEvent:
        event_type = str(event.get("type") or "unknown")
        if event_type == "session.output_audio.delta":
            try:
                pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
            except (ValueError, TypeError):
                pcm = b""
            return VoiceEvent("audio_delta", {"pcm16": pcm})
        if event_type in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            return VoiceEvent("transcript_delta", {
                "text": str(event.get("delta") or ""),
                "source_type": event_type,
                "start_ms": event.get("start_ms"),
                "end_ms": event.get("end_ms"),
            })
        if event_type == "session.delegation.created":
            delegation = event.get("delegation") if isinstance(event.get("delegation"), dict) else {}
            delegation_id = str(delegation.get("id") or "").strip()
            target = str(delegation.get("target") or "")
            if delegation_id and target == "client":
                self._known_delegations.add(delegation_id)
            return VoiceEvent("delegation", {
                "delegation_id": delegation_id or None,
                "target": target,
                "offset_ms": event.get("offset_ms"),
            })
        if event_type == "session.started":
            return VoiceEvent("session_state", event)
        if event_type == "session.closed":
            return VoiceEvent("session_closed", {"usage": event.get("usage"), "session": event.get("session")})
        if event_type == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            return VoiceEvent("error", {
                "code": error.get("code"),
                "message": str(error.get("message") or "GPT-Live provider error")[:240],
                "client_event_id": error.get("client_event_id"),
            })
        return VoiceEvent("provider_event", event)

    def receive_event(self, timeout_seconds: float = 0.1) -> VoiceEvent | None:
        if self._socket is None or self._closed:
            raise RuntimeError("GPT-Live session is not connected.")
        if self._pending:
            event = self._pending.popleft()
        else:
            try:
                self._socket.settimeout(max(0.01, float(timeout_seconds)))
                event = self._parse_event(self._socket.recv())
            except Exception as exc:
                try:
                    import websocket
                    if isinstance(exc, websocket.WebSocketTimeoutException):
                        return None
                except ImportError:
                    pass
                raise RuntimeError(f"GPT-Live receive failed ({type(exc).__name__}).") from exc
        result = self._event_to_voice_event(event)
        if result.kind == "audio_delta":
            pcm = bytes(result.payload.get("pcm16", b""))
            queued = self.playback.push(self.playback.generation, pcm)
            result = VoiceEvent("audio_delta", {"byte_count": len(pcm), "queued": queued})
        if result.kind == "session_closed":
            usage = result.payload.get("usage")
            self._last_usage = usage if isinstance(usage, Mapping) else None
            self._final_usage_confirmed = True
            self._closed = True
        return result

    def append_delegation_result(self, delegation_id: str, content: str, *, spoken: bool = True) -> None:
        """Return validated host-backend results for GPT-Live to say or reason over."""
        task_id = str(delegation_id or "").strip()
        if not task_id:
            raise ValueError("delegation_id is required.")
        if task_id not in self._known_delegations:
            raise ValueError("delegation_id must refer to an active client delegation from this session.")
        bounded_content = _bounded_live_content(content)
        self._send({
            "type": "session.commentary.append" if spoken else "session.thinking.append",
            "event_id": "brahma_result_" + uuid.uuid4().hex,
            "delegation_id": task_id,
            "content": bounded_content,
        })

    def append_instruction(self, content: str) -> None:
        bounded_content = _bounded_live_content(content)
        self._send({
            "type": "session.instructions.append",
            "event_id": "brahma_instruction_" + uuid.uuid4().hex,
            "delegation_id": None,
            "content": bounded_content,
        })

    def interrupt(self, *, item_id: str | None = None, audio_end_ms: int | None = None) -> None:
        # GPT-Live manages speech interruption. The host owns cancellation of any
        # already-running delegated backend job.
        self.playback.interrupt()

    def close(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            if self._started and not self._closed and not self._final_usage_confirmed:
                try:
                    self._send({"type": "session.close"})
                except Exception as exc:
                    self._finalization_error = f"session.close could not be sent ({type(exc).__name__})"
                deadline = time.monotonic() + self.policy.close_timeout_seconds
                while not self._final_usage_confirmed and time.monotonic() < deadline:
                    try:
                        sock.settimeout(max(0.01, deadline - time.monotonic()))
                        event = self._parse_event(sock.recv())
                    except Exception as exc:
                        self._finalization_error = (
                            f"session.closed was not received ({type(exc).__name__})"
                        )
                        break
                    if event.get("type") == "session.closed":
                        usage = event.get("usage")
                        self._last_usage = usage if isinstance(usage, Mapping) else None
                        self._final_usage_confirmed = True
                        self._finalization_error = None
                        self._closed = True
                        break
                if not self._final_usage_confirmed and self._finalization_error is None:
                    self._finalization_error = "session.closed was not received before the close timeout"
        finally:
            self.playback.interrupt()
            self._socket = None
            self._closed = True
            try:
                sock.close()
            except Exception:
                pass




@dataclass(frozen=True)
class TranscriptSegment:
    speaker: str
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class _ClientDelegationState:
    delegation_id: str
    offset_ms: int | None
    status: str = "pending"
    result_text: str = ""
    error: str = ""


class LiveDelegationCoordinator:
    """Bridge GPT-Live client delegations to any host-owned reasoning backend.

    The delegation-created event does not contain the spoken task. Feed transcript
    and delegation VoiceEvents to observe(), then let the application decide when
    there is enough context to run the backend. Provider choice, tools, permissions,
    confirmation, and cancellation remain owned by the host application.
    """

    def __init__(
        self,
        *,
        max_segments: int = 256,
        max_transcript_chars: int = 40000,
        max_delegations: int = 64,
    ) -> None:
        if max_segments < 1 or max_transcript_chars < 1000 or max_delegations < 1:
            raise ValueError("Transcript and delegation limits are invalid.")
        self.max_segments = max_segments
        self.max_transcript_chars = max_transcript_chars
        self.max_delegations = max_delegations
        self._segments: deque[TranscriptSegment] = deque()
        self._delegations: dict[str, _ClientDelegationState] = {}
        self._lock = threading.RLock()
        self._transcript_chars = 0

    def observe(self, event: VoiceEvent) -> str | None:
        """Record an event and return a new ID only for a fresh client delegation."""
        with self._lock:
            if event.kind == "transcript_delta":
                text = str(event.payload.get("text") or "")
                if not text:
                    return None
                source_type = str(event.payload.get("source_type") or "")
                speaker = "user" if "input_transcript" in source_type else "assistant"
                start = event.payload.get("start_ms")
                end = event.payload.get("end_ms")
                start_ms = int(start) if isinstance(start, (int, float)) else None
                end_ms = int(end) if isinstance(end, (int, float)) else None
                # Keep original provider fragments and timestamps intact. GPT-Live may
                # emit partial/corrected fragments; grouping belongs in the UI layer.
                self._segments.append(TranscriptSegment(speaker, text, start_ms, end_ms))
                self._transcript_chars += len(text)
                self._trim_transcript_locked()
                return None

            if event.kind != "delegation":
                return None
            delegation_id = str(event.payload.get("delegation_id") or "").strip()
            if str(event.payload.get("target") or "") != "client" or not delegation_id:
                return None
            if delegation_id in self._delegations:
                return None
            active_count = sum(
                state.status in {"pending", "running", "result_ready"}
                for state in self._delegations.values()
            )
            if active_count >= self.max_delegations:
                raise RuntimeError("Too many active client delegations; finish or close existing work.")
            offset = event.payload.get("offset_ms")
            offset_ms = int(offset) if isinstance(offset, (int, float)) else None
            self._delegations[delegation_id] = _ClientDelegationState(delegation_id, offset_ms)
            return delegation_id

    def _trim_transcript_locked(self) -> None:
        while self._segments and (
            len(self._segments) > self.max_segments
            or self._transcript_chars > self.max_transcript_chars
        ):
            segment = self._segments.popleft()
            self._transcript_chars -= len(segment.text)
        if self._segments and len(self._segments[0].text) > self.max_transcript_chars:
            first = self._segments[0]
            clipped = first.text[-self.max_transcript_chars:]
            self._segments[0] = TranscriptSegment(first.speaker, clipped, first.start_ms, first.end_ms)
            self._transcript_chars = sum(len(item.text) for item in self._segments)

    def transcript_snapshot(self) -> tuple[TranscriptSegment, ...]:
        with self._lock:
            return tuple(self._segments)

    def status(self, delegation_id: str) -> str:
        with self._lock:
            state = self._delegations.get(str(delegation_id or ""))
            if state is None:
                raise KeyError("Unknown client delegation ID.")
            return state.status

    def context_for(self, delegation_id: str, *, application_context: str = "") -> str:
        with self._lock:
            state = self._delegations.get(str(delegation_id or ""))
            if state is None:
                raise KeyError("Unknown client delegation ID.")
            segments = tuple(self._segments)
            offset_ms = state.offset_ms
        rendered: list[str] = []
        for segment in segments:
            stamp = ""
            if segment.start_ms is not None or segment.end_ms is not None:
                start = segment.start_ms if segment.start_ms is not None else "?"
                end = segment.end_ms if segment.end_ms is not None else "?"
                stamp = f" [{start}-{end} ms]"
            rendered.append(f"{segment.speaker}{stamp}: {segment.text}")
        parts = [
            "The live transcript contains partial and sometimes corrected speech fragments; it is not a perfect transcript. "
            "Use later corrections and current application state. Ask for clarification if ambiguity could materially change "
            "the answer or action. Never claim an action completed without verified output from the actual tool or service.",
            f"Client delegation ID: {delegation_id}",
            f"Delegation offset (session-relative ms): {offset_ms if offset_ms is not None else 'unknown'}",
            "Recent live transcript:",
            "\n".join(rendered) if rendered else "(No transcript fragments received yet.)",
        ]
        if application_context.strip():
            parts.extend(["Current application context:", application_context.strip()[:20000]])
        return "\n\n".join(parts)

    def run_delegation(
        self,
        delegation_id: str,
        engine: Any,
        voice_adapter: Any,
        *,
        application_context: str = "",
        task_prompt: str = (
            "Resolve the caller's latest request using the live transcript and application context. "
            "Return verified findings and a useful next step. Ask one concise clarifying question if necessary."
        ),
        system: str = (
            "You are the backend task agent for a live voice assistant. The host owns permissions and tools. "
            "Never invent successful actions; use only registered and authorized tools. Treat speech transcripts as fallible."
        ),
        complexity: str = "complex",
        tools: Sequence[Any] = (),
        authorize_tool: Callable[[Any, Mapping[str, Any]], bool] | None = None,
        strategy: str = "auto",
    ) -> Any:
        """Run backend reasoning once. Delivery failures can be retried without repeating backend calls."""
        task_id = str(delegation_id or "").strip()
        with self._lock:
            state = self._delegations.get(task_id)
            if state is None:
                raise KeyError("Unknown client delegation ID.")
            if state.status != "pending":
                raise RuntimeError(f"Delegation is already {state.status}; refusing duplicate backend execution.")
            state.status = "running"
        try:
            context = self.context_for(task_id, application_context=application_context)
            result = engine.answer(
                task_prompt,
                system=system,
                context=context,
                complexity=complexity,
                strategy=strategy,
                tools=tools,
                authorize_tool=authorize_tool,
            )
            result_text = str(result.text or "").strip()
            if not result_text:
                raise RuntimeError("Backend returned an empty result; no voice result was sent.")
        except Exception as exc:
            with self._lock:
                state.status = "failed"
                state.error = type(exc).__name__
            raise
        with self._lock:
            state.result_text = result_text
            state.status = "result_ready"
        self.deliver_result(task_id, voice_adapter, spoken=True)
        return result

    def deliver_result(self, delegation_id: str, voice_adapter: Any, *, spoken: bool = True) -> None:
        """Deliver a saved result. If sending fails, the saved result stays retryable."""
        task_id = str(delegation_id or "").strip()
        with self._lock:
            state = self._delegations.get(task_id)
            if state is None:
                raise KeyError("Unknown client delegation ID.")
            if state.status != "result_ready" or not state.result_text:
                raise RuntimeError("No saved backend result is ready for delivery.")
            result_text = state.result_text
        voice_adapter.append_delegation_result(task_id, result_text, spoken=spoken)
        with self._lock:
            state.status = "delivered"


class OpenAIRealtimeVoiceAdapter:
    """OpenAI Realtime speech-to-speech over WebSocket.

    Caller supplies signed PCM16 frames at the configured input rate and plays
    decoded audio_delta events at the output rate. This adapter does not access
    a microphone or speakers and does not execute model-requested tools.
    """
    def __init__(
        self, api_key: str, *, model: str = "gpt-realtime-2.1", voice: str = "marin",
        instructions: str = "Be clear, helpful, concise, and transparent. Confirm before side effects.",
        tools: tuple[Mapping[str, Any], ...] = (), policy: VoicePolicy | None = None,
        websocket_url: str = "wss://api.openai.com/v1/realtime",
    ) -> None:
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("An OpenAI API key is required.")
        validate_base_url(websocket_url.replace("wss://", "https://", 1))
        if not websocket_url.startswith("wss://api.openai.com/"):
            raise ValueError("The OpenAI realtime adapter only sends credentials to api.openai.com.")
        self.websocket_url = websocket_url.rstrip("/")
        self.model = str(model or "").strip()
        self.voice = str(voice or "marin").strip()
        self.instructions = str(instructions or "")
        self.tools = tuple(dict(tool) for tool in tools)
        self.policy = policy or VoicePolicy()
        self._socket: Any = None
        self._send_lock = threading.Lock()
        self.playback = BoundedPlaybackBuffer(self.policy.max_buffered_chunks)

    def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is required for OpenAI Realtime voice.") from exc
        url = self.websocket_url + "?" + urlencode({"model": self.model})
        self._socket = websocket.create_connection(
            url,
            header=[f"Authorization: Bearer {self.api_key}"],
            timeout=self.policy.connect_timeout_seconds,
            enable_multithread=True,
        )
        self._send({
            "type": "session.update",
            "session": {
                "type": "realtime", "model": self.model,
                "output_modalities": ["audio", "text"],
                "instructions": self.instructions,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.policy.input_sample_rate},
                        "turn_detection": {"type": "semantic_vad"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": self.policy.output_sample_rate},
                        "voice": self.voice,
                    },
                },
                **({"tools": list(self.tools), "tool_choice": "auto"} if self.tools else {}),
            },
        })

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self._socket is None:
            raise RuntimeError("Realtime session is not connected.")
        with self._send_lock:
            self._socket.send(json.dumps(dict(payload), ensure_ascii=False))

    def send_pcm16(self, pcm: bytes) -> None:
        if pcm:
            self._send({"type": "input_audio_buffer.append",
                        "audio": base64.b64encode(bytes(pcm)).decode("ascii")})

    def send_text(self, text: str) -> None:
        if not str(text or "").strip():
            return
        self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": str(text)}],
        }})
        self._send({"type": "response.create"})

    def receive_event(self, timeout_seconds: float = 0.1) -> VoiceEvent | None:
        if self._socket is None:
            raise RuntimeError("Realtime session is not connected.")
        try:
            self._socket.settimeout(max(0.01, float(timeout_seconds)))
            raw = self._socket.recv()
        except Exception as exc:
            try:
                import websocket
                if isinstance(exc, websocket.WebSocketTimeoutException):
                    return None
            except ImportError:
                pass
            raise RuntimeError(f"Realtime receive failed: {type(exc).__name__}") from exc
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Realtime server returned invalid JSON.") from exc
        if not isinstance(event, dict):
            raise RuntimeError("Realtime server returned an unexpected event.")
        event_type = str(event.get("type") or "unknown")
        if event_type in {"response.output_audio.delta", "response.audio.delta"}:
            try:
                pcm = base64.b64decode(str(event.get("delta") or ""), validate=True)
            except (ValueError, TypeError):
                pcm = b""
            queued = self.playback.push(self.playback.generation, pcm)
            return VoiceEvent("audio_delta", {"byte_count": len(pcm), "queued": queued, "item_id": event.get("item_id")})
        if event_type in {"response.output_text.delta", "response.text.delta", "response.audio_transcript.delta"}:
            return VoiceEvent("transcript_delta", {"text": str(event.get("delta") or ""), "source_type": event_type})
        if event_type in {"input_audio_buffer.speech_started", "response.cancelled", "response.canceled"}:
            self.playback.interrupt()
            return VoiceEvent("interrupted", event)
        if event_type in {"response.function_call_arguments.done", "response.output_item.done"}:
            item = event.get("item") if isinstance(event.get("item"), dict) else event
            if item.get("type") == "function_call":
                return VoiceEvent("tool_call", {
                    "call_id": item.get("call_id") or item.get("id"),
                    "name": item.get("name"), "arguments": item.get("arguments"),
                })
        if event_type == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            return VoiceEvent("error", {"code": error.get("code"), "type": error.get("type"),
                                        "message": str(error.get("message") or "Realtime provider error")[:300]})
        if event_type in {"response.done", "response.completed"}:
            return VoiceEvent("turn_complete", event)
        if event_type in {"session.created", "session.updated"}:
            return VoiceEvent("session_state", event)
        return VoiceEvent("provider_event", event)

    def interrupt(self, *, item_id: str | None = None, audio_end_ms: int | None = None) -> None:
        self.playback.interrupt()
        try:
            self._send({"type": "response.cancel"})
        except RuntimeError:
            return
        if item_id and audio_end_ms is not None and audio_end_ms >= 0:
            self._send({"type": "conversation.item.truncate", "item_id": item_id,
                        "content_index": 0, "audio_end_ms": int(audio_end_ms)})

    def send_tool_result(self, call_id: str, output: Any) -> None:
        try:
            rendered = json.dumps(output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(output)
        self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": str(call_id), "output": rendered[:20000],
        }})
        self._send({"type": "response.create"})

    def close(self) -> None:
        socket, self._socket = self._socket, None
        self.playback.interrupt()
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
