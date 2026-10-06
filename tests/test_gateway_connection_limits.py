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
