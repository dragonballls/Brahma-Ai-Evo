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


def test_corrupt_gateway_device_registry_is_quarantined():
    from brahma_connect.gateway.device_manager import DeviceManager

    path = Path("corrupt-device-registry-test.json")
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
        assert result["message"] == "Device approved and credentials delivered directly to the paired device."
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

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("network download must not start after checksum mismatch")

    monkeypatch.setattr(ota.urllib.request, "urlopen", unexpected_download)
    result = ota.download_and_apply_update(
        "https://example.test/setup.exe",
        expected_sha256="b" * 64,
    )
    assert result is False


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
    assert "Crucible sandbox denied filesystem access outside its temporary root." in message


def test_crucible_realpath_and_dynamic_path_queries_are_safely_confined():
    from core.skill_crucible import SkillCrucible

    code = """
import os
from pathlib import Path
def execute(**kwargs):
    outside = Path.cwd().parent
    try:
        os.path.realpath(outside)
        getattr(os.path, "exists")(outside)
    except PermissionError as exc:
        return {"denied": str(exc)}
    return {"denied": False}
"""
    ok, message, telemetry = SkillCrucible.run_sandbox_test(code, [{"input": {}}])
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
