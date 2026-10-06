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
