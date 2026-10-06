from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4


class ProtocolTypes:
    HELLO = "hello"
    PAIR_REQUEST = "pair_request"
    PAIR_APPROVED = "pair_approved"
    AUTHENTICATE = "authenticate"
    DEVICE_ONLINE = "device_online"
    DEVICE_OFFLINE = "device_offline"
    CAPABILITIES = "capabilities"
    EXECUTE = "execute"
    RESULT = "result"
    EVENT = "event"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"
    FILE_TRANSFER = "file_transfer"
    SCREEN_CAPTURE = "screen_capture"
    CHAT_MESSAGE = "chat_message"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request_id() -> str:
    return uuid4().hex


def build_message(message_type: str, payload: dict | None = None, request_id: str | None = None, timestamp: str | None = None) -> dict:
    if not isinstance(message_type, str) or not message_type.strip():
        raise TypeError("message_type must be a non-empty string")
    if payload is not None and not isinstance(payload, dict):
        raise TypeError("payload must be a JSON object")
    if request_id is not None and not isinstance(request_id, str):
        raise TypeError("request_id must be a string")
    if timestamp is not None and not isinstance(timestamp, str):
        raise TypeError("timestamp must be a string")
    return {
        "type": message_type,
        "request_id": request_id or new_request_id(),
        "timestamp": timestamp or now_iso(),
        "payload": payload if payload is not None else {},
    }


def validate_message(message: dict) -> tuple[bool, str]:
    if not isinstance(message, dict):
        return False, "Message must be a JSON object."
    for key in ("type", "request_id", "timestamp"):
        value = message.get(key)
        if not isinstance(value, str) or not value.strip():
            return False, f"Field '{key}' must be a non-empty string."
    if "payload" in message and message["payload"] is not None and not isinstance(message["payload"], dict):
        return False, "Payload must be a JSON object."
    return True, ""
