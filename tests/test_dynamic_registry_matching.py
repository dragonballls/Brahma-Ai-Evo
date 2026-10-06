from pathlib import Path

from core.dynamic_registry import DynamicSkill, DynamicToolRegistry


def test_dynamic_skill_alias_does_not_match_inside_unrelated_word():
    original = DynamicToolRegistry._skills
    try:
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
