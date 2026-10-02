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


def test_crucible_dependency_names_are_data_only(monkeypatch):
    calls = []

    class Proc:
        returncode = 1

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Proc()

    monkeypatch.setattr("core.skill_crucible.subprocess.run", fake_run)
    ok, message = SkillCrucible.resolve_dependencies(["os;__import__('shutil').rmtree('x')"])
    assert ok is False
    assert calls == []
    assert "Missing preinstalled dependencies" in message


def test_autoheal_rejects_external_absolute_path(monkeypatch, tmp_path):
    external = tmp_path / "outside.py"
    external.write_text("print('outside')\n", encoding="utf-8")
    tb = f'Traceback (most recent call last):\n  File "{external}", line 1, in <module>\nValueError: boom'
    parsed = __import__("actions.auto_heal_engine", fromlist=["TracebackAnalyzer"]).TracebackAnalyzer.parse(tb)
    assert parsed["success"] is False


def test_autoheal_allows_repo_source_and_parses_line(tmp_path, monkeypatch):
    from actions.auto_heal_engine import TracebackAnalyzer
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = repo_root / "actions" / "safe_feature.py"
    target.parent.mkdir()
    target.write_text("def execute():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr("actions.auto_heal_engine.BASE_DIR", repo_root)
    tb = f'Traceback (most recent call last):\n  File "{target}", line 2, in execute\nValueError: boom'
    parsed = TracebackAnalyzer.parse(tb)
    assert parsed["success"] is True
    assert parsed["line_number"] == 2


def test_autoheal_protects_runtime_boundary(tmp_path, monkeypatch):
    from actions.auto_heal_engine import TracebackAnalyzer
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = repo_root / "main.py"
    target.write_text("print('x')\n", encoding="utf-8")
    monkeypatch.setattr("actions.auto_heal_engine.BASE_DIR", repo_root)
    tb = f'Traceback (most recent call last):\n  File "{target}", line 1, in <module>\nValueError: boom'
    parsed = TracebackAnalyzer.parse(tb)
    assert parsed["success"] is False


def test_desktop_generated_code_blocks_dangerous_operations():
    from actions.desktop import _execute_generated_code

    blocked_ctypes = _execute_generated_code(
        "ctypes.windll.user32.MessageBoxW(0, 'x', 'x', 0)"
    )
    assert "blocked by the desktop safety policy" in blocked_ctypes

    blocked_delete = _execute_generated_code(
        "Path.home().joinpath('x').unlink()"
    )
    assert "blocked by the desktop safety policy" in blocked_delete
