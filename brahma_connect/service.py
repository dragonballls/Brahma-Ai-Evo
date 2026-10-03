from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from .gateway.server import BrahmaGateway, BrahmaGatewayConfig


@dataclass(slots=True)
class BrahmaConnectService:
    """Small runtime wrapper around the gateway server."""

    base_dir: Path
    config: BrahmaGatewayConfig | None = None
    gateway: BrahmaGateway = field(init=False)
    _thread: threading.Thread | None = field(init=False, default=None)
    _started: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir).expanduser().resolve()
        self.gateway = BrahmaGateway(self.base_dir, self.config)
        self._lock = threading.RLock()

    @property
    def app(self):
        return self.gateway.app

    @property
    def on_chat_message(self):
        return self.gateway.on_chat_message

    @on_chat_message.setter
    def on_chat_message(self, cb):
        self.gateway.on_chat_message = cb

    _loop: asyncio.AbstractEventLoop | None = field(init=False, default=None)
    _lock: threading.RLock = field(init=False, repr=False)

    def broadcast_chat_message(self, event: dict):
        from .gateway.protocol import ProtocolTypes, build_message
        if not self._loop:
            return
        msg = build_message(ProtocolTypes.CHAT_MESSAGE, event)
        asyncio.run_coroutine_threadsafe(
            self.gateway.hub.broadcast_chat_message(msg),
            self._loop
        )

    def start_background(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            def _runner():
                loop = asyncio.new_event_loop()
                with self._lock:
                    self._loop = loop
                    self._started = True
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.gateway.serve())
                finally:
                    try:
                        loop.run_until_complete(asyncio.sleep(0))
                    except Exception:
                        pass
                    loop.close()
                    with self._lock:
                        self._loop = None
                        self._thread = None
                        self._started = False

            self._thread = threading.Thread(
                target=_runner,
                name="BrahmaConnectGateway",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self.gateway.request_shutdown()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
                self._loop = None
                self._started = False

    def is_running(self) -> bool:
        return self.gateway.is_running()

    def create_pairing_offer(self, *, device_name: str = "Unknown Device", platform: str = "unknown") -> dict[str, Any]:
        return self.gateway.create_pairing_offer(device_name=device_name, platform=platform)

    def list_devices(self) -> list[dict[str, Any]]:
        return self.gateway.device_manager.list_devices()

    def gateway_info(self) -> dict[str, Any]:
        return {
            "running": self.gateway.is_running(),
            "host": self.gateway.config.host,
            "port": self.gateway.config.port,
            "advertise": self.gateway.config.advertise,
            "devices": len(self.list_devices()),
        }

    def logs(self) -> list[dict[str, Any]]:
        return self.gateway.log()

    def get_device(self, target: str) -> dict[str, Any] | None:
        return self.gateway.get_device(target)

    def resolve_devices(self, query: str) -> list[dict[str, Any]]:
        return self.gateway.resolve_devices(query)

    def get_capabilities(self, target: str) -> dict[str, Any]:
        return self.gateway.get_capabilities(target)

    def rename_device(self, target: str, new_name: str) -> dict[str, Any]:
        return self.gateway.rename_device(target, new_name)

    def route_command(self, target: str, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return asyncio.run(self.gateway.route_command(target, action, parameters or {}))

    async def disconnect_device(self, target: str, *, reason: str = "Disconnected by Brahma") -> dict[str, Any]:
        return await self.gateway.disconnect_device(target, reason=reason)

    async def reconnect_device(self, target: str) -> dict[str, Any]:
        return await self.gateway.reconnect_device(target)

    async def reject_pending_request(self, pending_id: str) -> bool:
        return self.gateway.reject_pending_request(pending_id)

    def list_pending_requests(self) -> list[dict[str, Any]]:
        return self.gateway.list_pending_requests()

    async def approve_pending_request(self, pending_id: str) -> dict[str, Any]:
        return await self.gateway.approve_pending_request(pending_id)


_SERVICE: BrahmaConnectService | None = None
_SERVICE_LOCK = threading.RLock()


def get_service(base_dir: str | Path) -> BrahmaConnectService:
    global _SERVICE
    requested = Path(base_dir).expanduser().resolve()
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BrahmaConnectService(requested)
        elif _SERVICE.base_dir != requested:
            _SERVICE.stop()
            _SERVICE = BrahmaConnectService(requested)
        return _SERVICE
