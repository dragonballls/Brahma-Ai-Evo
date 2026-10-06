from __future__ import annotations

import asyncio
import json
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from .capability_manager import CapabilityManager
from .command_router import CommandRouter
from .device_manager import DeviceManager
from .discovery import GatewayDiscovery, local_ip
from .models import PairingOffer
from .pairing import PairingManager
from .protocol import ProtocolTypes, build_message, validate_message, now_iso, new_request_id
from .websocket import ConnectionHub
from core.local_tls import certificate_sha256, ensure_local_certificate


def _safe_int(value: object, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    if maximum is not None and parsed > maximum:
        return default
    return parsed


def _safe_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


_SENSITIVE_LOG_KEYS = {
    "api_key", "apikey", "authorization", "bearer", "client_secret",
    "device_secret", "pairing_code", "pairing_token", "pin", "password",
    "secret", "secret_hash", "session_secret", "session_token", "token",
}


def _redact_log_value(value: Any, key: str = "") -> Any:
    if key.casefold() in _SENSITIVE_LOG_KEYS or any(
        marker in key.casefold() for marker in ("api_key", "secret", "password", "token", "pairing", "pin")
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact_log_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_log_value(item) for item in value]
    return value


def _default_config_path(base_dir: Path) -> Path:
    return Path(base_dir) / "config" / "brahma_connect.json"


def _default_registry_path(base_dir: Path) -> Path:
    return Path(base_dir) / "config" / "brahma_connect" / "devices.json"


@dataclass(slots=True)
class BrahmaGatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    enabled: bool = True
    advertise: bool = True
    service_name: str = "_BRAHMA._tcp.local."
    pairing_ttl_seconds: int = 300
    request_timeout_seconds: int = 30
    tls_enabled: bool = True
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    config_path: Path | None = None
    registry_path: Path | None = None

    @classmethod
    def load(cls, base_dir: Path, override: dict[str, Any] | None = None) -> "BrahmaGatewayConfig":
        base_dir = Path(base_dir)
        data: dict[str, Any] = {
            "host": "0.0.0.0",
            "port": 8765,
            "enabled": True,
            "advertise": True,
            "service_name": "_BRAHMA._tcp.local.",
            "pairing_ttl_seconds": 300,
            "request_timeout_seconds": 30,
            "tls_enabled": True,
        }
        config_path = _default_config_path(base_dir)
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "Brahma Connect configuration is unreadable or corrupted."
                ) from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(
                    "Brahma Connect configuration has an invalid root schema."
                )
            data.update(loaded)
        if override:
            data.update({k: v for k, v in override.items() if v is not None})
        return cls(
            host=str(data.get("host", "0.0.0.0")).strip() or "0.0.0.0",
            port=_safe_int(data.get("port", 8765), 8765, minimum=1, maximum=65535),
            enabled=_safe_bool(data.get("enabled", True), True),
            advertise=_safe_bool(data.get("advertise", True), True),
            service_name=str(data.get("service_name", "_BRAHMA._tcp.local.")).strip() or "_BRAHMA._tcp.local.",
            pairing_ttl_seconds=_safe_int(data.get("pairing_ttl_seconds", 300), 300, minimum=60),
            request_timeout_seconds=_safe_int(data.get("request_timeout_seconds", 30), 30, minimum=1),
            tls_enabled=_safe_bool(data.get("tls_enabled", True), True),
            tls_certfile=Path(str(data.get("tls_certfile") or "")).expanduser() if data.get("tls_certfile") else None,
            tls_keyfile=Path(str(data.get("tls_keyfile") or "")).expanduser() if data.get("tls_keyfile") else None,
            config_path=config_path,
            registry_path=_default_registry_path(base_dir),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "enabled": self.enabled,
            "advertise": self.advertise,
            "service_name": self.service_name,
            "pairing_ttl_seconds": self.pairing_ttl_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "tls_enabled": self.tls_enabled,
        }

    def save(self) -> None:
        if self.config_path is None:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


