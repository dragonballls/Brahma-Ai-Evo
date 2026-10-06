from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class PairingOffer:
    service: str
    host: str
    port: int
    pairing_token: str
    pairing_code: str
    expires_at: float
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "host": self.host,
            "port": self.port,
            "pairing_token": self.pairing_token,
            "pairing_code": self.pairing_code,
            "expires": int(max(0, self.expires_at - datetime.now(timezone.utc).timestamp())),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class DeviceRecord:
    device_id: str
    name: str
    platform: str
    os_version: str = ""
    agent_version: str = ""
    ip: str = ""
    online: bool = False
    last_seen: str = ""
    battery: int | None = None
    capabilities: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    paired_at: str = ""
    secret_hash: str = ""
    revoked: bool = False
    connection_id: str = ""
    identity_fingerprint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the public-safe device representation used by UI/API/tool results."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "platform": self.platform,
            "os_version": self.os_version,
            "agent_version": self.agent_version,
            "ip": self.ip,
            "online": self.online,
            "last_seen": self.last_seen,
            "battery": self.battery,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "paired_at": self.paired_at,
            "revoked": self.revoked,
            "metadata": dict(self.metadata),
        }

    def to_storage_dict(self) -> dict[str, Any]:
        """Return the complete on-disk representation, including internal secrets."""
        payload = self.to_dict()
        payload.update({
            "secret_hash": self.secret_hash,
            "connection_id": self.connection_id,
            "identity_fingerprint": self.identity_fingerprint,
        })
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceRecord":
        return cls(
            device_id=str(data.get("device_id", "")),
            name=str(data.get("name", "Unknown Device")),
            platform=str(data.get("platform", "unknown")),
            os_version=str(data.get("os_version", "")),
            agent_version=str(data.get("agent_version", "")),
            ip=str(data.get("ip", "")),
            online=bool(data.get("online", False)),
            last_seen=str(data.get("last_seen", "")),
            battery=_optional_int(data.get("battery")),
            capabilities=_string_list(data.get("capabilities")),
            permissions=_string_list(data.get("permissions")),
            paired_at=str(data.get("paired_at", "")),
            secret_hash=str(data.get("secret_hash", "")),
            revoked=bool(data.get("revoked", False)),
            connection_id=str(data.get("connection_id", "")),
            identity_fingerprint=str(data.get("identity_fingerprint", "")),
            metadata=_string_mapping(data.get("metadata")),
        )
