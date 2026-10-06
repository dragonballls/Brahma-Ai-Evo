from pathlib import Path


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
    assert "if item.is_symlink():" in block
    assert "Refusing symlinked feature entry" in block