class BrahmaGateway:
    def __init__(self, base_dir: Path, config: BrahmaGatewayConfig | None = None):
        self.base_dir = Path(base_dir)
        self.config = config or BrahmaGatewayConfig.load(self.base_dir)
        self.device_manager = DeviceManager(self.config.registry_path or _default_registry_path(self.base_dir))
        self.capability_manager = CapabilityManager()
        self.hub = ConnectionHub()
        self.command_router = CommandRouter(self.device_manager, self.hub, self.capability_manager)
        self.pairing_manager = PairingManager(self.config.service_name, self.config.pairing_ttl_seconds)
        self.discovery = GatewayDiscovery(self.config.service_name)
        self._tls_certfile: Path | None = None
        self._tls_keyfile: Path | None = None
        self._tls_fingerprint: str = ""
        self._running = False
        self._shutdown = threading.Event()
        self._server: uvicorn.Server | None = None
        self._log: list[dict[str, Any]] = []
        self._log_lock = threading.RLock()
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.RLock()
        self._pair_attempts: dict[str, tuple[int, float]] = {}
        self._serve_lock = threading.Lock()
        self.on_chat_message = None
        self.app = self._build_app()

    def is_running(self) -> bool:
        with self._serve_lock:
            return bool(self._running and not self._shutdown.is_set())

    def prepare_start(self) -> None:
        """Arm a gateway start without clearing a concurrent shutdown request."""
        with self._serve_lock:
            if not self._running:
                self._shutdown.clear()

    def request_shutdown(self) -> None:
        with self._serve_lock:
            self._shutdown.set()
            if self._server is not None:
                self._server.should_exit = True

    def _append_log(self, event_type: str, **payload: Any) -> None:
        safe_payload = {str(key): _redact_log_value(value, str(key)) for key, value in payload.items()}
        entry = {"type": event_type, "timestamp": now_iso(), **safe_payload}
        with self._log_lock:
            self._log.append(entry)
            self._log = self._log[-200:]

    def log(self) -> list[dict[str, Any]]:
        with self._log_lock:
            return list(self._log)

    def list_devices(self) -> list[dict[str, Any]]:
        return self.device_manager.list_devices()

    def get_device(self, device_or_target: str) -> dict[str, Any] | None:
        query = str(device_or_target or "").strip()
        if not query:
            return None
        record = self.device_manager.get(query)
        if record is None:
            matches = self.device_manager.resolve(query)
            record = matches[0] if len(matches) == 1 else None
        return record.to_dict() if record else None

    def resolve_devices(self, query: str) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.device_manager.resolve(query)]

    def rename_device(self, device_or_target: str, new_name: str) -> dict[str, Any]:
        query = str(device_or_target or "").strip()
        record = self.device_manager.get(query)
        if record is None:
            matches = self.device_manager.resolve(query)
            if len(matches) == 1:
                record = matches[0]
            elif len(matches) > 1:
                return {
                    "success": False,
                    "error": "Multiple devices matched the request.",
                    "error_code": "MULTIPLE_DEVICES",
                    "matches": [item.to_dict() for item in matches],
                }
        if record is None:
            return {"success": False, "error": "Device not found.", "error_code": "DEVICE_NOT_FOUND"}
        renamed = self.device_manager.rename(record.device_id, new_name)
        if renamed is None:
            return {"success": False, "error": "Device not found.", "error_code": "DEVICE_NOT_FOUND"}
        self._append_log("DEVICE_RENAMED", device_id=renamed.device_id, name=renamed.name)
        return {"success": True, "device": renamed.to_dict()}

    def get_capabilities(self, device_or_target: str) -> dict[str, Any]:
        record = self.device_manager.get(str(device_or_target or "").strip())
        if record is None:
            matches = self.device_manager.resolve(device_or_target)
            if len(matches) == 1:
                record = matches[0]
            elif len(matches) > 1:
                return {
                    "success": False,
                    "error": "Multiple devices matched the request.",
                    "error_code": "MULTIPLE_DEVICES",
                    "matches": [item.to_dict() for item in matches],
                }
        if record is None:
            return {"success": False, "error": "Device not found.", "error_code": "DEVICE_NOT_FOUND"}
        return {
            "success": True,
            "device": record.to_dict(),
            "capabilities": list(record.capabilities),
            "permissions": list(record.permissions),
        }

    async def disconnect_device(self, device_or_target: str, *, reason: str = "Disconnected by Brahma") -> dict[str, Any]:
        query = str(device_or_target or "").strip()
        matches = self.device_manager.resolve(query)
        record = None
        if self.device_manager.get(query):
            record = self.device_manager.get(query)
        elif len(matches) == 1:
            record = matches[0]
        elif len(matches) > 1:
            return {
                "success": False,
                "error": "Multiple devices matched the request.",
                "error_code": "MULTIPLE_DEVICES",
                "matches": [item.to_dict() for item in matches],
            }
        if record is None:
            return {"success": False, "error": "Device not found.", "error_code": "DEVICE_NOT_FOUND"}

        disconnected = False
        try:
            disconnected = await self.hub.close_device(record.device_id, reason=reason)
        except Exception:
            disconnected = False
        self.device_manager.mark_offline(record.device_id)
        self._append_log("DEVICE_DISCONNECTED", device_id=record.device_id, name=record.name, forced=disconnected)
        return {"success": True, "device": record.to_dict(), "disconnected": disconnected}

    async def reconnect_device(self, device_or_target: str) -> dict[str, Any]:
        query = str(device_or_target or "").strip()
        matches = self.device_manager.resolve(query)
        record = self.device_manager.get(query)
        if record is None and len(matches) == 1:
            record = matches[0]
        elif record is None and len(matches) > 1:
            return {
                "success": False,
                "error": "Multiple devices matched the request.",
                "error_code": "MULTIPLE_DEVICES",
                "matches": [item.to_dict() for item in matches],
            }
        if record is None:
            return {"success": False, "error": "Device not found.", "error_code": "DEVICE_NOT_FOUND"}
        state = await self.hub.get(record.device_id)
        if state is None:
            return {
                "success": False,
                "device": record.to_dict(),
                "error": f"Your {record.name} is currently offline.",
                "error_code": "DEVICE_OFFLINE",
            }
        self.device_manager.touch(record.device_id)
        self._append_log("DEVICE_RECONNECTED", device_id=record.device_id, name=record.name)
        return {"success": True, "device": record.to_dict(), "reconnected": True}

    def _ensure_tls(self) -> None:
        if not self.config.tls_enabled:
            self._tls_certfile = None
            self._tls_keyfile = None
            self._tls_fingerprint = ""
            return
        cert_dir = self.base_dir / "config" / "certs"
        keyfile = self.config.tls_keyfile or (cert_dir / "gateway.key")
        certfile = self.config.tls_certfile or (cert_dir / "gateway.crt")
        key_path, cert_path = ensure_local_certificate(
            keyfile.parent,
            key_name=keyfile.name,
            cert_name=certfile.name,
            common_name="Brahma Connect Local Gateway",
            hosts=[self.config.host, "localhost", "127.0.0.1", local_ip()],
        )
        self._tls_keyfile = key_path
        self._tls_certfile = cert_path
        self._tls_fingerprint = certificate_sha256(cert_path)

    def create_pairing_offer(self, *, device_name: str = "Unknown Device", platform: str = "unknown") -> dict[str, Any]:
        advertised_host = local_ip() if self.config.host in {"0.0.0.0", "::"} else self.config.host
        self._ensure_tls()
        offer = self.pairing_manager.create_offer(
            advertised_host,
            self.config.port,
            tls_enabled=self.config.tls_enabled,
            tls_certificate_sha256=self._tls_fingerprint,
        )
        self._append_log("PAIRING_REQUEST", device=device_name, platform=platform)
        return offer.to_dict()

    def list_pending_requests(self) -> list[dict[str, Any]]:
        with self._pending_lock:
            items = list(self._pending_requests.items())
        return [
            {
                "pending_id": pending_id,
                "timestamp": item.get("timestamp", ""),
                "device_name": item.get("device_name", "Unknown Device"),
                "platform": item.get("platform", "unknown"),
                "os_version": item.get("os_version", ""),
                "agent_version": item.get("agent_version", ""),
                "capabilities": list(item.get("capabilities") or []),
                "permissions": list(item.get("permissions") or []),
                "ip": item.get("ip", ""),
            }
            for pending_id, item in items
        ]

    async def approve_pending_request(self, pending_id: str) -> dict[str, Any]:
        key = str(pending_id)
        with self._pending_lock:
            item = self._pending_requests.get(key)
            if item is None:
                return {"success": False, "error": "Pending request not found."}
            if item.get("_approving"):
                return {"success": False, "error": "Pairing request is already being approved."}
            item["_approving"] = True

        websocket = item.get("websocket")
        if websocket is None:
            with self._pending_lock:
                self._pending_requests.pop(key, None)
            return {"success": False, "error": "Device is no longer connected."}

        try:
            record, secret = self.device_manager.create_from_pairing(
                name=str(item.get("device_name") or "Unknown Device"),
                platform=str(item.get("platform") or "unknown"),
                os_version=str(item.get("os_version") or ""),
                agent_version=str(item.get("agent_version") or ""),
                ip=str(item.get("ip") or ""),
                battery=item.get("battery"),
                capabilities=list(item.get("capabilities") or []),
                permissions=list(item.get("permissions") or []),
                metadata=dict(item.get("metadata") or {}),
            )
        except Exception:
            with self._pending_lock:
                current = self._pending_requests.get(key)
                if current is item:
                    item.pop("_approving", None)
            return {"success": False, "error": "Unable to persist the device approval; the request can be retried."}

        try:
            await websocket.send_json(
                build_message(
                    ProtocolTypes.PAIR_APPROVED,
                    {"device": record.to_dict(), "device_secret": secret},
                    request_id=str(item.get("request_id") or ""),
                )
            )
        except Exception:
            try:
                self.device_manager.remove(record.device_id)
            except Exception:
                try:
                    self.device_manager.revoke(record.device_id)
                except Exception:
                    pass
            with self._pending_lock:
                current = self._pending_requests.get(key)
                if current is item:
                    self._pending_requests.pop(key, None)
            return {"success": False, "error": "Device approval could not be delivered; no credentials were retained."}

        with self._pending_lock:
            current = self._pending_requests.get(key)
            if current is item:
                self._pending_requests.pop(key, None)
        self._append_log("PAIR_APPROVED", device=record.device_id, name=record.name, platform=record.platform)
        return {
            "success": True,
            "device": record.to_dict(),
            "message": "Device approved and credentials delivered directly to the paired device.",
        }

    def reject_pending_request(self, pending_id: str) -> bool:
        key = str(pending_id)
        with self._pending_lock:
            item = self._pending_requests.get(key)
            if item is None or item.get("_approving"):
                return False
            self._pending_requests.pop(key, None)
        websocket = item.get("websocket")
        if websocket is not None:
            try:
                task = asyncio.create_task(
                    websocket.send_json(
                        build_message(
                            ProtocolTypes.ERROR,
                            {"error": "Pairing request rejected by user."},
                            request_id=str(item.get("request_id") or ""),
                        )
                    )
                )
                task.add_done_callback(
                    lambda done: done.exception()
                    if not done.cancelled()
                    else None
                )
            except RuntimeError:
                pass
        self._append_log("PAIR_REJECTED", pending_id=key)
        return True

    async def route_command(self, target: str, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.command_router.route(target, action, parameters or {}, timeout=self.config.request_timeout_seconds)

    async def _pair_device(self, payload: dict[str, Any], websocket: WebSocket) -> dict[str, Any]:
        offer_token = str(payload.get("pairing_token") or "").strip()
        offer_code = str(payload.get("pairing_code") or "").strip()
        client_ip = str(getattr(getattr(websocket, "client", None), "host", "") or "").strip()
        now = asyncio.get_running_loop().time()
        if len(self._pair_attempts) > 4096:
            stale_ips = [
                ip for ip, (_attempts, blocked) in self._pair_attempts.items()
                if blocked <= now
            ]
            for ip in stale_ips:
                self._pair_attempts.pop(ip, None)
            if len(self._pair_attempts) > 4096:
                for ip, _value in sorted(
                    self._pair_attempts.items(), key=lambda item: item[1][1]
                )[: len(self._pair_attempts) - 4096]:
                    self._pair_attempts.pop(ip, None)
        attempts, blocked_until = self._pair_attempts.get(client_ip, (0, 0.0))
        if blocked_until > now:
            return {"success": False, "error": "Too many pairing attempts; try again shortly."}
        offer = (
            self.pairing_manager.get_offer(offer_token)
            if offer_token
            else self.pairing_manager.get_offer_by_code(offer_code)
        )
        if offer is None:
            attempts += 1
            self._pair_attempts[client_ip] = (
                (attempts, now + 60.0) if attempts >= 10 else (attempts, 0.0)
            )
            return {"success": False, "error": "Invalid or expired pairing token."}

        claimed_offer = self.pairing_manager.claim(offer.pairing_token)
        if claimed_offer is None:
            return {"success": False, "error": "Pairing offer is already being used."}
        self._pair_attempts.pop(client_ip, None)

        device_name = str(payload.get("device_name") or "Unknown Device").strip()
        platform = str(payload.get("platform") or "unknown").strip()
        os_version = str(payload.get("os_version") or "")
        agent_version = str(payload.get("agent_version") or "")
        battery = payload.get("battery")
        capabilities = list(payload.get("capabilities") or [])
        permissions = list(payload.get("permissions") or [])
        metadata = dict(payload.get("metadata") or {})
        try:
            record, secret = self.device_manager.create_from_pairing(
                name=device_name,
                platform=platform,
                os_version=os_version,
                agent_version=agent_version,
                ip=client_ip,
                battery=int(battery) if isinstance(battery, (int, float, str)) and str(battery).isdigit() else None,
                capabilities=capabilities,
                permissions=permissions,
                metadata=metadata,
            )
        except Exception:
            self.pairing_manager.release(claimed_offer.pairing_token)
            raise
        approved_offer = self.pairing_manager.approve(claimed_offer.pairing_token)
        if approved_offer is None:
            self.device_manager.remove(record.device_id)
            return {"success": False, "error": "Pairing offer was consumed concurrently; no credentials were retained."}
        self._append_log("PAIR_APPROVED", device=record.device_id, name=record.name, platform=record.platform)
        return {
            "success": True,
            "device": record.to_dict(),
            "device_secret": secret,
            "pairing_token": approved_offer.pairing_token,
        }

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.get("/health")
        async def health():
            return {"ok": True, "running": self.is_running(), "host": self.config.host, "port": self.config.port}

        def _local_management_allowed(req: Request) -> bool:
            host = str(getattr(getattr(req, "client", None), "host", "") or "").strip().lower()
            return host in {"127.0.0.1", "::1"} or host.startswith("::ffff:127.0.0.1")

        @app.get("/gateway/info")
        async def info():
            return {
                "ok": True,
                "service": "Brahma Connect",
                "host": self.config.host,
                "port": self.config.port,
                "advertise": self.config.advertise,
                "paired_devices": len(self.device_manager.list_devices()),
                "log_entries": len(self._log),
            }

        @app.get("/gateway/pair")
        async def get_pairing_offer(req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            return self.create_pairing_offer()

        @app.get("/gateway/devices")
        async def list_devices(req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            return {"ok": True, "devices": self.device_manager.list_devices()}

        @app.post("/gateway/devices/{device_id}/revoke")
        async def revoke_device(device_id: str, req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            if not self.device_manager.revoke(device_id):
                return JSONResponse({"ok": False, "error": "Device not found."}, status_code=404)
            await self.hub.close_device(device_id, reason="Device revoked")
            self._append_log("DEVICE_REVOKED", device_id=device_id)
            return {"ok": True}

        @app.post("/gateway/devices/{device_id}/forget")
        async def forget_device(device_id: str, req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            if not self.device_manager.remove(device_id):
                return JSONResponse({"ok": False, "error": "Device not found."}, status_code=404)
            await self.hub.close_device(device_id, reason="Device forgotten")
            self._append_log("DEVICE_FORGOTTEN", device_id=device_id)
            return {"ok": True}

        @app.get("/gateway/logs")
        async def logs(req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            return {"ok": True, "entries": self.log()}

        @app.get("/gateway/pending")
        async def pending_requests(req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            return {"ok": True, "requests": self.list_pending_requests()}

        @app.post("/gateway/pending/{pending_id}/approve")
        async def approve_pending(pending_id: str, req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            result = await self.approve_pending_request(pending_id)
            if not result.get("success"):
                return JSONResponse(result, status_code=404)
            return result

        @app.post("/gateway/pending/{pending_id}/reject")
        async def reject_pending(pending_id: str, req: Request):
            if not _local_management_allowed(req):
                return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)
            if not self.reject_pending_request(pending_id):
                return JSONResponse({"ok": False, "error": "Pending request not found."}, status_code=404)
            return {"ok": True}

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            state = await self.hub.attach(websocket)
            device_id = ""
            try:
                while True:
                    incoming = await websocket.receive_json()
                    valid, error = validate_message(incoming)
                    if not valid:
                        await websocket.send_json(build_message(ProtocolTypes.ERROR, {"error": error}))
                        continue
                    msg_type = str(incoming["type"]).strip().lower()
                    request_id = str(incoming["request_id"])
                    payload = dict(incoming.get("payload") or {})

                    if msg_type == ProtocolTypes.PING:
                        await websocket.send_json(build_message(ProtocolTypes.PONG, {"status": "ok"}, request_id=request_id))
                        continue

                    if msg_type == ProtocolTypes.HELLO:
                        # Replace any older pending request from this socket and
                        # bound the total pending set so unauthenticated HELLO spam
                        # cannot grow memory without limit.
                        with self._pending_lock:
                            stale_ids = [
                                pending_id
                                for pending_id, item in self._pending_requests.items()
                                if item.get("websocket") is websocket
                            ]
                            for stale_id in stale_ids:
                                self._pending_requests.pop(stale_id, None)
                            while len(self._pending_requests) >= 64:
                                oldest_id = min(
                                    self._pending_requests,
                                    key=lambda pending_id: self._pending_requests[pending_id].get("timestamp", ""),
                                )
                                self._pending_requests.pop(oldest_id, None)
                            pending_id = new_request_id()
                            pending_item = {
                                "request_id": pending_id,
                                "websocket": websocket,
                                "timestamp": now_iso(),
                                "device_name": str(payload.get("device_name") or "Unknown Device"),
                                "platform": str(payload.get("platform") or "unknown"),
                                "os_version": str(payload.get("os_version") or ""),
                                "agent_version": str(payload.get("agent_version") or ""),
                                "capabilities": list(payload.get("capabilities") or []),
                                "permissions": list(payload.get("permissions") or []),
                                "battery": payload.get("battery"),
                                "metadata": dict(payload.get("metadata") or {}),
                                "ip": websocket.client.host if websocket.client else "",
                            }
                            self._pending_requests[pending_id] = pending_item
                            pending_device_name = pending_item["device_name"]
                            pending_platform = pending_item["platform"]
                        await websocket.send_json(
                            build_message(
                                ProtocolTypes.PAIR_REQUEST,
                                {
                                    "pending_id": pending_id,
                                    "message": "Pairing request received. Awaiting user approval in Brahma.",
                                    "device_name": pending_device_name,
                                    "platform": pending_platform,
                                },
                                request_id=request_id,
                            )
                        )
                        continue

                    if msg_type == ProtocolTypes.AUTHENTICATE:
                        device_id = str(payload.get("device_id") or "").strip()
                        secret = str(payload.get("device_secret") or "").strip()
                        record = self.device_manager.authenticate(
                            device_id,
                            secret,
                            ip=websocket.client.host if websocket.client else "",
                        )
                        if record is None:
                            await websocket.send_json(build_message(ProtocolTypes.ERROR, {"error": "Authentication failed."}, request_id=request_id))
                            continue
                        await self.hub.register(websocket, record.device_id)
                        self.device_manager.touch(record.device_id, ip=websocket.client.host if websocket.client else "")
                        self._append_log("DEVICE_CONNECTED", device_id=record.device_id, name=record.name)
                        await websocket.send_json(build_message(ProtocolTypes.DEVICE_ONLINE, {"device": record.to_dict()}, request_id=request_id))
                        await websocket.send_json(build_message(ProtocolTypes.CAPABILITIES, {"device_id": record.device_id, "capabilities": record.capabilities}, request_id=request_id))
                        continue

                    if msg_type == ProtocolTypes.PAIR_REQUEST:
                        result = await self._pair_device(payload, websocket)
                        await websocket.send_json(build_message(ProtocolTypes.PAIR_APPROVED if result.get("success") else ProtocolTypes.ERROR, result, request_id=request_id))
                        continue

                    if msg_type == ProtocolTypes.RESULT:
                        if not device_id or not await self.hub.is_current(websocket, device_id):
                            await websocket.send_json(
                                build_message(
                                    ProtocolTypes.ERROR,
                                    {"error": "Authentication required for command results."},
                                    request_id=request_id,
                                )
                            )
                            continue
                        await self.hub.resolve_pending(device_id, request_id, payload)
                        continue

                    if msg_type == ProtocolTypes.ERROR:
                        if not device_id or not await self.hub.is_current(websocket, device_id):
                            await websocket.send_json(
                                build_message(
                                    ProtocolTypes.ERROR,
                                    {"error": "Authentication required for command errors."},
                                    request_id=request_id,
                                )
                            )
                            continue
                        await self.hub.reject_pending(device_id, request_id, str(payload.get("error") or "Unknown error"))
                        continue

                    if msg_type == ProtocolTypes.EVENT:
                        if not device_id or not await self.hub.is_current(websocket, device_id):
                            await websocket.send_json(
                                build_message(
                                    ProtocolTypes.ERROR,
                                    {"error": "Authentication required for device events."},
                                    request_id=request_id,
                                )
                            )
                            continue
                        self._append_log("EVENT", device_id=device_id, payload=payload)
                        continue

                    if msg_type == ProtocolTypes.CHAT_MESSAGE:
                        if not device_id or not await self.hub.is_current(websocket, device_id):
                            await websocket.send_json(
                                build_message(
                                    ProtocolTypes.ERROR,
                                    {"error": "Authentication required for chat messages."},
                                    request_id=request_id,
                                )
                            )
                            continue
                        if self.on_chat_message and payload.get("text"):
                            self.on_chat_message(payload.get("text"))
                        continue

                    if msg_type == ProtocolTypes.DEVICE_OFFLINE:
                        if not device_id or not await self.hub.is_current(websocket, device_id):
                            await websocket.send_json(
                                build_message(
                                    ProtocolTypes.ERROR,
                                    {"error": "Authentication required for device status changes."},
                                    request_id=request_id,
                                )
                            )
                            continue
                        self.device_manager.mark_offline(device_id)
                        self._append_log("DEVICE_DISCONNECTED", device_id=device_id)
                        continue

                    await websocket.send_json(build_message(ProtocolTypes.ERROR, {"error": f"Unsupported message type: {msg_type}."}, request_id=request_id))
            except WebSocketDisconnect:
                pass
            finally:
                with self._pending_lock:
                    for pending_id, item in list(self._pending_requests.items()):
                        if item.get("websocket") is websocket:
                            self._pending_requests.pop(pending_id, None)
                detached = await self.hub.unregister(websocket)
                if detached:
                    self.device_manager.mark_offline(detached)
                    self._append_log("DEVICE_DISCONNECTED", device_id=detached)

        return app

    async def serve(self) -> None:
        if not self.config.enabled:
            return
        # A gateway instance may be restarted after a clean stop.
        with self._serve_lock:
            if self._running:
                return
            if self._shutdown.is_set():
                return
            self._running = True
        try:
            advertised = False
            if self.config.advertise:
                advertised = self.discovery.start(
                    host=self.config.host,
                    port=self.config.port,
                    properties={
                        "service": "brahma",
                        "version": "1",
                        "tls": "1" if self.config.tls_enabled else "0",
                        "tls_certificate_sha256": self._tls_fingerprint,
                    },
                )
            self._append_log("GATEWAY_STARTING", host=self.config.host, port=self.config.port, advertised=advertised)
            cfg = uvicorn.Config(
                self.app,
                host=self.config.host,
                port=self.config.port,
                log_level="warning",
                log_config=None,
                access_log=False,
            )
            with self._serve_lock:
                self._server = uvicorn.Server(cfg)
                if self.config.tls_enabled:
                    cfg.ssl_keyfile = str(self._tls_keyfile)
                    cfg.ssl_certfile = str(self._tls_certfile)
                self._server.install_signal_handlers = lambda: None
                self._server.should_exit = self._shutdown.is_set()
            await self._server.serve()
        finally:
            with self._serve_lock:
                self._server = None
                self._running = False
            self.discovery.stop()
