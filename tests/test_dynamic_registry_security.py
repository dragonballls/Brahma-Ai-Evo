from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_persisted_skill_reload_revalidates_and_confines_source():
    source = (ROOT / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
    assert "vault_root = APPDATA_SKILLS_DIR.resolve()" in source
    assert "skill_root.relative_to(vault_root)" in source
    assert "code_file.relative_to(skill_root)" in source
    assert "SkillCrucible.validate_ast(source)" in source

def test_skill_toggle_persists_before_mutating_live_state():
    source = (ROOT / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
    block = source[source.index("def toggle_skill"):source.index("def delete_skill", source.index("def toggle_skill"))]
    assert "updated_manifest = dict(skill.manifest)" in block
    assert "os.replace(temp_path, manifest_path)" in block
    assert "skill.manifest = updated_manifest" in block
    assert "skill.active = new_status" in block

def test_dynamic_registry_rejects_symlinked_native_features():
    source = (ROOT / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
    start = source.index("for item in sorted(FEATURES_DIR.iterdir()")
    block = source[start:source.index("# Case A: Single Python module", start)]
    assert "if _is_link_like(item):" in block
    assert "Refusing symlinked/junction/reparse feature entry" in block


def test_untrusted_skill_executes_validated_source_bytes_after_replacement_race(tmp_path, monkeypatch):
    from core.dynamic_registry import DynamicSkill
    from core.skill_crucible import SkillCrucible

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    code = skill_dir / "skill.py"
    manifest = skill_dir / "manifest.json"
    code.write_text("def execute(**kwargs):\n    return 'safe'\n", encoding="utf-8")
    manifest.write_text("{\"name\": \"race_skill\"}", encoding="utf-8")

    original_validate = SkillCrucible.validate_ast
    swapped = {"done": False}

    def validate_then_swap(source):
        result = original_validate(source)
        if not swapped["done"]:
            swapped["done"] = True
            code.write_text("def execute(**kwargs):\n    return 'ATTACKED'\n", encoding="utf-8")
        return result

    monkeypatch.setattr(SkillCrucible, "validate_ast", staticmethod(validate_then_swap))
    skill = DynamicSkill(skill_dir, {"name": "race_skill"}, untrusted=True)
    assert skill.execute_sync() == "safe"


def test_dynamic_registry_rejects_native_feature_deletion():
    from core.dynamic_registry import DynamicSkill, DynamicToolRegistry
    original = DynamicToolRegistry._skills
    initialized = DynamicToolRegistry._initialized
    try:
        native = DynamicSkill(ROOT / "features" / "native_feature.py", {"name": "native_feature"})
        DynamicToolRegistry._skills = {"native_feature": native}
        DynamicToolRegistry._initialized = True
        assert DynamicToolRegistry.delete_skill("native_feature") is False
    finally:
        DynamicToolRegistry._skills = original
        DynamicToolRegistry._initialized = initialized


def test_dynamic_registry_rejects_junction_and_reparse_native_feature_entries():
    source = (ROOT / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
    assert "def _is_link_like(path: Path) -> bool:" in source
    assert 'is_junction = getattr(path, "is_junction", None)' in source
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in source
    assert "if _is_link_like(item):" in source
    assert "if _is_link_like(manifest_file) or _is_link_like(code_file) or _is_link_like(item):" in source


def test_persisted_skill_rejects_hardlinked_code(tmp_path):
    from core.dynamic_registry import DynamicSkill

    skill_dir = tmp_path / "hardlinked_skill"
    skill_dir.mkdir()
    source = tmp_path / "source.py"
    code = skill_dir / "skill.py"
    source.write_text("def execute(**kwargs):\n    return 'safe'\n", encoding="utf-8")
    try:
        code.hardlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Hard links are unavailable in this test environment.")

    skill = DynamicSkill(skill_dir, {"name": "hardlinked_skill"}, untrusted=True)
    with pytest.raises(RuntimeError, match="hard-linked"):
        skill.execute_sync()


def test_untrusted_skill_cannot_read_host_filesystem_during_activation(tmp_path):
    from core.dynamic_registry import DynamicSkill

    skill_dir = tmp_path / "host_escape"
    skill_dir.mkdir()
    code = skill_dir / "skill.py"
    code.write_text(
        "def execute(**kwargs):\n"
        "    with open('/etc/hosts', 'r', encoding='utf-8') as handle:\n"
        "        return handle.read()\n",
        encoding="utf-8",
    )

    skill = DynamicSkill(skill_dir, {"name": "host_escape"}, untrusted=True)
    with pytest.raises(RuntimeError, match="execution failed safely"):
        skill.execute_sync()
