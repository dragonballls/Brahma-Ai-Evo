from __future__ import annotations

import pytest

import asyncio
import concurrent.futures
import subprocess
import threading
import time
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
        task = asyncio.create_task(router.route("Phone", "get_battery", {}))
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


def test_service_sync_gateway_api_closes_coroutine_inside_running_loop():
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = threading.RLock()
    service._loop = None
    service.gateway = type("Gateway", (), {
        "config": type("Config", (), {"request_timeout_seconds": 1})(),
    })()

    async def scenario():
        coroutine = asyncio.sleep(0)
        with pytest.raises(
            RuntimeError,
            match="Synchronous gateway operation cannot run inside an active event loop",
        ):
            service._run_on_gateway_loop(coroutine)
        assert coroutine.cr_frame is None or coroutine.cr_running is False

    asyncio.run(scenario())


def test_service_gateway_timeout_cancels_submitted_future():
    from brahma_connect.service import BrahmaConnectService

    service = object.__new__(BrahmaConnectService)
    service._lock = threading.RLock()
    class LoopState:
        def is_closed(self):
            return False
        def is_running(self):
            return True
    service._loop = LoopState()
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
            service._run_on_gateway_loop(object())
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


def test_corrupt_gateway_device_registry_is_quarantined(tmp_path):
    from brahma_connect.gateway.device_manager import DeviceManager

    path = tmp_path / "corrupt-device-registry-test.json"
    path.write_text("{broken", encoding="utf-8")
    try:
        manager = DeviceManager(path)
        assert manager.list_devices() == []
        assert not path.exists()
        assert list(path.parent.glob(path.name + ".corrupt-*"))
    finally:
        for candidate in path.parent.glob(path.name + ".corrupt-*"):
            candidate.unlink(missing_ok=True)



def test_self_coding_undo_checks_branch_switch_result():
    source = Path("core/self_coding.py").read_text(encoding="utf-8")
    undo = source.split("def _undo_unlocked", 1)[1]
    assert 'switched = self._git("switch", "main")' in undo
    assert "Unable to switch to main for undo." in undo


def test_followup_audit_workflows_execute_module_tests_with_pytest():
    windows = Path(".github/workflows/windows-release.yml").read_text(encoding="utf-8")
    targeted = Path(".github/workflows/brahma-regression.yml").read_text(encoding="utf-8")
    assert "python -m pytest -q tests/test_followup_audits.py" in windows
    assert "python -m pytest -q" in targeted
    assert "tests/test_followup_audits.py" in targeted
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
    from brahma_connect.gateway.server import BrahmaGateway

    class Record:
        device_id = "device-1"
        name = "Phone"
        platform = "android"
        def to_dict(self):
            return {"device_id": self.device_id, "name": self.name}

    class Manager:
        def create_from_pairing(self, **_kwargs):
            return Record(), "device-secret"

        def remove(self, _device_id):
            return True

    class WebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def scenario():
        gateway = object.__new__(BrahmaGateway)
        gateway._pending_lock = threading.RLock()
        gateway._pending_requests = {
            "pending-1": {
                "request_id": "request-1",
                "websocket": WebSocket(),
                "device_name": "Phone",
                "platform": "android",
            }
        }
        gateway.device_manager = Manager()
        gateway._append_log = lambda *_args, **_kwargs: None
        result = await gateway.approve_pending_request("pending-1")
        item = gateway._pending_requests
        assert "device_secret" not in result
        assert result["message"] == "Device approved and credentials sent to the paired connection; receipt was not independently verified."
        assert item == {}

    asyncio.run(scenario())




def test_device_record_normalizes_string_booleans():
    record = DeviceRecord.from_dict({
        "device_id": "d",
        "name": "Phone",
        "platform": "android",
        "online": "false",
        "revoked": "off",
    })
    assert record.online is False
    assert record.revoked is False

    enabled = DeviceRecord.from_dict({
        "device_id": "d2",
        "name": "Phone",
        "platform": "android",
        "online": "true",
        "revoked": "1",
    })
    assert enabled.online is True
    assert enabled.revoked is True


def test_device_action_wrappers_do_not_nest_asyncio_run():
    source = Path("actions/brahma_connect.py").read_text(encoding="utf-8")
    assert "asyncio.run(service.disconnect_device" not in source
    assert "asyncio.run(service.approve_pending_request" not in source


