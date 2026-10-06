from unittest.mock import patch

from core.dynamic_registry import DynamicToolRegistry
from core.skill_crucible import SkillCrucible
from core.skill_forge import SkillForge


def test_forged_feature_registers_and_executes(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized
    payload = {
        "success": True,
        "manifest": {
            "name": "forge_smoke_test",
            "description": "Forge smoke test",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return "forge smoke test passed"\n',
        "test_cases": [{"input": {}}],
    }

    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", tmp_path / "features"),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", tmp_path / "vault"),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=payload),
            patch.object(SkillCrucible, "resolve_dependencies", return_value=(True, "No dependencies.")),
        ):
            result = SkillForge.forge_skill("smoke test feature", "forge_smoke_test")

            assert result["success"] is True, result
            assert DynamicToolRegistry.execute_sync("forge_smoke_test", {}) == "forge smoke test passed"
            assert (tmp_path / "vault" / "forge_smoke_test" / "skill.py").is_file()
            assert not (tmp_path / "features" / "forge_smoke_test.py").exists()
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized


def test_forged_skill_persistence_is_outside_repository(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized
    payload = {
        "success": True,
        "manifest": {
            "name": "persistent_smoke_test",
            "description": "Persistent smoke test",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return "persistent"\n',
        "test_cases": [{"input": {}}],
    }
    source = tmp_path / "features"
    vault = tmp_path / "vault"
    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", source),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", vault),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=payload),
            patch.object(SkillCrucible, "resolve_dependencies", return_value=(True, "No dependencies.")),
        ):
            result = SkillForge.forge_skill("persist this capability", "persistent_smoke_test")
            assert result["success"] is True, result
            assert str(vault / "persistent_smoke_test" / "skill.py") == result["skill_path"]
            assert not (source / "persistent_smoke_test.py").exists()
            assert not (source / "persistent_smoke_test").exists()
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized


def test_skill_forge_rejects_unsafe_manifest_names(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized
    payload = {
        "success": True,
        "manifest": {
            "name": "../escape",
            "description": "Unsafe name",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return "should never persist"\n',
        "test_cases": [{"input": {}}],
    }
    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", tmp_path / "features"),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", tmp_path / "vault"),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=payload),
        ):
            result = SkillForge.forge_skill("unsafe skill", "safe_hint")
            assert result["success"] is False
            assert "unsafe skill name" in result["message"].lower()
            assert not (tmp_path / "escape").exists()
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized


def test_skill_forge_refuses_to_overwrite_existing_skill(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized
    payload = {
        "success": True,
        "manifest": {
            "name": "existing_skill",
            "description": "Existing",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return "new"\n',
        "test_cases": [{"input": {}}],
    }
    vault = tmp_path / "vault"
    existing = vault / "existing_skill"
    existing.mkdir(parents=True)
    (existing / "skill.py").write_text('def execute(**kwargs):\n    return "old"\n', encoding="utf-8")
    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", tmp_path / "features"),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", vault),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=payload),
        ):
            result = SkillForge.forge_skill("overwrite check", "existing_skill")
            assert result["success"] is False
            assert "already exists" in result["message"]
            assert (existing / "skill.py").read_text(encoding="utf-8").strip().endswith('return "old"')
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized


def test_crucible_rejects_dangerous_generated_primitives():
    cases = {
        'import subprocess\ndef execute(**kwargs):\n    return subprocess.run(["whoami"])': "prohibited import 'subprocess'",
        'from os import system as run_cmd\ndef execute(**kwargs):\n    return run_cmd("echo test")': "prohibited imported call 'run_cmd'",
        'from pathlib import Path\ndef execute(**kwargs):\n    return Path("demo.txt").unlink()': "prohibited Path.unlink",
        'import ssl\ndef execute(**kwargs):\n    return ssl._create_unverified_context()': "prohibited call 'ssl._create_unverified_context'",
        'import requests\ndef execute(**kwargs):\n    return requests.get("https://example.com", verify=False)': "TLS certificate verification cannot be disabled.",
        'def execute(**kwargs):\n    return eval("1+1")': "prohibited dynamic execution 'eval'",
    }
    for code, expected in cases.items():
        ok, error = SkillCrucible.validate_ast(code)
        assert ok is False
        assert expected in (error or "")


def test_crucible_detects_error_dictionary_in_sandbox():
    broken_code = 'def execute(**kwargs):\n    return {"error": "SSL handshake failed"}\n'
    ok, msg, telemetry = SkillCrucible.run_sandbox_test(broken_code, [{"input": {}}])
    assert ok is False
    assert "SSL handshake failed" in msg

    working_code = 'def execute(**kwargs):\n    return {"title": "Success", "summary": "Rendered"}\n'
    ok2, msg2, telemetry2 = SkillCrucible.run_sandbox_test(working_code, [{"input": {}}])
    assert ok2 is True


def test_forge_repairs_skill_when_sandbox_returns_error_dict(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized

    initial_payload = {
        "success": True,
        "manifest": {
            "name": "repair_smoke_test",
            "description": "Repair test",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return {"error": "Initial SSL failure"}\n',
        "test_cases": [{"input": {}}],
    }

    repaired_code = 'def execute(**kwargs):\n    return {"title": "Repaired Deliverable", "summary": "Working"}\n'

    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", tmp_path / "features"),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", tmp_path / "vault"),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=initial_payload),
            patch.object(SkillCrucible, "resolve_dependencies", return_value=(True, "No dependencies.")),
            patch.object(SkillForge, "_repair_code", return_value={"success": True, "code": repaired_code}) as mock_repair,
        ):
            result = SkillForge.forge_skill("repair smoke test", "repair_smoke_test")

            assert mock_repair.called
            assert result["success"] is True, result
            res = DynamicToolRegistry.execute_sync("repair_smoke_test", {})
            assert isinstance(res, dict) and res.get("title") == "Repaired Deliverable"
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized