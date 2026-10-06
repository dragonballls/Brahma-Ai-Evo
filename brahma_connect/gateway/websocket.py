from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from .models import DeviceRecord

SOCKET_SEND_TIMEOUT_SECONDS = 10.0
MAX_ACTIVE_CONNECTIONS = 128


@dataclass(slots=True)
class ConnectionState:
    websocket: WebSocket
    device_id: str = ""
    role: str = "agent"
    authenticated: bool = False
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConnectionHub:
    def __init__(self):
        self._connections: dict[str, ConnectionState] = {}
        self._socket_index: dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def attach(self, websocket: WebSocket, *, role: str = "agent") -> ConnectionState | None:
        state = ConnectionState(websocket=websocket, role=role)
        async with self._lock:
            if len(self._socket_index) >= MAX_ACTIVE_CONNECTIONS:
                return None
            key = id(websocket)
            self._socket_index[key] = ""
        return state

    async def register(self, websocket: WebSocket, device_id: str, *, role: str = "agent") -> ConnectionState:
        state = ConnectionState(websocket=websocket, device_id=device_id, role=role, authenticated=True)
        stale_pending: list[asyncio.Future] = []
        stale_socket: WebSocket | None = None
        async with self._lock:
            previous = self._connections.get(device_id)
            if previous is not None and previous.websocket is not websocket:
                stale_socket = previous.websocket
                stale_pending = list(previous.pending.values())
                previous.pending.clear()
                self._socket_index.pop(id(previous.websocket), None)
            self._connections[device_id] = state
            self._socket_index[id(websocket)] = device_id

        for future in stale_pending:
            if not future.done():
                future.set_exception(RuntimeError("Device connection was replaced."))
        if stale_socket is not None:
            try:
                await stale_socket.close(code=1000, reason="Replaced by newer device connection")
            except Exception:
                pass
        return state

    async def unregister(self, websocket: WebSocket) -> str:
        pending: list[asyncio.Future] = []
        async with self._lock:
            device_id = self._socket_index.pop(id(websocket), "")
            if device_id:
                state = self._connections.get(device_id)
                if state is not None and state.websocket is websocket:
                    self._connections.pop(device_id, None)
                    pending = list(state.pending.values())
                    state.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError("Device connection closed."))
        return device_id

    async def get(self, device_id: str) -> ConnectionState | None:
        async with self._lock:
            return self._connections.get(str(device_id))

    async def is_current(self, websocket: WebSocket, device_id: str) -> bool:
        async with self._lock:
            state = self._connections.get(str(device_id))
            return bool(
                state
                and state.authenticated
                and state.websocket is websocket
            )

    async def _drop_if_current(self, device_id: str, websocket: WebSocket, error: str) -> None:
        pending: list[asyncio.Future] = []
        async with self._lock:
            current = self._connections.get(str(device_id))
            if current is None or current.websocket is not websocket:
                return
            self._connections.pop(str(device_id), None)
            self._socket_index.pop(id(websocket), None)
            current.authenticated = False
            pending = list(current.pending.values())
            current.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError(error))

    async def send_to_device(self, device_id: str, message: dict[str, Any]) -> bool:
        # Hold only the connection-state lock for lookup. Socket I/O is serialized
        # per connection so slow sends cannot block registration, disconnects, or
        # other devices behind the global connection lock.
        async with self._lock:
            state = self._connections.get(str(device_id))
            if state is None or not state.authenticated:
                return False
        try:
            async with state.send_lock:
                await asyncio.wait_for(
                    state.websocket.send_json(message), timeout=SOCKET_SEND_TIMEOUT_SECONDS
                )
        except Exception:
            await self._drop_if_current(
                device_id,
                state.websocket,
                "Device connection failed during command delivery.",
            )
            return False
        return True

    async def broadcast_chat_message(self, message: dict[str, Any]) -> None:
        async with self._lock:
            states = list(self._connections.items())
        dead: list[tuple[str, int]] = []
        for device_id, state in states:
            try:
                async with state.send_lock:
                    await asyncio.wait_for(
                        state.websocket.send_json(message),
                        timeout=SOCKET_SEND_TIMEOUT_SECONDS,
                    )
            except Exception:
                dead.append((device_id, id(state.websocket)))

        if dead:
            async with self._lock:
                pending: list[asyncio.Future] = []
                for device_id, socket_id in dead:
                    current = self._connections.get(device_id)
                    if current is None or id(current.websocket) != socket_id:
                        continue
                    self._connections.pop(device_id, None)
                    self._socket_index.pop(socket_id, None)
                    pending.extend(current.pending.values())
                    current.pending.clear()
            for future in pending:
                if not future.done():
                    future.set_exception(RuntimeError("Device connection failed during broadcast."))

    async def set_pending(self, device_id: str, request_id: str) -> asyncio.Future | None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            state = self._connections.get(str(device_id))
            if state is None or not state.authenticated:
                return None
            future: asyncio.Future = loop.create_future()
            state.pending[request_id] = future
            return future

    async def resolve_pending(self, device_id: str, request_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            state = self._connections.get(device_id)
            if state is None:
                return
            future = state.pending.pop(request_id, None)
        if future and not future.done():
            future.set_result(payload)

    async def reject_pending(self, device_id: str, request_id: str, error: str) -> None:
        async with self._lock:
            state = self._connections.get(device_id)
            if state is None:
                return
            future = state.pending.pop(request_id, None)
        if future and not future.done():
            future.set_exception(RuntimeError(error))

    async def close_device(self, device_id: str, code: int = 1000, reason: str = "") -> bool:
        key = str(device_id)
        async with self._lock:
            state = self._connections.get(key)
            if state is None or not state.authenticated:
                return False

        try:
            await state.websocket.close(code=code, reason=reason)
        except Exception:
            # Keep the connection tracked and authenticated when the close
            # operation itself failed; callers can retry instead of losing the
            # lifecycle state while the socket may still be alive.
            return False

        pending: list[asyncio.Future] = []
        async with self._lock:
            current = self._connections.get(key)
            if current is state:
                self._connections.pop(key, None)
                state.authenticated = False
                self._socket_index.pop(id(state.websocket), None)
                pending = list(state.pending.values())
                state.pending.clear()

        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError(reason or "Device connection closed."))
        return True
