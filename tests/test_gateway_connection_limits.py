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
    assert "handshake_deadline = asyncio.get_running_loop().time() + AUTH_HANDSHAKE_TIMEOUT_SECONDS" in source
    assert "timeout=remaining" in source
    assert 'websocket.close(code=1008, reason="Authentication handshake timed out")' in source
    assert "Authentication required before keepalive traffic." in source


def test_gateway_pairing_uses_configured_pairing_ttl_after_hello():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert "pairing_deadline: float | None = None" in source
    assert "deadline = pairing_deadline or handshake_deadline" in source
    assert "pairing_deadline = loop.time() + self.config.pairing_ttl_seconds" in source