def test_ota_rejects_mismatched_supplied_checksum(monkeypatch, tmp_path: Path):
    import core.updater_ota as ota

    release = {"assets": [{"name": "BrahmaEvo_Setup.exe", "browser_download_url": "https://example.test/setup.exe"}]}
    monkeypatch.setattr(ota, "_get_release", lambda: release)
    monkeypatch.setattr(
        ota,
        "_release_asset",
        lambda _release, _url: release["assets"][0],
    )
    monkeypatch.setattr(ota, "_asset_digest", lambda _release, _asset: "a" * 64)

    calls = []

    def unexpected_download(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("network download must not start after checksum mismatch")

    monkeypatch.setattr(ota, "download_public_to_file", unexpected_download)
    result = ota.download_and_apply_update(
        "https://example.test/setup.exe",
        expected_sha256="b" * 64,
    )
    assert result is False
    assert calls == []


def test_dynamic_registry_redacts_credential_like_errors():
    from core.dynamic_registry import _redact_text
    assert _redact_text("failed bearer sk-proj-abcdefghijklmnopqrstuvwxyz123456") == "failed bearer [REDACTED]"


def test_gateway_pair_attempt_tracking_is_bounded():
    from types import SimpleNamespace
    from brahma_connect.gateway.server import BrahmaGateway

    gateway = BrahmaGateway(Path("."))
    gateway._pair_attempts = {
        f"192.0.2.{i}": (1, 0.0) for i in range(4105)
    }
    websocket = SimpleNamespace(client=SimpleNamespace(host="198.51.100.10"))
    result = asyncio.run(gateway._pair_device({"pairing_token": "invalid"}, websocket))
    assert result["success"] is False
    assert len(gateway._pair_attempts) <= 4096


def test_dashboard_is_https_first():
    source = Path("dashboard/server.py").read_text(encoding="utf-8")
    assert 'return f"https://{self._ip}:{PORT}"' in source
    assert 'ssl_keyfile=str(ssl_key)' in source
    assert 'ssl_certfile=str(ssl_cert)' in source
    assert "Dashboard HTTPS certificate could not be created." in source
    assert "serialization.load_pem_private_key" in source
    assert "x509.load_pem_x509_certificate" in source
    assert "os.replace(key_tmp, key_path)" in source
    assert "os.replace(cert_tmp, cert_path)" in source


def test_gateway_tls_is_enabled_and_pinned_in_pairing_offers():
    from brahma_connect.gateway.models import PairingOffer
    from brahma_connect.gateway.server import BrahmaGatewayConfig

    config = BrahmaGatewayConfig()
    assert config.tls_enabled is True
    offer = PairingOffer(
        service="_BRAHMA._tcp.local.",
        host="192.168.1.20",
        port=8765,
        pairing_token="t",
        pairing_code="123456",
        expires_at=1.0,
        tls_enabled=True,
        tls_certificate_sha256="a" * 64,
    )
    data = offer.to_dict()
    assert data["tls"] is True
    assert data["tls_certificate_sha256"] == "a" * 64


def test_android_gateway_requires_tls_and_uses_wss_with_pinning():
    source = Path("brahma-connect-android/app/src/main/java/com/brahma/connect/network/BrahmaWebSocketClient.kt").read_text(encoding="utf-8")
    manifest = Path("brahma-connect-android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert 'require(endpoint.tls) { "Brahma Connect requires TLS for gateway connections." }' in source
    assert 'AgentStateStore.setError("Brahma Connect requires a TLS-secured gateway.")' in source
    assert 'url("wss://" + endpoint.host + ":" + endpoint.port + "/ws")' in source
    assert 'endpoint.tlsCertificateSha256.lowercase().trim()' in source
    assert "Gateway TLS certificate fingerprint does not match the pairing record." in source
    assert 'android:usesCleartextTraffic="false"' in manifest


def test_dashboard_https_and_gateway_tls_helpers(tmp_path: Path):
    from core.local_tls import certificate_sha256
    from brahma_connect.gateway.server import BrahmaGateway, BrahmaGatewayConfig

    dashboard_source = Path("dashboard/server.py").read_text(encoding="utf-8")
    assert 'return f"https://{self._ip}:{PORT}"' in dashboard_source

    gateway = BrahmaGateway(tmp_path, BrahmaGatewayConfig(registry_path=tmp_path / "devices.json"))
    gateway._ensure_tls()
    assert gateway._tls_certfile is not None
    assert gateway._tls_keyfile is not None
    assert len(gateway._tls_fingerprint) == 64
    assert gateway._tls_fingerprint == certificate_sha256(gateway._tls_certfile)


def test_gateway_config_refuses_corrupt_json(tmp_path: Path):
    path = tmp_path / "config" / "brahma_connect.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupted"):
        BrahmaGatewayConfig.load(tmp_path)


def test_windows_api_keys_are_protected_at_rest_or_plaintext_on_non_windows(tmp_path: Path, monkeypatch):
    import sys
    import config as config_module
    if sys.platform != "win32":
        pytest.skip("Windows DPAPI is only available on Windows CI.")

    monkeypatch.setattr(config_module, "API_CONFIG_PATH", tmp_path / "api_keys.json")
    monkeypatch.setattr(config_module.platform, "system", lambda: "Windows")
    config_module.save_config({"openrouter_api_key": "example-test-key"})
    raw = (tmp_path / "api_keys.json").read_text(encoding="utf-8")
    assert "example-test-key" not in raw
    loaded = config_module.get_api_key("OpenRouter")
    assert loaded == "example-test-key"


def test_windows_api_key_protection_falls_back_on_non_bytes_pywin32(monkeypatch, tmp_path: Path):
    import sys
    import types
    import config as config_module

    monkeypatch.setattr(config_module.platform, "system", lambda: "Windows")
    monkeypatch.setitem(
        sys.modules,
        "win32crypt",
        types.SimpleNamespace(
            CryptProtectData=lambda *_args, **_kwargs: ("description", 0),
            CryptUnprotectData=lambda *_args, **_kwargs: ("description", 0),
        ),
    )
    monkeypatch.setattr(config_module, "_dpapi_protect", lambda _text: b"native-ciphertext")
    monkeypatch.setattr(config_module, "_dpapi_unprotect", lambda _blob: "example-test-key")
    monkeypatch.setattr(config_module, "API_CONFIG_PATH", tmp_path / "api_keys.json")

    config_module.save_config({"openrouter_api_key": "example-test-key"})
    raw = (tmp_path / "api_keys.json").read_text(encoding="utf-8")
    assert "example-test-key" not in raw
    assert config_module.get_api_key("OpenRouter") == "example-test-key"

def test_android_stored_credentials_carry_tls_pin():
    source = Path("brahma-connect-android/app/src/main/java/com/brahma/connect/pairing/PairingStorage.kt").read_text(encoding="utf-8")
    assert 'put("tls_certificate_sha256", credential.tlsCertificateSha256)' in source
    assert 'tlsCertificateSha256 = json.optString("tls_certificate_sha256")' in source


def test_crucible_sandbox_confines_execution_to_temporary_root():
    source = Path("core/skill_crucible.py").read_text(encoding="utf-8")
    assert 'env["BRAHMA_CRUCIBLE_ROOT"] = str(root)' in source
    assert 'cwd=str(sandbox_root)' in source
    assert "_sandbox_path(value)" in source
    assert "Crucible sandbox denied filesystem access outside its temporary root." in source


def test_crucible_dependency_auto_install_is_runtime_allowlisted():
    from core.skill_crucible import _approved_auto_install_packages, _normalize_distribution_name
    approved = _approved_auto_install_packages()
    assert _normalize_distribution_name("requests") in approved
    assert _normalize_distribution_name("definitely-not-a-brahma-package") not in approved


def test_crucible_rejects_unapproved_dependency_before_import_probe(monkeypatch):
    import core.skill_crucible as crucible

    def unexpected_import_probe(*_args, **_kwargs):
        raise AssertionError("Unapproved dependencies must be rejected before import execution.")

    monkeypatch.setattr(crucible.subprocess, "run", unexpected_import_probe)
    ok, message = crucible.SkillCrucible.resolve_dependencies(["definitely_not_approved"])
    assert ok is False
    assert "not an approved Brahma runtime package" in message


def test_crucible_dependency_import_probe_uses_safe_path_and_sandbox_cwd():
    source = Path("core/skill_crucible.py").read_text(encoding="utf-8")
    assert 'check_cmd = [py_exe, "-P", "-c", check_script]' in source
    assert 'with tempfile.TemporaryDirectory(prefix=".crucible-import-")' in source
    assert 'env=_sandbox_environment(Path(import_root))' in source
    assert "cwd=import_root" in source


def test_crucible_blocks_filesystem_enumeration_outside_sandbox():
    from core.skill_crucible import SkillCrucible
    code = """
import os
from pathlib import Path
def execute(**kwargs):
    outside = Path.cwd().parent
    return os.listdir(outside)
"""
    ok, message, _telemetry = SkillCrucible.run_sandbox_test(code, [{"input": {}}])
    assert ok is False
    assert "Sandbox static validation failed: Security Violation: prohibited call 'os.listdir'." in message


def test_crucible_rejects_unsafe_realpath_and_path_queries_before_execution():
    from core.skill_crucible import SkillCrucible

    for call in ("os.path.realpath(outside)", "os.path.exists(outside)"):
        code = f"""
import os
from pathlib import Path
def execute(**kwargs):
    outside = Path.cwd().parent
    return {call}
"""
        ok, message = SkillCrucible.validate_ast(code)
        assert ok is False
        assert "Security Violation: prohibited call 'os.path." in (message or "")

    runtime_code = """
from pathlib import Path
def execute(**kwargs):
    outside = Path.cwd().parent / "outside.txt"
    try:
        with open(outside, "r", encoding="utf-8") as handle:
            return handle.read()
    except PermissionError as exc:
        return {"denied": str(exc)}
"""
    ok, message, telemetry = SkillCrucible.run_sandbox_test(runtime_code, [{"input": {}}])
    assert ok is True, message
    assert telemetry["results"][0]["output"].startswith("{'denied':")
    assert "outside its temporary root" in telemetry["results"][0]["output"]



def test_crucible_blocks_native_and_dynamic_escape_surfaces():
    from core.skill_crucible import SkillCrucible
    cases = [
        "import numpy\ndef execute(**kwargs):\n    return numpy.load('/tmp/secret.npy')",
        "import webbrowser\ndef execute(**kwargs):\n    return webbrowser.open('https://example.com')",
        "import builtins\ndef execute(**kwargs):\n    return builtins.__import__('ctypes')",
        "import pickle\ndef execute(**kwargs):\n    return pickle.loads(b'')",
        "import concurrent.futures\ndef execute(**kwargs):\n    return concurrent.futures.ProcessPoolExecutor()",
        "import os\ndef execute(**kwargs):\n    return os.truncate('/tmp/secret', 0)",
        "import sys\ndef execute(**kwargs):\n    return sys.modules",
        "def execute(**kwargs):\n    return object.__subclasses__()",
    ]
    for code in cases:
        ok, error = SkillCrucible.validate_ast(code)
        assert ok is False
        assert "Security Violation" in (error or "")


def test_crucible_blocks_native_windows_escape_modules():
    from core.skill_crucible import SkillCrucible
    cases = [
        "import win32file\ndef execute(**kwargs):\n    return 1",
        "import win32cred\ndef execute(**kwargs):\n    return 1",
        "import winreg\ndef execute(**kwargs):\n    return 1",
    ]
    for code in cases:
        ok, error = SkillCrucible.validate_ast(code)
        assert ok is False
        assert "prohibited import" in (error or "")

def _make_payload_source(root: Path) -> Path:
    source = root / "payload"
    source.mkdir()
    (source / "BrahmaEvo.exe").write_bytes(b"app")
    (source / "BrahmaEvoSupervisor.exe").write_bytes(b"supervisor")
    return source


def test_payload_builder_rejects_symlink_sources(tmp_path: Path):
    import scripts.pack_windows_payload as payload

    source = _make_payload_source(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        (source / "leak.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this runner.")

    with pytest.raises(ValueError, match="symlinks"):
        payload._validate_source(source)


def test_payload_builder_does_not_destroy_existing_archive_on_failed_verification(tmp_path: Path, monkeypatch):
    import scripts.pack_windows_payload as payload

    source = _make_payload_source(tmp_path)
    output = tmp_path / "payload.zip"
    output.write_bytes(b"known-good-old-archive")

    def fail_verify(*_args, **_kwargs):
        raise RuntimeError("verification failed")

    monkeypatch.setattr(payload, "verify", fail_verify)
    with pytest.raises(RuntimeError, match="verification failed"):
        payload.build(source, output)

    assert output.read_bytes() == b"known-good-old-archive"


def test_payload_verifier_rejects_zip_symlinks_and_traversal(tmp_path: Path):
    import scripts.pack_windows_payload as payload
    import stat
    import zipfile

    source = _make_payload_source(tmp_path)
    for mode, name in (
        (stat.S_IFLNK | 0o777, "leak.txt"),
        (0o100644, "../escape.txt"),
    ):
        archive_path = tmp_path / f"malicious-{name.replace('/', '_')}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("BrahmaEvo.exe", b"app")
            archive.writestr("BrahmaEvoSupervisor.exe", b"supervisor")
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, b"bad")

        with pytest.raises(RuntimeError):
            payload.verify(source, archive_path)



def test_conversation_export_preserves_existing_file_if_atomic_promotion_fails(tmp_path: Path, monkeypatch):
    from workspace_store import WorkspaceStore

    workspace = WorkspaceStore(tmp_path / "workspace.sqlite3")
    conversation_id = workspace.create_conversation("Export test")
    destination = tmp_path / "conversation.json"
    destination.write_text("old export", encoding="utf-8")

    def fail_replace(_self, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        workspace.export_conversation(conversation_id, destination)

    assert destination.read_text(encoding="utf-8") == "old export"
    assert not list(tmp_path.glob(".conversation.json.export-*"))



def test_focus_window_never_reports_success_when_backend_returns_failure(monkeypatch):
    import actions.computer_control as computer

    class FailedProcess:
        returncode = 1

    monkeypatch.setattr(computer.subprocess, "run", lambda *_args, **_kwargs: FailedProcess())
    monkeypatch.setattr(computer, "_get_os", lambda: "windows")
    assert "failed" in computer._focus_window("Missing Window").lower()

    monkeypatch.setattr(computer, "_get_os", lambda: "mac")
    assert "failed" in computer._focus_window("Missing Window").lower()

    monkeypatch.setattr(computer, "_get_os", lambda: "linux")
    assert "failed" in computer._focus_window("Missing Window").lower()


def test_windows_focus_script_treats_false_appactivate_as_failure():
    source = Path("actions/computer_control.py").read_text(encoding="utf-8")
    assert 'if (-not $ok) {{ exit 1 }}' in source
    assert "if result.returncode != 0:" in source



def test_phone_link_status_does_not_report_success_on_service_failure(monkeypatch):
    from core.selected_capabilities import PhoneLinkBridge

    class FailingService:
        def list_devices(self):
            raise RuntimeError("device registry unavailable")

    monkeypatch.setattr(PhoneLinkBridge, "_service", lambda: FailingService())
    monkeypatch.setattr(PhoneLinkBridge, "installed", classmethod(lambda cls: False))

    result = PhoneLinkBridge.status()

    assert result["success"] is False
    assert "device registry unavailable" in result["error"]


def test_selected_capability_async_bridge_works_from_running_event_loop():
    from core.selected_capabilities import asyncio_run

    async def scenario():
        return asyncio_run(asyncio.sleep(0, result="bridge-ok"))

    assert asyncio.run(scenario()) == "bridge-ok"


def test_selected_capability_state_io_errors_do_not_reset_or_overwrite(tmp_path, monkeypatch):
    import core.selected_capabilities as capabilities

    path = tmp_path / "learning.json"
    path.write_text('{"schema_version":1,"events":[{"signal":"existing"}]}', encoding="utf-8")
    monkeypatch.setattr(capabilities.LearningEngine, "PATH", path)

    def fail_read(_path, _default):
        raise RuntimeError("read failure")

    monkeypatch.setattr(capabilities, "_json_load", fail_read)
    with pytest.raises(RuntimeError, match="read failure"):
        capabilities.LearningEngine.record("new")


def test_learning_record_is_serialized_under_concurrency(tmp_path, monkeypatch):
    import core.selected_capabilities as capabilities

    path = tmp_path / "learning.json"
    monkeypatch.setattr(capabilities.LearningEngine, "PATH", path)

    barrier = threading.Barrier(8)
    results = []

    def record(index):
        barrier.wait()
        results.append(capabilities.LearningEngine.record(f"signal-{index}"))

    threads = [threading.Thread(target=record, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    events = capabilities.LearningEngine.recent(20)
    assert len(events) == 8


def test_installer_source_copy_rejects_symlink_entries():
    source = Path("installer/install_wizard.py").read_text(encoding="utf-8")
    assert "followlinks=False" in source
    assert "source_root.is_symlink()" in source
    assert "os.path.islink(src_path)" in source
    assert "Unsafe installer source symlink" in source



def test_task_queue_console_logging_is_encoding_safe():
    source = Path("agent/task_queue.py").read_text(encoding="utf-8")
    assert "def _safe_print(message: object)" in source
    assert 'text.encode("ascii", "replace").decode("ascii")' in source
    assert "\nprint(f\"[TaskQueue]" not in source
    assert source.count("_safe_print(") >= 10



def test_life360_location_transport_errors_are_not_reported_as_success(monkeypatch):
    from core.selected_capabilities import Life360Provider

    provider = Life360Provider(enabled=True, token="test-token", base_url="http://127.0.0.1:8123")

    def fail_request(_path):
        raise OSError("HA unavailable")

    monkeypatch.setattr(provider, "_request_json", fail_request)
    locations = provider.locations()
    status = provider.status()

    assert locations == []
    assert status["success"] is False
    assert "HA unavailable" in status["error"]


def test_remote_compute_inventory_propagates_service_failures(monkeypatch):
    from core.selected_capabilities import RemoteComputeManager, execute_selected_capability

    monkeypatch.setattr(RemoteComputeManager, "computers", classmethod(lambda cls: [{"success": False, "error": "registry unavailable"}]))
    result = execute_selected_capability("remote_computing", "list")

    assert result["success"] is False
    assert result["error"] == "registry unavailable"


def test_installer_zip_payload_rejects_duplicate_entries_before_staging():
    source = Path("installer/install_wizard.py").read_text(encoding="utf-8")
    assert 'Installer payload contains duplicate ZIP entries.' in source
    assert 'destinations: set[Path] = set()' in source
    assert 'Installer payload contains colliding entries:' in source


def test_life360_credential_transport_disables_redirects():
    source = Path("core/selected_capabilities.py").read_text(encoding="utf-8")
    assert "class _NoRedirectHandler(urllib.request.HTTPRedirectHandler)" in source
    assert "Redirects are disabled for credential-bearing Home Assistant requests." in source
    assert "opener = urlrequest.build_opener(_NoRedirectHandler())" in source
    assert "with opener.open(req, timeout=self.timeout)" in source


def test_atomberg_authenticated_requests_reject_redirects():
    source = Path("smart_home/providers/builtin.py").read_text(encoding="utf-8")
    assert "allow_redirects=False" in source
    assert "Atomberg API redirected an authenticated request" in source


def test_memory_load_fails_closed_on_io_error(tmp_path, monkeypatch):
    import memory.memory_manager as memory

    path = tmp_path / "long_term.json"
    path.write_text('{"identity":{"name":{"value":"existing"}}}', encoding="utf-8")
    monkeypatch.setattr(memory, "MEMORY_PATH", path)

    monkeypatch.setattr(memory, "_read_memory_text", lambda: (_ for _ in ()).throw(OSError("temporary read failure")))

    with pytest.raises(RuntimeError, match="Unable to read persistent memory"):
        memory.load_memory()


def test_learned_rules_read_io_errors_fail_closed():
    from core.learned_rules import LearnedRulesEngine

    original = LearnedRulesEngine._load_raw
    LearnedRulesEngine._load_raw = staticmethod(lambda: (_ for _ in ()).throw(OSError("disk read failure")))
    try:
        result = LearnedRulesEngine.add_rule("keep existing rules")
    finally:
        LearnedRulesEngine._load_raw = original

    assert result["success"] is False
    assert "read" in result["message"].lower()


def test_learned_rules_mutations_do_not_report_success_when_save_fails(monkeypatch):
    from core.learned_rules import LearnedRulesEngine

    monkeypatch.setattr(LearnedRulesEngine, "_load_raw", staticmethod(lambda: [{"id": "r1", "rule": "old", "active": True}]))
    monkeypatch.setattr(LearnedRulesEngine, "_save_raw", staticmethod(lambda _rules: False))

    toggled = LearnedRulesEngine.toggle_rule("r1")
    deleted = LearnedRulesEngine.delete_rule("r1")

    assert toggled["success"] is False
    assert deleted["success"] is False


def test_update_memory_rejects_secret_bypass(tmp_path, monkeypatch):
    import memory.memory_manager as memory

    path = tmp_path / "long_term.json"
    monkeypatch.setattr(memory, "MEMORY_PATH", path)
    with __import__("pytest").raises(ValueError, match="Credential-like values"):
        memory.update_memory({
            "notes": {"secret": {"value": "api_key=sk-abcdefghijklmnopqrstuvwxyz123456"}}}
        )
    assert not path.exists()


def test_save_memory_rejects_secret_bypass(tmp_path, monkeypatch):
    import memory.memory_manager as memory

    path = tmp_path / "long_term.json"
    monkeypatch.setattr(memory, "MEMORY_PATH", path)
    payload = {"notes": {"secret": {"value": "api_key=sk-abcdefghijklmnopqrstuvwxyz123456"}}}

    with pytest.raises(ValueError, match="Credential-like values"):
        memory.save_memory(payload)
    assert not path.exists()


def test_smart_home_restart_reports_provider_failure(monkeypatch):
    from smart_home.service import SmartHomeService

    service = object.__new__(SmartHomeService)
    device = {"id": "d1", "provider_key": "test", "provider_account_id": "a1", "name": "Lamp"}
    class Storage:
        def get_device(self, device_id):
            return device if device_id == "d1" else None
        def get_provider_account(self, _account_id):
            return {"credentials": {}}
        def log_activity(self, *_args):
            pass
    class Provider:
        def execute(self, *_args):
            raise RuntimeError("restart unavailable")
    class Registry:
        def get(self, _key):
            return Provider()
    service._storage = Storage()
    service._registry = Registry()

    result = service.restart_device("d1")
    assert result["success"] is False
    assert result["error"] == "restart unavailable"


def test_smart_home_power_cycle_result_is_false_on_device_failure(monkeypatch):
    from smart_home.service import SmartHomeService

    service = object.__new__(SmartHomeService)
    device = {"id": "d1", "name": "Lamp", "is_on": True, "traits": {}, "room": "Kitchen"}
    service.list_devices = lambda search="", room="": [device]
    calls = {"count": 0}
    def fail_once(*_args, **_kwargs):
        calls["count"] += 1
        raise RuntimeError("device offline")
    service.execute_device_action = fail_once

    result = service.execute_command("turn off and on 3 times")
    assert result["success"] is False
    assert result["failures"]
    assert "device offline" in result["failures"][0]["error"]


def test_smart_home_provider_rejection_does_not_mutate_local_device_state():
    from smart_home.service import SmartHomeService

    service = object.__new__(SmartHomeService)
    device = {"id": "d1", "provider_key": "test", "provider_account_id": "a1", "name": "Lamp", "is_on": False, "traits": {}}
    class Storage:
        def get_device(self, device_id):
            return device if device_id == "d1" else None
        def get_provider_account(self, _account_id):
            return {"credentials": {}}
        def update_device(self, *_args, **_kwargs):
            raise AssertionError("Rejected provider actions must not mutate local state.")
        def log_activity(self, *_args):
            pass
    class Provider:
        def execute(self, *_args):
            return {"success": False, "error": "device refused"}
    class Registry:
        def get(self, _key):
            return Provider()
    service._storage = Storage()
    service._registry = Registry()

    result = service.execute_device_action("d1", "power", {"is_on": True})
    assert result["success"] is False
    assert result["error"] == "device refused"
    assert device["is_on"] is False


def test_executor_treats_boolean_false_as_tool_failure():
    from agent.executor import _raise_for_failed_tool_result

    with pytest.raises(RuntimeError, match="Tool reported failure"):
        _raise_for_failed_tool_result(False)
    _raise_for_failed_tool_result(True)


def test_executor_does_not_claim_forged_skill_executed_after_exception(monkeypatch):
    import agent.executor as executor
    from core.dynamic_registry import DynamicToolRegistry
    from core.skill_forge import SkillForge

    monkeypatch.setattr(
        SkillForge,
        "forge_skill",
        lambda *args, **kwargs: {
            "success": True,
            "name": "broken_skill",
            "description": "test",
        },
    )
    monkeypatch.setattr(DynamicToolRegistry, "has_tool", lambda _name: True)

    def fail_execute(*_args, **_kwargs):
        raise RuntimeError("forced execution failure")

    monkeypatch.setattr(DynamicToolRegistry, "execute_sync", fail_execute)
    spoken = []
    result = executor._run_skill_forge("test goal", speak=spoken.append)

    assert isinstance(result, dict)
    assert result["success"] is False
    assert result["created"] is True
    assert "initial execution failed" in result["error"].lower()
    assert not any("activated feature" in message.lower() for message in spoken)


def test_smart_home_credential_key_is_created_private_and_atomic():
    source = Path("smart_home/storage.py").read_text(encoding="utf-8")
    assert "os.O_EXCL" in source
    assert "os.O_WRONLY" in source
    assert "os.open(self._key_file, flags, 0o600)" in source
    assert "os.fsync(handle.fileno())" in source


def test_confirmation_gate_does_not_convert_explicit_false_to_done(monkeypatch):
    import core.confirm as confirm

    logs = []
    confirm.bind(lambda *_args: None, lambda: None, logs.append)
    with confirm._lock:
        confirm._pending = confirm._Pending(
            key="test",
            title="Dangerous operation",
            detail="detail",
            run=lambda: False,
            at=time.monotonic(),
        )
    confirm.resolve(True)

    deadline = time.time() + 1.0
    while not logs and time.time() < deadline:
        time.sleep(0.01)

    assert any("failed" in entry.lower() for entry in logs)
    assert not any("done." in entry.lower() for entry in logs)


def test_universal_task_preserves_structured_failure_for_executor_validation(monkeypatch):
    import agent.executor as executor
    import core.universal_agent as universal_agent

    failure = {
        "success": False,
        "status": "provider-failed",
        "message": "provider unavailable",
    }
    monkeypatch.setattr(universal_agent, "run", lambda *args, **kwargs: failure)

    result = executor._call_tool(
        "universal_task",
        {"request": "test task"},
        speak=None,
        player=None,
    )

    assert result == failure
    with pytest.raises(RuntimeError, match="provider unavailable"):
        executor._raise_for_failed_tool_result(result)


def test_executor_preserves_structured_dynamic_skill_failures():
    source = Path("agent/executor.py").read_text(encoding="utf-8")
    dynamic_block = source.split('elif tool == "dynamic_skill"', 1)[1].split('else:', 1)[0]
    assert "if isinstance(run_res, (dict, list, bool)):" in dynamic_block
    assert "return run_res" in dynamic_block


def test_confirmation_gate_does_not_replace_an_existing_pending_action():
    import core.confirm as confirm

    shown = []
    confirm.bind(lambda title, detail: shown.append((title, detail)), lambda: None)
    first = confirm.request("first", "First action", "first detail", lambda: "first")
    second = confirm.request("second", "Second action", "second detail", lambda: "second")

    assert "First action" in first
    assert "First action" in second
    assert len(shown) == 1

    with confirm._lock:
        pending = confirm._pending
    assert pending is not None
    assert pending.key == "first"

    confirm.resolve(False)


def test_openrouter_and_omniroute_authenticated_posts_disable_redirects():
    source = Path("or_client.py").read_text(encoding="utf-8")
    assert source.count("allow_redirects=False") >= 3
    assert "Authenticated request was redirected; refusing credential forwarding." in source


def test_geospatial_live_failures_do_not_fabricate_data(monkeypatch):
    import actions.geospatial_globe as globe

    monkeypatch.setattr(globe, "_open_no_redirect", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("network down")))

    with pytest.raises(ValueError, match="Unable to resolve location"):
        globe.geocode_location("definitely-not-a-real-place-9e7f")
    with pytest.raises(RuntimeError, match="Live weather"):
        globe.fetch_location_weather(34.05, -118.25)
    with pytest.raises(RuntimeError, match="Live flight"):
        globe.fetch_live_flights_in_bounds(33.0, 35.0, -119.0, -117.0)
    with pytest.raises(RuntimeError, match="Live ISS"):
        globe.fetch_live_iss()
    with pytest.raises(RuntimeError, match="Live earthquake"):
        globe.fetch_live_earthquakes()


def test_iss_tracker_does_not_fallback_to_plain_http():
    source = Path("actions/geospatial_globe.py").read_text(encoding="utf-8")
    assert "http://api.open-notify.org" not in source
    assert 'raise RuntimeError("Live ISS data is unavailable.") from exc' in source

def test_smart_home_credential_key_rejects_symlink(tmp_path):
    import smart_home.storage as storage

    target = tmp_path / "real.key"
    target.write_bytes(storage.Fernet.generate_key())
    link = tmp_path / "smart_home.key"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return
    vault = storage.CredentialVault.__new__(storage.CredentialVault)
    vault._key_file = link
    with pytest.raises(RuntimeError, match="may not be a symlink"):
        vault._load_or_create_key()

def test_device_mutations_roll_back_when_persistence_fails(tmp_path, monkeypatch):
    from brahma_connect.gateway.device_manager import DeviceManager

    manager = DeviceManager(tmp_path / "devices.json")
    record, secret = manager.create_from_pairing(name="Phone", platform="android")
    original_name = record.name
    original_revoked = record.revoked

    monkeypatch.setattr(manager, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    assert manager.remove(record.device_id) is False
    assert manager.get(record.device_id) is not None

    with pytest.raises(OSError, match="disk full"):
        manager.rename(record.device_id, "Renamed")
    assert manager.get(record.device_id).name == original_name

    assert manager.revoke(record.device_id) is False
    current = manager.get(record.device_id)
    assert current is not None
    assert current.revoked is original_revoked

    assert manager.authenticate(record.device_id, secret, ip="127.0.0.1", connection_id="c1") is None
    current = manager.get(record.device_id)
    assert current is not None
    assert current.online is False
    assert current.connection_id == ""

def test_gateway_disconnect_does_not_mark_active_device_offline_when_close_fails():
    from brahma_connect.gateway.server import BrahmaGateway

    gateway = object.__new__(BrahmaGateway)
    record = type(
        "Record",
        (),
        {
            "device_id": "d1",
            "name": "Phone",
            "online": True,
            "to_dict": lambda self: {"device_id": self.device_id, "online": self.online},
        },
    )()

    class Manager:
        def __init__(self):
            self.offline_called = False
        def resolve(self, _query):
            return [record]
        def get(self, _query):
            return None
        def mark_offline(self, _device_id):
            self.offline_called = True

    manager = Manager()
    gateway.device_manager = manager
    gateway._append_log = lambda *args, **kwargs: None

    class Hub:
        async def get(self, *_args, **_kwargs):
            return object()

        async def close_device(self, *_args, **_kwargs):
            return False

    gateway.hub = Hub()
    result = asyncio.run(gateway.disconnect_device("Phone"))
    assert result["success"] is False
    assert result["error_code"] == "DISCONNECT_FAILED"
    assert manager.offline_called is False

def test_connection_hub_close_failure_keeps_current_connection_tracked():
    class FailingSocket:
        async def close(self, **_kwargs):
            raise RuntimeError("close failed")

    async def scenario():
        hub = ConnectionHub()
        socket = FailingSocket()
        await hub.register(socket, "device-1")
        assert await hub.close_device("device-1", reason="test") is False
        state = await hub.get("device-1")
        assert state is not None
        assert await hub.is_current(socket, "device-1") is True

    asyncio.run(scenario())


def test_executor_skipped_step_cannot_be_reported_as_full_success(monkeypatch):
    import agent.executor as executor
    from agent.error_handler import ErrorDecision

    monkeypatch.setattr(
        executor,
        "create_plan",
        lambda _goal: {
            "steps": [{
                "step": 1,
                "tool": "test_action",
                "description": "Perform required action",
                "parameters": {},
            }]
        },
    )
    monkeypatch.setattr(
        executor,
        "_call_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("action unavailable")),
    )
    monkeypatch.setattr(
        executor,
        "analyze_error",
        lambda *_args, **_kwargs: {
            "decision": ErrorDecision.SKIP,
            "user_message": "Skipping unavailable action.",
        },
    )
    monkeypatch.setattr(
        executor.AgentExecutor,
        "_summarize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("skipped task must not enter success summary")),
    )

    result = executor.AgentExecutor().execute("do required action")
    assert "partially completed" in result.lower()
    assert "step 1" in result.lower()
    assert "action unavailable" in result.lower()


def test_executor_rejects_explicit_plain_text_failures_and_unverified_send_results():
    from agent.executor import _raise_for_failed_tool_result

    failure_results = [
        "Browser error: navigation failed",
        "Could not open WhatsApp.",
        "Playback control failed.",
        "Back error: history unavailable",
        "Attempted to send to Alice via WhatsApp; the desktop UI provided no delivery acknowledgement.",
    ]
    for result in failure_results[:3]:
        with pytest.raises(RuntimeError):
            _raise_for_failed_tool_result(result)
    with pytest.raises(RuntimeError, match="delivery was not verified"):
        _raise_for_failed_tool_result(failure_results[4], "send_message")


def test_send_message_does_not_log_failures_with_success_checkmark():
    source = Path("actions/send_message.py").read_text(encoding="utf-8")
    assert 'print(f"[SendMessage] ✅ {result}")' not in source


def test_executor_does_not_retry_non_idempotent_action_after_ambiguous_failure(monkeypatch):
    import agent.executor as executor
    from agent.error_handler import ErrorDecision

    attempts = []
    monkeypatch.setattr(
        executor,
        "create_plan",
        lambda _goal: {
            "steps": [{
                "step": 1,
                "tool": "send_message",
                "description": "Send message",
                "parameters": {"receiver": "Alice", "message_text": "hello"},
            }]
        },
    )
    monkeypatch.setattr(
        executor,
        "_call_tool",
        lambda *_args, **_kwargs: (attempts.append(True), (_ for _ in ()).throw(RuntimeError("delivery acknowledgement timed out")))[1],
    )
    monkeypatch.setattr(
        executor,
        "analyze_error",
        lambda *_args, **_kwargs: {
            "decision": ErrorDecision.RETRY,
            "reason": "timeout after dispatch",
            "user_message": "The delivery acknowledgement timed out.",
        },
    )

    result = executor.AgentExecutor().execute("send the message")
    assert len(attempts) == 1
    assert "could not be verified" in result.lower()
    assert "retry" in result.lower()


def test_executor_rejects_non_object_tool_parameters_before_dispatch():
    from agent.executor import _call_tool

    with pytest.raises(ValueError, match="parameters must be a JSON object"):
        _call_tool("web_search", ["query", "hello"], speak=None)

    with pytest.raises(ValueError, match="parameters must be a JSON object"):
        _call_tool("web_search", "query=hello", speak=None)


def test_action_result_failure_classifier_does_not_misclassify_empty_or_explicit_failures():
    from core.action_result import action_result_is_failure
    assert action_result_is_failure("") is True
    assert action_result_is_failure("Could not open the Gmail inbox.") is True
    assert action_result_is_failure("Gmail credentials not configured.") is True
    assert action_result_is_failure("No messages found matching query 'ALL'.") is False
