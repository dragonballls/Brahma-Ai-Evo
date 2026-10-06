from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "brahma-connect-android" / "app" / "src" / "main" / "java" / "com" / "brahma" / "connect" / "network" / "BrahmaWebSocketClient.kt"


def test_android_snapshot_uses_current_credential():
    source = CLIENT.read_text(encoding="utf-8")
    assert "deviceId = currentCredential?.deviceId" in source
    assert "deviceId = credential?.deviceId" not in source


def test_android_ignores_stale_websocket_messages_before_dispatch():
    source = CLIENT.read_text(encoding="utf-8")
    block = source[source.index("override fun onMessage(webSocket: WebSocket, text: String)"):source.index("override fun onMessage(webSocket: WebSocket, bytes", source.index("override fun onMessage(webSocket: WebSocket, text: String)"))]
    assert "if (socket !== webSocket) return" in block
    assert "handleCommandMessage(root)" in block


def test_android_rejects_malformed_pair_approval_credentials():
    source = CLIENT.read_text(encoding="utf-8")
    assert "Pair approval was malformed; device credentials were not saved." in source
    assert "if (deviceId.isBlank() || secret.isBlank())" in source
