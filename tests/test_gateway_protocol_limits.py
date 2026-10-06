from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gateway_websocket_has_application_message_size_limit():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert "2 * 1024 * 1024" in source
    assert 'code=1009' in source
    assert "receive_text()" in source


def test_command_router_has_explicit_unknown_action_guard():
    source = (ROOT / "brahma_connect" / "gateway" / "command_router.py").read_text(encoding="utf-8")
    assert "ACTION_UNSUPPORTED" in source
