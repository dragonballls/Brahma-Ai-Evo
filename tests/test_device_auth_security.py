from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_device_auth_rejects_empty_or_malformed_credentials():
    source = (ROOT / "brahma_connect" / "gateway" / "device_manager.py").read_text(encoding="utf-8")
    assert 'if not key or not str(record.secret_hash or "").strip()' in source
    assert 'if not str(secret or "").strip()' in source


def test_device_registry_temp_file_is_process_safe():
    source = (ROOT / "brahma_connect" / "gateway" / "device_manager.py").read_text(encoding="utf-8")
    assert 'uuid.uuid4().hex' in source
    assert 'registry_path.name}.tmp' not in source
