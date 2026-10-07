import re
from pathlib import Path

from brahma_connect.gateway.command_router import ACTION_CAPABILITIES


ROOT = Path(__file__).resolve().parents[1]
ANDROID_HANDLER = ROOT / "brahma-connect-android" / "app" / "src" / "main" / "java" / "com" / "brahma" / "connect" / "commands" / "DeviceCommandHandler.kt"
ANDROID_STATE = ROOT / "brahma-connect-android" / "app" / "src" / "main" / "java" / "com" / "brahma" / "connect" / "core" / "AgentStateStore.kt"


def test_android_remote_actions_are_capability_gated():
    handler = ANDROID_HANDLER.read_text(encoding="utf-8")
    dispatch = handler.split("return when (action.lowercase())", 1)[1].split("private fun getDeviceInfo", 1)[0]
    supported = set(re.findall(r'^\s*"([^"]+)"\s*->', dispatch, flags=re.MULTILINE))
    assert supported
    assert supported <= set(ACTION_CAPABILITIES)


def test_android_initial_capabilities_cover_gateway_requirements():
    capability_source = ANDROID_STATE.read_text(encoding="utf-8")
    initial_block = re.search(r'val INITIAL: List<String> = listOf\((.*?)\n    \)', capability_source, re.DOTALL)
    assert initial_block
    constants = set(re.findall(r'\b([A-Z][A-Z0-9_]*)\b', initial_block.group(1)))
    definitions = dict(re.findall(r'const val ([A-Z][A-Z0-9_]*) = "([^"]+)"', capability_source))
    initial_values = {definitions[name] for name in constants if name in definitions}

    gateway_capabilities = {cap for values in ACTION_CAPABILITIES.values() for cap in values}
    assert gateway_capabilities <= initial_values


def test_android_websocket_reconnect_state_has_visibility_and_stale_socket_guard():
    source = (ROOT / "brahma-connect-android" / "app" / "src" / "main" / "java" / "com" / "brahma" / "connect" / "network" / "BrahmaWebSocketClient.kt").read_text(encoding="utf-8")
    assert source.count("@Volatile private var") >= 5
    reconnect_block = source[source.index("val previousSocket = socket"):source.index("BrahmaSocketListener()", source.index("val previousSocket = socket"))]
    assert "socket = null" in reconnect_block
    assert reconnect_block.index("socket = null") < reconnect_block.index('previousSocket?.close')


def test_android_send_paths_check_websocket_send_result():
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
        / "network"
        / "BrahmaWebSocketClient.kt"
    ).read_text(encoding="utf-8")
    assert 'if (send(BrahmaProtocol.hello(buildSnapshot())))' in source
    assert 'if (send(BrahmaProtocol.envelope(BrahmaProtocol.PAIR_REQUEST, snapshot)))' in source
    assert 'if (send(BrahmaProtocol.authenticate(credential)))' in source
    assert 'if (!send(response))' in source
    assert 'if (!send(BrahmaProtocol.envelope(BrahmaProtocol.PONG' in source
    assert 'private fun reportSendFailure(message: String)' in source


def test_android_reconnect_timers_are_daemon_and_generation_guarded():
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
        / "network"
        / "BrahmaWebSocketClient.kt"
    ).read_text(encoding="utf-8")
    assert "@Volatile private var connectionGeneration = 0L" in source
    assert "val generation = connectionGeneration" in source
    assert "connectionGeneration == generation" in source
    assert "isDaemon = true" in source
