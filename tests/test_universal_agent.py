from __future__ import annotations

import unittest

from core import universal_agent


class UniversalCapabilityAgentTests(unittest.TestCase):
    def test_existing_skill_is_preferred(self):
        class Registry:
            @classmethod
            def initialize(cls):
                pass

            @classmethod
            def find_matching_skill(cls, request):
                return ("known_skill", {"query": "hello"})

            @classmethod
            def has_tool(cls, name):
                return name == "known_skill"

            @classmethod
            def execute_sync(cls, name, args):
                return {"summary": f"{name}:{args['query']}"}

        original = universal_agent.DynamicToolRegistry
        try:
            universal_agent.DynamicToolRegistry = Registry
            result = universal_agent.run("do the known thing")
        finally:
            universal_agent.DynamicToolRegistry = original

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "executed-existing-skill")
        self.assertEqual(result["skill"], "known_skill")

    def test_packaged_skill_directory_is_authoritative_over_legacy_module(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
        block = source.split("def initialize", 1)[1].split("def get_tool_declarations", 1)[0]
        self.assertIn('packaged_dir = item.with_suffix("")', block)
        self.assertIn('(packaged_dir / "manifest.json").is_file()', block)
        self.assertIn('(packaged_dir / "skill.py").is_file()', block)
        self.assertIn("continue", block)

    def test_dynamic_tool_declarations_deduplicate_aliases(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
        block = source.split("def get_tool_declarations", 1)[1].split("def has_tool", 1)[0]
        self.assertIn("seen: set[int] = set()", block)
        self.assertIn("id(skill) in seen", block)

    def test_packaged_and_legacy_skills_register_manifest_aliases(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "dynamic_registry.py").read_text(encoding="utf-8")
        packaged = source.split("elif item.is_dir():", 1)[1].split("    # 2. Check legacy", 1)[0]
        legacy = source.split("# 2. Check legacy AppData skills vault", 1)[1]
        for block in (packaged, legacy):
            self.assertIn("for alias in skill.aliases:", block)
            self.assertIn('cls._skills[str(alias)] = skill', block)
            self.assertIn('if alias not in cls._skills:', block)

    def test_prompt_routes_unmatched_capabilities_to_universal_task(self):
        from pathlib import Path

        prompt = (Path(__file__).resolve().parents[1] / "core" / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("call `universal_task`", prompt)
        self.assertIn("Do not merely explain that the capability is unavailable", prompt)

    def test_skill_forge_keeps_generated_network_code_on_verified_tls(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "core" / "skill_forge.py").read_text(encoding="utf-8")
        start = source.index("4. Resilient Network & Safe SSL Handling:")
        end = source.index("5. Visual Deliverables", start)
        block = source[start:end]
        self.assertIn("verify=True", block)
        self.assertNotIn("verify=False", block)
        self.assertNotIn("_create_unverified_context", block)
        self.assertNotIn("LIVDSRZULELA", block)
    def test_empty_request_is_rejected(self):
        result = universal_agent.run("   ")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "invalid")

    def test_forge_failure_is_returned_cleanly(self):
        class Registry:
            @classmethod
            def initialize(cls):
                pass

            @classmethod
            def find_matching_skill(cls, request):
                return None

            @classmethod
            def has_tool(cls, name):
                return False

        class Forge:
            @classmethod
            def forge_skill(cls, *args, **kwargs):
                return {"success": False, "message": "no model"}

        original_registry = universal_agent.DynamicToolRegistry
        original_import = __import__

        try:
            universal_agent.DynamicToolRegistry = Registry

            import sys
            old_module = sys.modules.get("core.skill_forge")

            class FakeModule:
                SkillForge = Forge

            sys.modules["core.skill_forge"] = FakeModule
            result = universal_agent.run("invent a missing capability")
        finally:
            universal_agent.DynamicToolRegistry = original_registry
            import sys
            if old_module is None:
                sys.modules.pop("core.skill_forge", None)
            else:
                sys.modules["core.skill_forge"] = old_module

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "synthesis-failed")


    def test_existing_skill_structured_failure_is_not_reported_as_success(self):
        class Registry:
            @classmethod
            def initialize(cls):
                pass

            @classmethod
            def find_matching_skill(cls, request):
                return ("known_skill", {})

            @classmethod
            def has_tool(cls, name):
                return name == "known_skill"

            @classmethod
            def execute_sync(cls, name, args):
                return {
                    "success": False,
                    "status": "provider-failed",
                    "error": "provider unavailable",
                }

        original = universal_agent.DynamicToolRegistry
        try:
            universal_agent.DynamicToolRegistry = Registry
            result = universal_agent.run("do the known thing")
        finally:
            universal_agent.DynamicToolRegistry = original

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "existing-skill-failed")
        self.assertEqual(result["error"], "provider unavailable")


if __name__ == "__main__":
    unittest.main()
