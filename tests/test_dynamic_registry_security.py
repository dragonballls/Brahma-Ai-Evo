from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_persisted_skill_reload_revalidates_and_confines_source():
    source = (ROOT / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
    assert "vault_root = APPDATA_SKILLS_DIR.resolve()" in source
    assert "skill_root.relative_to(vault_root)" in source
    assert "code_file.relative_to(skill_root)" in source
    assert "SkillCrucible.validate_ast(source)" in source
