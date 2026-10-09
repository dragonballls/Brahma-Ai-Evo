from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_device_auth_rejects_empty_or_malformed_credentials(tmp_path):
    from brahma_connect.gateway.device_manager import DeviceManager

    manager = DeviceManager(tmp_path / "devices.json")
    record, secret = manager.create_from_pairing(name="Test Device", platform="android")
    assert manager.authenticate(record.device_id, "") is None
    assert manager.authenticate(record.device_id, "   ") is None
    assert manager.authenticate(record.device_id, "not-the-device-secret") is None
    assert manager.authenticate("", secret) is None
    authenticated = manager.authenticate(record.device_id, secret)
    assert authenticated is not None
    assert authenticated.device_id == record.device_id

def test_device_registry_temp_file_is_process_safe():
    source = (ROOT / "brahma_connect" / "gateway" / "device_manager.py").read_text(encoding="utf-8")
    assert 'uuid.uuid4().hex' in source
    assert 'registry_path.name}.tmp' not in source


def test_failed_gateway_auth_does_not_leave_device_id_set_for_handshake_timeout_bypass():
    source = (ROOT / "brahma_connect" / "gateway" / "server.py").read_text(encoding="utf-8")
    block_start = source.index("if msg_type == ProtocolTypes.AUTHENTICATE:")
    block_end = source.index("if msg_type == ProtocolTypes.PAIR_REQUEST:", block_start)
    block = source[block_start:block_end]
    assert "attempted_device_id" in block
    assert 'device_id = ""' in block
    assert "if record is None:" in block
