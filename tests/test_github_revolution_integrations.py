from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.agent_task_ledger import AgentTaskLedger
from core.capability_sources import CURATED_SOURCES, matching_sources
from core.github_research import GitHubResearchClient
from core.memory_reflection import ReflectiveMemory
from core.software_harness import SoftwareHarnessRegistry


class GithubRevolutionIntegrationTests(unittest.TestCase):
    def test_curated_source_catalog_contains_all_owner_suggested_repos(self):
        names = {item["repository"] for item in CURATED_SOURCES}
        expected = {
            "paperclipai/paperclip",
            "vectorize-io/hindsight",
            "debpalash/VoiceStudio",
            "rohitg00/ai-engineering-from-scratch",
            "anthropics/financial-services",
            "vercel/next.js",
            "pbakaus/impeccable",
            "HKUDS/CLI-Anything",
            "alirezarezvani/claude-skills",
            "davila7/claude-code-templates",
        }
        self.assertTrue(expected <= names)

    def test_matching_sources_prioritizes_relevant_patterns(self):
        matches = matching_sources("improve autonomous agent orchestration with heartbeats")
        names = {item["repository"] for item in matches[:3]}
        self.assertIn("paperclipai/paperclip", names)

        matches = matching_sources("improve long term memory recall and learning")
        names = {item["repository"] for item in matches[:3]}
        self.assertIn("vectorize-io/hindsight", names)

    def test_agent_task_ledger_persists_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = AgentTaskLedger(Path(tmp) / "ledger.json")
            task_id = ledger.create("demo", "test goal", source="test")
            self.assertTrue(ledger.heartbeat(task_id, evidence="started"))
            self.assertTrue(ledger.complete(task_id, evidence="passed"))
            recent = ledger.recent(5)
            self.assertEqual(recent[0]["task_id"], task_id)
            self.assertEqual(recent[0]["state"], "completed")
            self.assertGreaterEqual(recent[0]["heartbeats"], 2)

    def test_reflective_memory_uses_existing_store_contract(self):
        memory = ReflectiveMemory()
        fake = {
            "preferences": {"theme": {"value": "ice"}},
            "notes": {"workspace": {"value": "keep workspaces persistent"}},
        }
        with patch("memory.memory_manager.load_memory", return_value=fake):
            result = memory.reflect("workspace theme", limit=4)
        self.assertIn("REFLECTIVE MEMORY CONTEXT", result)
        self.assertIn("keep workspaces persistent", result)

    def test_harness_registry_is_dependency_light(self):
        registry = SoftwareHarnessRegistry()
        discovered = registry.discover()
        names = {item["name"] for item in discovered}
        self.assertTrue({"git", "python", "powershell"} <= names)
        context = registry.context_for("run git status")
        self.assertIn("AGENT-NATIVE SOFTWARE HARNESS CONTEXT", context)

    def test_github_research_includes_curated_sources(self):
        client = GitHubResearchClient()
        with patch.object(client, "search_repositories", return_value=[]),              patch.object(client, "search_code", return_value=[]),              patch.object(
                 client,
                 "get_repository",
                 side_effect=Exception("network not used in unit test"),
             ):
            result = client.research_goal("agent orchestration heartbeat task approvals")
        self.assertIn("curated_sources", result)
        self.assertTrue(result["curated_sources"])
        self.assertIn("paperclipai/paperclip", {x["repository"] for x in result["curated_sources"]})
        dossier = client.format_dossier(result)
        self.assertIn("Curated research sources:", dossier)


if __name__ == "__main__":
    unittest.main()
