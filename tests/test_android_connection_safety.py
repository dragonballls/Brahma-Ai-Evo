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


def test_android_remote_device_action_is_not_advertised_or_dispatched():
    source = (
        ROOT
        / "brahma-connect-android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "brahma"
        / "connect"
        / "commands"
        / "DeviceCommandHandler.kt"
    ).read_text(encoding="utf-8")
    dispatch = source.split("return when (action.lowercase())", 1)[1].split("private fun getDeviceInfo", 1)[0]
    assert '"unlock_phone"' not in dispatch
    assert "unlockPhone(" not in source


def test_android_accessibility_bounds_ui_tree_and_gestures():
    source = (
        ROOT
        / "brahma-connect-android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "brahma"
        / "connect"
        / "accessibility"
        / "BrahmaAccessibilityService.kt"
    ).read_text(encoding="utf-8")
    assert "MAX_UI_NODES = 1000" in source
    assert "MAX_UI_DEPTH = 100" in source
    assert "UI tree exceeds safety limits" in source
    assert "fun withinScreen" in source
    assert "MAX_GESTURE_DURATION_MS = 10_000L" in source


def test_android_pairing_storage_fails_closed_and_verifies_writes():
    source = (
        ROOT
        / "brahma-connect-android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "brahma"
        / "connect"
        / "pairing"
        / "PairingStorage.kt"
    ).read_text(encoding="utf-8")
    assert 'throw IllegalStateException("Stored device credentials are corrupt; reconnect requires explicit repair.", exc)' in source
    assert ".commit()" in source
    assert "Failed to persist device credentials safely." in source
    assert "Failed to clear stored device credentials." in source


def test_android_chat_send_reports_queue_not_delivery():
    source = CLIENT.read_text(encoding="utf-8")
    assert 'Queued — awaiting gateway acknowledgement' in source
    assert 'val finalStatus = if (accepted) "Sent"' not in source


def test_android_pairing_hint_corruption_is_not_treated_as_absent():
    source = (
        ROOT
        / "brahma-connect-android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "brahma"
        / "connect"
        / "pairing"
        / "PairingStorage.kt"
    ).read_text(encoding="utf-8")
    assert "Stored gateway pairing hint is corrupt; refusing to treat it as absent." in source


def test_android_main_activity_surfaces_corrupt_persistent_state():
    source = (
        ROOT
        / "brahma-connect-android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "brahma"
        / "connect"
        / "MainActivity.kt"
    ).read_text(encoding="utf-8")
    assert "Stored credentials require repair" in source
    assert "Stored pairing hint requires repair" in source


def test_android_foreground_service_stops_reconnect_on_corrupt_persistent_state():
    source = (
        ROOT
        / "brahma-connect-android"
        / "app"
        / "src"
        / "main"
        / "java"
        / "com"
        / "brahma"
        / "connect"
        / "BrahmaConnectForegroundService.kt"
    ).read_text(encoding="utf-8")
    assert "Stored credentials require repair" in source
    assert "Stored pairing hint requires repair" in source
    assert "return" in source[source.index("private fun connectIfPossible"):source.index("private fun updateNotification")]
