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
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode

from .providers import validate_base_url


@dataclass(frozen=True)
class VoicePolicy:
    input_sample_rate: int = 24000
    output_sample_rate: int = 24000
    chunk_ms: int = 20
    max_buffered_chunks: int = 48
    max_reconnect_attempts: int = 2
    connect_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.chunk_ms not in {20, 30, 40}:
            raise ValueError("Live audio chunks must be 20, 30, or 40 ms.")
        if self.input_sample_rate <= 0 or self.output_sample_rate <= 0:
            raise ValueError("Audio sample rates must be positive.")
        if self.max_buffered_chunks < 1 or self.max_reconnect_attempts < 0:
            raise ValueError("Voice buffer/reconnect limits are invalid.")


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

    @staticmethod
    def _event_to_voice_event(event: Mapping[str, Any]) -> VoiceEvent:
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
            return VoiceEvent("delegation", {
                "delegation_id": delegation.get("id"),
                "target": delegation.get("target"),
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
            self.playback.push(self.playback.generation, bytes(result.payload.get("pcm16", b"")))
        if result.kind == "session_closed":
            usage = result.payload.get("usage")
            self._last_usage = usage if isinstance(usage, Mapping) else None
            self._final_usage_confirmed = True
            self._closed = True
        return result

    def append_delegation_result(self, delegation_id: str, content: str, *, spoken: bool = True) -> None:
        """Return validated host-backend results for GPT-Live to say or reason over."""
        task_id = str(delegation_id or "").strip()
        text = str(content or "").strip()
        if not task_id:
            raise ValueError("delegation_id is required.")
        if not text:
            raise ValueError("A non-empty delegated result is required.")
        self._send({
            "type": "session.commentary.append" if spoken else "session.thinking.append",
            "event_id": "brahma_result_" + uuid.uuid4().hex,
            "delegation_id": task_id,
            "content": text[:1600],
        })

    def append_instruction(self, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            raise ValueError("Instruction content is required.")
        self._send({
            "type": "session.instructions.append",
            "event_id": "brahma_instruction_" + uuid.uuid4().hex,
            "content": text[:1600],
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
            if self._started and not self._closed:
                self._send({"type": "session.close"})
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    try:
                        sock.settimeout(max(0.05, deadline - time.monotonic()))
                        event = self._parse_event(sock.recv())
                    except Exception:
                        break
                    if event.get("type") == "session.closed":
                        usage = event.get("usage")
                        self._last_usage = usage if isinstance(usage, Mapping) else None
                        self._final_usage_confirmed = True
                        self._closed = True
                        break
        finally:
            self.playback.interrupt()
            self._socket = None
            self._closed = True
            try:
                sock.close()
            except Exception:
                pass


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
            self.playback.push(self.playback.generation, pcm)
            return VoiceEvent("audio_delta", {"pcm16": pcm, "item_id": event.get("item_id")})
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
