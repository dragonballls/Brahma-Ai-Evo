import asyncio
from pathlib import Path

from brahma_connect.gateway.command_router import CommandRouter
from brahma_connect.gateway.device_manager import DeviceManager
from brahma_connect.gateway.capability_manager import CapabilityManager
from brahma_connect.gateway.models import DeviceRecord
from brahma_connect.gateway.pairing import PairingManager
from brahma_connect.gateway.protocol import ProtocolTypes, build_message, validate_message
from brahma_connect.gateway.websocket import ConnectionHub


def test_protocol_message_has_required_fields():
    message = build_message(ProtocolTypes.HELLO, {"hello": True})

    assert message["type"] == ProtocolTypes.HELLO
    assert message["request_id"]
    assert message["timestamp"]
    assert message["payload"] == {"hello": True}
    assert validate_message(message) == (True, "")


def test_device_manager_persists_and_authenticates(tmp_path: Path):
    registry_path = tmp_path / "devices.json"
    manager = DeviceManager(registry_path)

    record, secret = manager.create_from_pairing(
        name="Galaxy S24",
        platform="android",
        os_version="14",
        agent_version="1.0.0",
        ip="192.168.1.12",
        capabilities=["camera", "screen_capture"],
        permissions=["camera"],
    )

    assert record.device_id
    assert secret
    assert registry_path.exists()

    loaded = DeviceManager(registry_path)
    fetched = loaded.get(record.device_id)
    assert fetched is not None
    assert fetched.name == "Galaxy S24"

    authenticated = loaded.authenticate(record.device_id, secret, ip="192.168.1.13")
    assert authenticated is not None
    assert authenticated.online is True
    assert authenticated.ip == "192.168.1.13"


def test_pairing_manager_returns_expiring_offer():
    pairing = PairingManager(ttl_seconds=120)
    offer = pairing.create_offer("192.168.1.20", 8765)

    assert offer.service == "_BRAHMA._tcp.local."
    assert offer.host == "192.168.1.20"
    assert len(offer.pairing_code) == 6
    assert pairing.get_offer(offer.pairing_token) is not None
    assert pairing.get_offer_by_code(offer.pairing_code) is not None



def test_device_manager_skips_corrupt_registry_records(tmp_path: Path):
    registry = tmp_path / "devices.json"
    registry.write_text(
        '{"devices": {"good": {"device_id": "good", "name": "Good", "platform": "android"},'
        '"bad": {"device_id": "bad", "name": "Bad", "platform": "android", "capabilities": 42}}}',
        encoding="utf-8",
    )
    manager = DeviceManager(registry)
    assert manager.get("good") is not None
    assert manager.get("bad") is None


def test_capability_manager_accepts_single_required_capability():
    manager = CapabilityManager()
    assert manager.missing(["camera"], "camera") == []
    assert manager.missing([], "camera") == ["camera"]


def test_connection_hub_rejects_pending_commands_on_disconnect():
    class Socket:
        async def send_json(self, message):
            return None

    async def scenario():
        hub = ConnectionHub()
        socket = Socket()
        state = await hub.register(socket, "android_disconnect")
        future = await hub.set_pending("android_disconnect", "req-1")
        await hub.unregister(socket)
        return state, future

    state, future = asyncio.run(scenario())
    assert state.pending == {}
    assert future.done()
    try:
        future.result()
    except RuntimeError as exc:
        assert "connection closed" in str(exc).lower()
    else:
        raise AssertionError("Pending command future was not rejected")


def test_connection_hub_old_socket_cannot_unregister_new_connection():
    class Socket:
        def __init__(self, name):
            self.name = name
            self.closed = False

        async def send_json(self, message):
            return None

        async def close(self, code=1000, reason=""):
            self.closed = True

    async def scenario():
        hub = ConnectionHub()
        old_socket = Socket("old")
        new_socket = Socket("new")
        await hub.register(old_socket, "device-1")
        old_future = await hub.set_pending("device-1", "old-req")
        await hub.register(new_socket, "device-1")
        await hub.unregister(old_socket)
        current = await hub.get("device-1")
        return old_future, old_socket, current

    old_future, old_socket, current = asyncio.run(scenario())
    assert current is not None
    assert current.websocket.name == "new"
    assert old_socket.closed is True
    assert old_future.done()
    try:
        old_future.result()
    except RuntimeError as exc:
        assert "replaced" in str(exc).lower()
    else:
        raise AssertionError("Old pending future was not rejected")





def test_pairing_offer_is_single_use(tmp_path: Path):
    from brahma_connect.gateway.server import BrahmaGateway, BrahmaGatewayConfig

    class Socket:
        class Client:
            host = "192.168.1.50"
        client = Client()

    gateway = BrahmaGateway(
        tmp_path,
        BrahmaGatewayConfig(
            config_path=tmp_path / "config.json",
            registry_path=tmp_path / "devices.json",
        ),
    )
    offer = gateway.pairing_manager.create_offer("192.168.1.2", 8765)
    socket = Socket()

    async def scenario():
        first = await gateway._pair_device(
            {
                "pairing_token": offer.pairing_token,
                "device_name": "Phone",
                "platform": "android",
            },
            socket,
        )
        second = await gateway._pair_device(
            {
                "pairing_token": offer.pairing_token,
                "device_name": "Phone 2",
                "platform": "android",
            },
            socket,
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first["success"] is True
    assert second["success"] is False
    assert "invalid or expired" in second["error"].lower()


def test_gateway_management_routes_are_local_only():
    source = Path(__file__).resolve().parents[1] / "brahma_connect" / "gateway" / "server.py"
    text_value = source.read_text(encoding="utf-8")
    assert "def _local_management_allowed(req: Request)" in text_value
    assert text_value.count("if not _local_management_allowed(req):") >= 8
    assert 'return JSONResponse({"ok": False, "error": "Local management endpoint."}, status_code=403)' in text_value




def test_device_target_matching_avoids_substring_false_positives(tmp_path: Path):
    manager = DeviceManager(tmp_path / "devices.json")
    manager._devices["pc-1"] = DeviceRecord(
        device_id="pc-1",
        name="PC",
        platform="pc",
        online=True,
    )
    assert manager.resolve("space") == []
    assert [item.device_id for item in manager.resolve("PC")] == ["pc-1"]


def test_pairing_codes_are_unique_while_active(tmp_path: Path):
    pairing = PairingManager(ttl_seconds=120)
    first = pairing.create_offer("192.168.1.10", 8765)
    second = pairing.create_offer("192.168.1.10", 8765)
    assert first.pairing_code != second.pairing_code


def test_capability_normalization_tolerates_non_strings():
    manager = CapabilityManager()
    assert manager.normalize_many(["camera", 123, None]) == ["123", "camera"]




def test_connection_hub_broadcast_removes_dead_socket():
    class DeadSocket:
        async def send_json(self, message):
            raise RuntimeError("socket closed")

    async def scenario():
        hub = ConnectionHub()
        state = await hub.register(DeadSocket(), "dead-device")
        future = await hub.set_pending("dead-device", "req-1")
        await hub.broadcast_chat_message({"type": "CHAT"})
        current = await hub.get("dead-device")
        return state, future, current

    state, future, current = asyncio.run(scenario())
    assert state.pending == {}
    assert future.done()
    assert current is None
    try:
        future.result()
    except RuntimeError as exc:
        assert "broadcast" in str(exc).lower()
    else:
        raise AssertionError("Dead socket pending future was not rejected")


def test_service_async_operations_use_gateway_loop(tmp_path: Path):
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = __import__("threading").RLock()
    service._loop = asyncio.new_event_loop()
    service.gateway = type(
        "Gateway",
        (),
        {
            "disconnect_device": lambda self, target, reason="": asyncio.sleep(
                0, result={"success": True, "action": "disconnect", "device": target}
            ),
            "reconnect_device": lambda self, target: asyncio.sleep(
                0, result={"success": True, "action": "reconnect", "device": target}
            ),
            "approve_pending_request": lambda self, pending_id: asyncio.sleep(
                0, result={"success": True, "pending_id": pending_id}
            ),
        },
    )()

    def run_loop():
        asyncio.set_event_loop(service._loop)
        service._loop.run_forever()

    thread = __import__("threading").Thread(target=run_loop, daemon=True)
    thread.start()
    try:
        async def scenario():
            disconnected = await service.disconnect_device("phone")
            reconnected = await service.reconnect_device("phone")
            approved = await service.approve_pending_request("req-1")
            return disconnected, reconnected, approved

        disconnected, reconnected, approved = asyncio.run(scenario())
        assert disconnected["success"] is True
        assert reconnected["action"] == "reconnect"
        assert approved["pending_id"] == "req-1"
    finally:
        service._loop.call_soon_threadsafe(service._loop.stop)
        thread.join(timeout=2)
        service._loop.close()




def test_service_route_command_uses_gateway_loop(tmp_path: Path):
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = __import__("threading").RLock()
    service._loop = asyncio.new_event_loop()
    service.gateway = type(
        "Gateway",
        (),
        {"config": type("Config", (), {"request_timeout_seconds": 1})()},
    )()

    async def fake_route(target, action, parameters):
        return {
            "success": True,
            "device": target,
            "action": action,
            "parameters": parameters,
        }

    service.gateway.route_command = fake_route

    def run_loop():
        asyncio.set_event_loop(service._loop)
        service._loop.run_forever()

    thread = __import__("threading").Thread(target=run_loop, daemon=True)
    thread.start()
    try:
        result = service.route_command("phone", "ping", {"x": 1})
        assert result["success"] is True
        assert result["device"] == "phone"
        assert result["action"] == "ping"
    finally:
        service._loop.call_soon_threadsafe(service._loop.stop)
        thread.join(timeout=2)
        service._loop.close()




def test_command_router_cleans_pending_when_device_send_fails(tmp_path: Path):
    registry_path = tmp_path / "devices.json"
    manager = DeviceManager(registry_path)
    record = DeviceRecord(
        device_id="android_002",
        name="Galaxy S24",
        platform="android",
        online=True,
        capabilities=["launch_app"],
    )
    manager._devices[record.device_id] = record
    manager.save()

    class FailingHub:
        def __init__(self):
            self.rejected = []

        async def set_pending(self, device_id, request_id):
            return asyncio.get_running_loop().create_future()

        async def send_to_device(self, device_id, message):
            return False

        async def reject_pending(self, device_id, request_id, error):
            self.rejected.append((device_id, request_id, error))

    hub = FailingHub()
    router = CommandRouter(manager, hub, CapabilityManager())
    result = asyncio.run(
        router.route("Galaxy S24", "launch_app", {"package": "com.spotify.music"})
    )

    assert result["success"] is False
    assert result["error_code"] == "DEVICE_UNAVAILABLE"
    assert hub.rejected


def test_command_router_reports_offline_device(tmp_path: Path):
    registry_path = tmp_path / "devices.json"
    manager = DeviceManager(registry_path)
    record = DeviceRecord(
        device_id="android_001",
        name="Galaxy S24",
        platform="android",
        online=False,
        capabilities=["camera"],
    )
    manager._devices[record.device_id] = record
    manager.save()

    router = CommandRouter(manager, ConnectionHub(), CapabilityManager())
    result = asyncio.run(router.route("Galaxy S24", "launch_app", {"package": "com.spotify.music"}))

    assert result["success"] is False
    assert "offline" in result["error"].lower()

def test_gateway_rejects_unauthenticated_event_and_chat_paths():
    source = Path(__file__).resolve().parents[1] / "brahma_connect" / "gateway" / "server.py"
    text_value = source.read_text(encoding="utf-8")
    event_block = text_value.split("if msg_type == ProtocolTypes.EVENT:", 1)[1].split(
        'if msg_type == ProtocolTypes.CHAT_MESSAGE:', 1
    )[0]
    chat_block = text_value.split("if msg_type == ProtocolTypes.CHAT_MESSAGE:", 1)[1].split(
        'if msg_type == ProtocolTypes.DEVICE_OFFLINE:', 1
    )[0]
    offline_block = text_value.split("if msg_type == ProtocolTypes.DEVICE_OFFLINE:", 1)[1]
    assert "if not device_id:" in event_block
    assert "if not device_id:" in chat_block
    assert "if not device_id:" in offline_block
    assert 'Authentication required for chat messages.' in chat_block
    assert 'Authentication required for device events.' in event_block
    assert 'Authentication required for device status changes.' in offline_block

def test_command_router_enforces_canonical_sensitive_capabilities():
    from brahma_connect.gateway.command_router import ACTION_CAPABILITIES
    assert ACTION_CAPABILITIES["file_write"] == ("files",)
    assert ACTION_CAPABILITIES["file_delete"] == ("files",)
    assert ACTION_CAPABILITIES["ui_type"] == ("ui_control",)
    assert ACTION_CAPABILITIES["unlock_phone"] == ("unlock_phone",)


def test_android_remote_file_boundary_is_canonical_and_protected():
    source = (
        Path(__file__).resolve().parents[1]
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
    assert 'if (normalized.startsWith("/") || Regex("^[A-Za-z]:").containsMatchIn(normalized))' in source
    assert 'if (parts.any { it == ".." })' in source
    assert 'Deleting a storage root is not allowed.' in source
    assert '10 MB remote-read limit' in source

def test_connection_hub_set_pending_cancels_when_device_is_missing():
    async def scenario():
        hub = ConnectionHub()
        future = await hub.set_pending("missing-device", "req-1")
        return future

    future = asyncio.run(scenario())
    assert future.cancelled()


def test_connection_hub_current_socket_guard_rejects_replaced_connection():
    class Socket:
        pass

    async def scenario():
        hub = ConnectionHub()
        old_socket = Socket()
        new_socket = Socket()
        await hub.register(old_socket, "device-1")
        await hub.register(new_socket, "device-1")
        return await hub.is_current(old_socket, "device-1"), await hub.is_current(new_socket, "device-1")

    old_current, new_current = asyncio.run(scenario())
    assert old_current is False
    assert new_current is True
