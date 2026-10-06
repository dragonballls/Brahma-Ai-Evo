import asyncio

from brahma_connect.gateway.capability_manager import CapabilityManager
from brahma_connect.gateway.command_router import ACTION_CAPABILITIES, CommandRouter


class FakeDevice:
    def __init__(self):
        self.device_id = "android_123"
        self.name = "Test Phone"
        self.revoked = False
        self.online = True
        self.capabilities = list({cap for caps in ACTION_CAPABILITIES.values() for cap in caps})

    def to_dict(self):
        return {"device_id": self.device_id, "name": self.name, "capabilities": self.capabilities}


class FakeDevices:
    def resolve(self, _target):
        return [FakeDevice()]


class FakeHub:
    pass


def test_unknown_remote_action_is_rejected_before_network_dispatch():
    router = CommandRouter(FakeDevices(), FakeHub(), CapabilityManager())
    result = asyncio.run(router.route("test", "not_a_real_action"))
    assert result["success"] is False
    assert result["error_code"] == "ACTION_UNSUPPORTED"

class ResultHub:
    def __init__(self, result):
        self.result = result
        self.future = None

    async def set_pending(self, _device_id, _request_id):
        self.future = asyncio.get_running_loop().create_future()
        return self.future

    async def send_to_device(self, _device_id, _message):
        self.future.set_result(self.result)
        return True

    async def reject_pending(self, _device_id, _request_id, _error):
        if self.future and not self.future.done():
            self.future.cancel()


def test_remote_command_requires_explicit_success_boolean():
    router = CommandRouter(FakeDevices(), ResultHub({"message": "finished"}), CapabilityManager())
    result = asyncio.run(router.route("test", "get_device_info"))
    assert result["success"] is False
    assert result["error_code"] == "MALFORMED_RESULT"


def test_remote_command_preserves_explicit_failure():
    router = CommandRouter(
        FakeDevices(),
        ResultHub({"success": False, "error": "Permission denied"}),
        CapabilityManager(),
    )
    result = asyncio.run(router.route("test", "get_device_info"))
    assert result["success"] is False
    assert result["error"] == "Permission denied"
