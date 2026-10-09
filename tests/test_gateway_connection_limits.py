from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gateway_connection_hub_has_global_active_connection_cap():
    source = (ROOT / "brahma_connect" / "gateway" / "websocket.py").read_text(encoding="utf-8")
    assert "MAX_ACTIVE_CONNECTIONS = 128" in source
    assert "len(self._socket_index) >= MAX_ACTIVE_CONNECTIONS" in source


def test_gateway_rejects_capacity_exhaustion_without_entering_protocol_loop():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert 'await websocket.close(code=1013, reason="Gateway connection capacity reached")' in source
    assert "if state is None:" in source


def test_gateway_unauthenticated_connections_have_a_cumulative_handshake_deadline():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert "AUTH_HANDSHAKE_TIMEOUT_SECONDS = 30.0" in source
    assert "handshake_deadline =" in source
    assert "loop.time() + AUTH_HANDSHAKE_TIMEOUT_SECONDS" in source
    assert "timeout=remaining" in source
    assert 'websocket.close(code=1008, reason="Authentication handshake timed out")' in source
    assert "Authentication required before keepalive traffic." in source


def test_gateway_pairing_uses_configured_pairing_ttl_after_hello():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert "pairing_deadline: float | None = None" in source
    assert "deadline = pairing_deadline or handshake_deadline" in source
    assert "pairing_deadline = loop.time() + self.config.pairing_ttl_seconds" in source


class _FakeWebSocket:
    async def close(self, **kwargs):
        return None


def test_gateway_same_socket_reregistration_cleans_stale_device_state():
    import asyncio

    from brahma_connect.gateway.websocket import ConnectionHub

    async def scenario():
        hub = ConnectionHub()
        websocket = _FakeWebSocket()
        assert await hub.attach(websocket) is not None
        await hub.register(websocket, "device-a")
        pending = await hub.set_pending("device-a", "request-a")
        assert pending is not None

        await hub.register(websocket, "device-b")

        assert await hub.get("device-a") is None
        assert (await hub.get("device-b")).websocket is websocket
        try:
            await pending
        except RuntimeError as exc:
            assert "replaced" in str(exc).lower()
        else:
            raise AssertionError("Stale pending request was not rejected.")

        assert await hub.unregister(websocket) == "device-b"
        assert await hub.get("device-a") is None
        assert await hub.get("device-b") is None

    asyncio.run(scenario())


def test_connection_hub_close_all_settles_every_pending_future():
    import asyncio
    from brahma_connect.gateway.websocket import ConnectionHub

    class Socket:
        async def close(self, **kwargs):
            return None

    async def scenario():
        hub = ConnectionHub()
        socket = Socket()
        await hub.attach(socket)
        await hub.register(socket, "device-shutdown")
        future = await hub.set_pending("device-shutdown", "req-shutdown")
        assert future is not None
        closed = await hub.close_all()
        return closed, future, await hub.get("device-shutdown")

    closed, future, current = asyncio.run(scenario())
    assert closed == ["device-shutdown"]
    assert current is None
    assert future.done()
    try:
        future.result()
    except RuntimeError as exc:
        assert "shutting down" in str(exc).lower()
    else:
        raise AssertionError("Pending future was not rejected during shutdown.")


def test_gateway_bounds_unauthenticated_payload_fields_and_log_size():
    from brahma_connect.gateway.server import (
        _MAX_CHAT_TEXT,
        _MAX_LOG_PAYLOAD_BYTES,
        _bounded_metadata,
        _bounded_string,
        _bounded_string_list,
    )
    import pytest

    with pytest.raises(ValueError):
        _bounded_string("x" * 257, "device_name")
    with pytest.raises(ValueError):
        _bounded_string_list(["x"] * 65, "capabilities")
    with pytest.raises(ValueError):
        _bounded_metadata({"large": "x" * (_MAX_LOG_PAYLOAD_BYTES)})
    assert _MAX_CHAT_TEXT == 16 * 1024


def test_gateway_rejects_reauthentication_on_an_already_authenticated_socket():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert "This WebSocket is already authenticated; reconnect to change devices." in source
    assert "if device_id:" in source


def test_connection_hub_invalidate_device_revokes_before_socket_close():
    import asyncio
    from brahma_connect.gateway.websocket import ConnectionHub

    class Socket:
        def __init__(self):
            self.closed = False
            self.close_calls = 0

        async def close(self, **kwargs):
            self.close_calls += 1
            self.closed = True
            raise RuntimeError("socket close failed")

    async def scenario():
        hub = ConnectionHub()
        socket = Socket()
        await hub.register(socket, "dev-1")
        result = await hub.invalidate_device("dev-1")
        assert result == {"found": True, "closed": False}
        assert await hub.get("dev-1") is None
        assert socket.close_calls == 1

    asyncio.run(scenario())


def test_pairing_deadline_starts_only_after_acceptance_and_never_resets():
    from brahma_connect.gateway.server import _start_pairing_deadline

    assert _start_pairing_deadline(
        None, now=10.0, ttl_seconds=300, accepted=False
    ) is None

    deadline = _start_pairing_deadline(
        None, now=10.0, ttl_seconds=300, accepted=True
    )
    assert deadline == 310.0
    assert _start_pairing_deadline(
        deadline, now=1000.0, ttl_seconds=300, accepted=True
    ) == deadline
    assert _start_pairing_deadline(
        deadline, now=1000.0, ttl_seconds=300, accepted=False
    ) == deadline


def test_gateway_starts_pairing_deadline_after_hello_validation_and_pair_result():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    hello_start = source.index("if msg_type == ProtocolTypes.HELLO:")
    auth_start = source.index("if msg_type == ProtocolTypes.AUTHENTICATE:", hello_start)
    hello_block = source[hello_start:auth_start]
    assert hello_block.index("hello_metadata = _bounded_metadata") < hello_block.index(
        "pairing_deadline = _start_pairing_deadline("
    )
    assert hello_block.index("except ValueError as exc:") < hello_block.index(
        "pairing_deadline = _start_pairing_deadline("
    )

    pair_start = source.index("if msg_type == ProtocolTypes.PAIR_REQUEST:")
    result_start = source.index("if msg_type == ProtocolTypes.RESULT:", pair_start)
    pair_block = source[pair_start:result_start]
    assert pair_block.index("result = await self._pair_device(payload, websocket)") < pair_block.index(
        "pairing_deadline = _start_pairing_deadline("
    )
    assert "accepted=bool(result.get(\"success\"))" in pair_block
