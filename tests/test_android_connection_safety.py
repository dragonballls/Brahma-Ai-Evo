from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID_HANDLER = (
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
)


def test_android_remote_unlock_is_not_advertised_or_dispatched():
    handler = ANDROID_HANDLER.read_text(encoding="utf-8")
    dispatch = handler.split("return when (action.lowercase())", 1)[1].split("private fun getDeviceInfo", 1)[0]
    assert '"unlock_phone"' not in dispatch
    assert "unlockPhone(" not in handler


def test_android_handler_exposes_only_gateway_capabilities():
    from brahma_connect.gateway.command_router import ACTION_CAPABILITIES

    handler = ANDROID_HANDLER.read_text(encoding="utf-8")
    dispatch = handler.split("return when (action.lowercase())", 1)[1].split("private fun getDeviceInfo", 1)[0]
    supported = set(__import__("re").findall(r'^\s*"([^"]+)"\s*->', dispatch, flags=__import__("re").MULTILINE))
    assert supported <= set(ACTION_CAPABILITIES)
