from __future__ import annotations

import asyncio
import concurrent.futures
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from brahma_connect.gateway.models import DeviceRecord
from brahma_connect.gateway.pairing import PairingManager
from brahma_connect.gateway.protocol import ProtocolTypes, build_message, validate_message
from brahma_connect.gateway.server import BrahmaGatewayConfig
from brahma_connect.gateway.websocket import ConnectionHub
from agent.task_queue import Task, TaskQueue, TaskStatus
import updater as updater_module


def test_device_public_serialization_excludes_internal_credentials():
    record = DeviceRecord(
        device_id="android_abc123",
        name="Phone",
        platform="android",
        secret_hash="sensitive-hash",
        connection_id="private-connection",
        identity_fingerprint="private-fingerprint",
    )
    public = record.to_dict()
    assert "secret_hash" not in public
    assert "connection_id" not in public
    assert "identity_fingerprint" not in public
    storage = record.to_storage_dict()
    assert storage["secret_hash"] == "sensitive-hash"


def test_persisted_online_state_is_reset_on_manager_restart(tmp_path: Path):
    from brahma_connect.gateway.device_manager import DeviceManager

    path = tmp_path / "devices.json"
    manager = DeviceManager(path)
    record, secret = manager.create_from_pairing(name="Phone", platform="android")
    manager.authenticate(record.device_id, secret, connection_id="live-1")

    restarted = DeviceManager(path)
    loaded = restarted.get(record.device_id)
    assert loaded is not None
    assert loaded.online is False
    assert loaded.connection_id == ""


def test_pairing_claim_is_single_use_under_concurrency():
    pairing = PairingManager(ttl_seconds=120)
    offer = pairing.create_offer("127.0.0.1", 8765)
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait()
        results.append(pairing.claim(offer.pairing_token) is not None)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 1


def test_gateway_config_recovers_invalid_typed_values(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "brahma_connect.json").write_text(
        '{"port":"not-a-port","enabled":"false","advertise":"off",'
        '"pairing_ttl_seconds":-1,"request_timeout_seconds":"bad"}',
        encoding="utf-8",
    )
    config = BrahmaGatewayConfig.load(tmp_path)
    assert config.port == 8765
    assert config.enabled is False
    assert config.advertise is False
    assert config.pairing_ttl_seconds == 300
    assert config.request_timeout_seconds == 30


def test_protocol_rejects_non_object_payload():
    message = build_message(ProtocolTypes.HELLO, {"ok": True})
    message["payload"] = ["invalid"]
    valid, error = validate_message(message)
    assert valid is False
    assert "payload" in error.lower()


def test_connection_hub_does_not_return_cancelled_future_when_unavailable():
    async def scenario():
        hub = ConnectionHub()
        return await hub.set_pending("missing", "request-1")

    assert asyncio.run(scenario()) is None


def test_command_router_cancellation_cleans_pending_future():
    from brahma_connect.gateway.command_router import CommandRouter

    class DummyDevice:
        device_id = "device-1"
        name = "Phone"
        platform = "android"
        revoked = False
        online = True
        capabilities = []
        permissions = []

        def to_dict(self):
            return {"device_id": self.device_id, "name": self.name}

    class DeviceManager:
        def resolve(self, _target):
            return [DummyDevice()]

    class Hub:
        def __init__(self):
            self.pending = asyncio.get_running_loop().create_future()
            self.rejected = False

        async def set_pending(self, _device_id, _request_id):
            return self.pending

        async def send_to_device(self, _device_id, _message):
            return True

        async def reject_pending(self, _device_id, _request_id, _error):
            self.rejected = True
            if not self.pending.done():
                self.pending.cancel()

    async def scenario():
        hub = Hub()
        router = CommandRouter(DeviceManager(), hub, type(
            "Caps", (), {"missing": staticmethod(lambda *_args: [])}
        )())
        task = asyncio.create_task(router.route("Phone", "ping", {}))
        await asyncio.sleep(0)
        task.cancel()
        with_value = False
        try:
            await task
        except asyncio.CancelledError:
            with_value = True
        assert with_value is True
        assert hub.rejected is True

    asyncio.run(scenario())


