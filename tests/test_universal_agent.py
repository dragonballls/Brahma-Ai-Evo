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


if __name__ == "__main__":
    unittest.main()
