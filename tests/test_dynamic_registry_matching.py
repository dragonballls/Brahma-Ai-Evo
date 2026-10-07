from pathlib import Path

from core.dynamic_registry import DynamicSkill, DynamicToolRegistry


def test_dynamic_skill_failed_import_does_not_poison_sys_modules(tmp_path):
    import sys
    from core.dynamic_registry import DynamicSkill

    code = tmp_path / "broken_skill.py"
    code.write_text("def execute(:\n    return None\n", encoding="utf-8")
    skill = DynamicSkill(code, {"name": "broken_dynamic_skill"})
    module_name = f"brahma_skill_{skill.name}_{abs(hash(str(code.resolve())))}"
    try:
        skill._load_module()
    except SyntaxError:
        pass
    else:
        raise AssertionError("Broken skill unexpectedly loaded.")
    assert module_name not in sys.modules


def test_dynamic_skill_alias_does_not_match_inside_unrelated_word():
    original = DynamicToolRegistry._skills
    initialized = DynamicToolRegistry._initialized
    try:
        DynamicToolRegistry._initialized = True
        skill = DynamicSkill(
            Path("net.py"),
            {
                "name": "network_check",
                "description": "Check a configured network target.",
                "aliases": ["net"],
            },
        )
        DynamicToolRegistry._skills = {"network_check": skill, "net": skill}
        assert DynamicToolRegistry.find_matching_skill("check internet connectivity") is None
        assert DynamicToolRegistry.find_matching_skill("check the net") == ("network_check", {})
    finally:
        DynamicToolRegistry._skills = original
        DynamicToolRegistry._initialized = initialized
