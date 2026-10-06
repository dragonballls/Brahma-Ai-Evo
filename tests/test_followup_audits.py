from __future__ import annotations

import pytest

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


def test_corrupt_device_registry_is_quarantined_for_recovery(tmp_path: Path):
    from brahma_connect.gateway.device_manager import DeviceManager

    path = tmp_path / "devices.json"
    path.write_text("{not-json", encoding="utf-8")
    manager = DeviceManager(path)

    assert manager.list_devices() == []
    assert not path.exists()
    backups = list(tmp_path.glob("devices.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not-json"


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


def test_service_restart_prepares_gateway_start():
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = threading.RLock()
    service._thread = None
    calls = []

    class Gateway:
        def prepare_start(self):
            calls.append("prepared")

    service.gateway = Gateway()

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
        def is_alive(self):
            return False

    with patch("brahma_connect.service.threading.Thread", FakeThread):
        service.start_background()

    assert calls == ["prepared"]


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


def test_crucible_rejects_common_hardcoded_credentials():
    from core.skill_crucible import SkillCrucible, _sandbox_environment

    ok, message = SkillCrucible.validate_ast(
        "def execute(**kwargs):\n    return {'key': 'sk-abcdefghijklmnopqrstuvwxyz123456'}"
    )
    assert ok is False
    assert "credential" in message.lower()

    env = _sandbox_environment(Path("/tmp/brahma-crucible-test"))
    assert env["LOCALAPPDATA"].endswith("brahma-crucible-test")
    assert env["HOME"].endswith("brahma-crucible-test")
    assert all(
        not any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE"))
        for key in env
    )


def test_skill_and_runtime_promotions_are_atomic():
    skill_source = Path("core/skill_forge.py").read_text(encoding="utf-8")
    assert "staging_dir.replace(target_dir)" in skill_source
    assert "target_dir.mkdir" not in skill_source
    assert "committed and target_dir.exists()" in skill_source
    runtime_source = Path("scripts/prepare_omniroute_runtime.py").read_text(encoding="utf-8")
    assert "destination.replace(backup)" in runtime_source
    assert "promotion.replace(destination)" in runtime_source
    assert "shutil.rmtree(destination)" not in runtime_source


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


def test_service_falls_back_until_gateway_loop_is_running():
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = threading.RLock()
    service._loop = asyncio.new_event_loop()
    try:
        assert service._run_on_gateway_loop(asyncio.sleep(0)) is None
    finally:
        service._loop.close()


def test_gateway_shutdown_wins_over_startup_race(tmp_path: Path):
    from brahma_connect.gateway.server import BrahmaGateway

    gateway = BrahmaGateway(tmp_path)
    gateway.prepare_start()
    gateway.request_shutdown()
    asyncio.run(gateway.serve())
    assert gateway.is_running() is False


def test_connection_hub_close_invalidates_socket_and_pending_work():
    class Socket:
        async def close(self, **_kwargs):
            return None

    async def scenario():
        hub = ConnectionHub()
        socket = Socket()
        await hub.register(socket, "device-1")
        future = await hub.set_pending("device-1", "request-1")
        assert future is not None
        assert await hub.close_device("device-1", reason="revoked") is True
        return await hub.get("device-1"), future

    current, future = asyncio.run(scenario())
    assert current is None
    assert future.done()
    with pytest.raises(RuntimeError, match="revoked"):
        future.result()


def test_pending_pairing_cannot_be_rejected_during_approval(tmp_path: Path):
    from brahma_connect.gateway.server import BrahmaGateway

    gateway = BrahmaGateway(tmp_path)
    gateway._pending_requests["pending-1"] = {
        "_approving": True,
        "websocket": None,
    }
    assert gateway.reject_pending_request("pending-1") is False
    assert "pending-1" in gateway._pending_requests


def test_protocol_rejects_non_string_required_fields():
    message = build_message(ProtocolTypes.PING)
    message["type"] = {"bad": "type"}
    valid, error = validate_message(message)
    assert valid is False
    assert "string" in error.lower()

    with pytest.raises(TypeError):
        build_message(ProtocolTypes.PING, request_id=123)


def test_task_queue_stop_cancels_queued_work():
    queue = TaskQueue()
    task = Task(
        priority=2,
        created_at=0.0,
        task_id="queued-stop",
        goal="test",
        status=TaskStatus.PENDING,
    )
    queue._tasks[task.task_id] = task
    queue._queue.append(task)
    queue.stop()
    assert task.status is TaskStatus.CANCELLED
    assert queue._queue == []


def test_corrupt_gateway_device_registry_fails_closed(tmp_path: Path):
    from brahma_connect.gateway.device_manager import DeviceManager

    path = tmp_path / "devices.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupted"):
        DeviceManager(path)


def test_self_coding_undo_checks_branch_switch_result():
    source = Path("core/self_coding.py").read_text(encoding="utf-8")
    undo = source.split("def _undo_unlocked", 1)[1]
    assert 'switched = self._git("switch", "main")' in undo
    assert "Unable to switch to main for undo." in undo


def test_followup_audit_workflows_execute_module_tests_with_pytest():
    windows = Path(".github/workflows/windows-release.yml").read_text(encoding="utf-8")
    targeted = Path(".github/workflows/brahma-regression.yml").read_text(encoding="utf-8")
    assert "python -m pytest -q tests/test_followup_audits.py" in windows
    assert "python -m pytest -q tests/test_followup_audits.py" in targeted
    assert "tests.test_followup_audits" not in windows


def test_api_config_refuses_to_overwrite_corrupt_file(tmp_path: Path, monkeypatch):
    import config as config_module

    path = tmp_path / "api_keys.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config_module, "API_CONFIG_PATH", path)
    with pytest.raises(RuntimeError, match="corrupted"):
        config_module.save_config({"openrouter_api_key": "should-not-be-written"})
    assert path.read_text(encoding="utf-8") == "{broken"


def test_settings_refuse_to_overwrite_corrupt_file(tmp_path: Path, monkeypatch):
    from memory import config_manager

    path = tmp_path / "app_settings.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config_manager, "SETTINGS_FILE", path)
    with pytest.raises(RuntimeError, match="corrupted"):
        config_manager.save_settings({"wake_word_enabled": False})
    assert path.read_text(encoding="utf-8") == "{broken"


def test_local_json_error_does_not_echo_raw_model_output():
    source = Path("or_client.py").read_text(encoding="utf-8")
    assert "Raw output:" not in source
    assert "Local model returned unparseable JSON." in source


def test_pair_approval_tool_result_never_contains_device_secret():
    source = Path("brahma_connect/gateway/server.py").read_text(encoding="utf-8")
    approval = source.split("async def approve_pending_request", 1)[1]
    assert '"device_secret": secret' not in approval
    assert "credentials delivered directly to the paired device" in approval