def test_service_gateway_timeout_cancels_submitted_future():
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = threading.RLock()
    service._loop = object()
    service.gateway = type("Gateway", (), {
        "config": type("Config", (), {"request_timeout_seconds": 1})(),
    })()

    class FakeFuture:
        cancelled = False

        def result(self, timeout):
            raise concurrent.futures.TimeoutError()

        def cancel(self):
            self.cancelled = True
            return True

    fake = FakeFuture()
    with patch("brahma_connect.service.asyncio.run_coroutine_threadsafe", return_value=fake):
        with pytest.raises(TimeoutError):
            service._run_on_gateway_loop(asyncio.sleep(0))
    assert fake.cancelled is True


def test_task_queue_cancelled_executor_failure_stays_cancelled():
    queue = TaskQueue()
    task = Task(
        priority=2,
        created_at=0.0,
        task_id="cancelled",
        goal="test",
        status=TaskStatus.RUNNING,
    )
    task.cancel_flag.set()

    class Executor:
        def execute(self, **_kwargs):
            raise RuntimeError("late cancellation")

    queue._executor = Executor()
    queue._active_count = 1
    queue._run_task(task)
    assert task.status is TaskStatus.CANCELLED
    assert queue._active_count == 0


def test_task_queue_terminal_history_is_bounded():
    queue = TaskQueue()
    queue._history_limit = 3
    for index in range(3):
        task = Task(priority=2, created_at=float(index), task_id=str(index), goal="done", status=TaskStatus.COMPLETED)
        queue._tasks[task.task_id] = task
    queue.submit("new")
    assert len(queue._tasks) <= 3


def test_updater_refuses_to_update_non_main_branch():
    fake_calls = []

    def fake_run(_base_dir, *args, **_kwargs):
        fake_calls.append(args)
        if args == ("branch", "--show-current"):
            return subprocess.CompletedProcess(["git"], 0, stdout="feature/test\n", stderr="")
        return subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")

    with patch.object(updater_module, "_run_git", side_effect=fake_run):
        assert updater_module.update_from_github(Path(".")) is False

    assert fake_calls == [("branch", "--show-current")]


def test_checkpoint_state_transitions_support_slotted_dataclass():
    from core.self_coding import Checkpoint

    checkpoint = Checkpoint(
        checkpoint_id="c",
        branch="agent/checkpoint/c",
        baseline="a" * 40,
        base_branch="main",
        commits=("b" * 40,),
        created_at="now",
        state="pending",
    )
    approved = replace(checkpoint, state="approved", promoted_sha="b" * 40)
    assert approved.state == "approved"
    assert approved.promoted_sha == "b" * 40

    source = Path("core/self_coding.py").read_text(encoding="utf-8")
    assert "checkpoint.__dict__" not in source


def test_gateway_logs_redact_credential_fields():
    from brahma_connect.gateway.server import BrahmaGateway, BrahmaGatewayConfig

    gateway = BrahmaGateway(
        Path("/tmp/brahma-audit-log-test"),
        BrahmaGatewayConfig(enabled=False, registry_path=Path("/tmp/brahma-audit-log-test/devices.json")),
    )
    gateway._append_log(
        "EVENT",
        pairing_code="123456",
        device_secret="device-secret",
        payload={"api_key": "sk-test", "nested": {"pin": "9876"}},
    )
    entry = gateway.log()[-1]
    assert entry["pairing_code"] == "[REDACTED]"
    assert entry["device_secret"] == "[REDACTED]"
    assert entry["payload"]["api_key"] == "[REDACTED]"
    assert entry["payload"]["nested"]["pin"] == "[REDACTED]"
