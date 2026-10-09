import asyncio

import pytest


def test_smart_home_service_rejects_provider_result_without_explicit_success():
    from smart_home.service import SmartHomeService

    class Storage:
        def get_device(self, device_id):
            return {
                "id": device_id,
                "name": "Test Device",
                "provider_key": "fake",
                "provider_account_id": "acct",
                "is_on": False,
                "traits": {},
            }

        def get_provider_account(self, _account_id):
            return {"credentials": {}}

        def update_device(self, *args, **kwargs):
            raise AssertionError("unverified provider result must never reach persistence")

        def log_activity(self, *args, **kwargs):
            pass

    class Provider:
        available = True
        coming_soon = False
        name = "Fake"

        def execute(self, device, action, payload):
            return {"is_on": True, "traits": {}, "detail": "request accepted"}

    service = SmartHomeService(storage=Storage())
    service._registry._providers = {"fake": Provider}
    result = service.execute_device_action("device-1", "power", {"is_on": True})
    assert result["success"] is False
    assert "authoritative success verification" in result["error"]


def test_kasa_command_does_not_claim_success_when_final_state_refresh_fails(monkeypatch):
    from smart_home.providers.builtin import KasaProvider

    class Device:
        is_on = False
        alias = "Test Kasa"
        model = "HS103"
        device_type = "plug"
        host = "192.0.2.10"
        mac = "aa:bb:cc:dd:ee:ff"

        async def turn_on(self):
            self.is_on = True

        async def turn_off(self):
            self.is_on = False

        async def update(self):
            raise RuntimeError("refresh failed")

    provider = KasaProvider()
    device = Device()

    async def connect(_device):
        return device

    monkeypatch.setattr(provider, "_connect_device", connect)

    with pytest.raises(RuntimeError, match="final device state could not be verified"):
        asyncio.run(
            provider._execute_async(
                {
                    "name": "Test Kasa",
                    "is_on": False,
                    "traits": {},
                    "provider_credentials": {},
                },
                "power",
                {"is_on": True},
            )
        )
