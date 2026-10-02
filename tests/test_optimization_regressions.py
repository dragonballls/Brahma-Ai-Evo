from pathlib import Path

from starlette.requests import Request

from core.dynamic_registry import DynamicSkill, DynamicToolRegistry
from core.local_brain import LocalBrain, convert_schema_to_lowercase
from core.skill_crucible import SkillCrucible
from memory import config_manager
from brahma_connect.gateway.server import BrahmaGateway


def test_config_manager_atomic_round_trip(tmp_path, monkeypatch):
    settings_file = tmp_path / "app_settings.json"
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_manager, "_settings_cache", None)
    monkeypatch.setattr(config_manager, "_settings_mtime_ns", None)

    config_manager.save_settings({"local_ai_model": "qwen2.5:3b"})
    assert config_manager.load_settings()["local_ai_model"] == "qwen2.5:3b"
    assert settings_file.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_local_brain_honors_configured_endpoint(monkeypatch):
    monkeypatch.setattr(
        config_manager,
        "load_settings",
        lambda: {
            "local_ai_url": "http://127.0.0.1:1234/v1/",
            "local_ai_model": "test-model",
        },
    )
    brain = LocalBrain()
    assert brain.endpoint == "http://127.0.0.1:1234/v1"
    assert brain.default_model == "test-model"

    schema = {"type": "OBJECT", "properties": {"count": {"type": "INTEGER"}}}
    assert convert_schema_to_lowercase(schema)["type"] == "object"
    assert convert_schema_to_lowercase(schema)["properties"]["count"]["type"] == "integer"


def test_crucible_blocks_high_risk_generated_code():
    bad_import = "import subprocess\ndef execute(**kwargs):\n    return 'x'\n"
    ok, error = SkillCrucible.validate_ast(bad_import)
    assert ok is False
    assert "subprocess" in error

    bad_call = "import os\ndef execute(**kwargs):\n    os.system('echo nope')\n    return 'x'\n"
    ok, error = SkillCrucible.validate_ast(bad_call)
    assert ok is False
    assert "os.system" in error


def test_registry_deduplicates_aliases(tmp_path, monkeypatch):
    skill_file = tmp_path / "skill.py"
    skill_file.write_text("def execute(**kwargs): return 'ok'\n", encoding="utf-8")
    skill = DynamicSkill(
        skill_file,
        {
            "name": "demo",
            "description": "demo",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
    )

    monkeypatch.setattr(DynamicToolRegistry, "_skills", {"demo": skill, "alias": skill})
    monkeypatch.setattr(DynamicToolRegistry, "_initialized", True)
    declarations = DynamicToolRegistry.get_tool_declarations()
    assert [item["name"] for item in declarations] == ["demo"]


def test_gateway_admin_is_loopback_only():
    gateway = BrahmaGateway.__new__(BrahmaGateway)
    local = Request({"type": "http", "client": ("127.0.0.1", 1234)})
    remote = Request({"type": "http", "client": ("192.168.1.50", 1234)})
    assert gateway._is_loopback_client(local) is True
    assert gateway._is_loopback_client(remote) is False
