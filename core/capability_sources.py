"""Curated high-value GitHub research sources for Brahma Evo.

These entries are research seeds, not executable dependencies. They make the
GitHub-first system consistently consider strong external patterns discovered
by the project owner while preserving license-aware synthesis.
"""

from __future__ import annotations

CURATED_SOURCES = (
    {
        "repository": "paperclipai/paperclip",
        "license": "MIT",
        "domains": ("agent orchestration", "heartbeats", "governance", "task management", "agent teams"),
        "keywords": ("agent", "orchestration", "task", "heartbeat", "approval", "budget", "workflow"),
        "patterns": ("goal-aligned tasks", "short-lived heartbeats", "durable run history", "approval gates"),
    },
    {
        "repository": "vectorize-io/hindsight",
        "license": "MIT",
        "domains": ("memory", "retention", "recall", "reflection"),
        "keywords": ("memory", "remember", "recall", "learn", "history", "context", "knowledge"),
        "patterns": ("retain/recall/reflect", "long-term learning", "memory banks"),
    },
    {
        "repository": "HKUDS/CLI-Anything",
        "license": "Apache-2.0",
        "domains": ("computer use", "software automation", "agent-native software"),
        "keywords": ("software", "app", "application", "cli", "automation", "desktop", "tool"),
        "patterns": ("stateful CLI harnesses", "structured JSON output", "preview and E2E loops"),
    },
    {
        "repository": "pbakaus/impeccable",
        "license": "Apache-2.0",
        "domains": ("ui", "frontend", "design", "quality", "accessibility"),
        "keywords": ("ui", "ux", "frontend", "interface", "dashboard", "design", "layout", "accessibility"),
        "patterns": ("deterministic design detectors", "design/product contracts", "browser iteration"),
    },
    {
        "repository": "alirezarezvani/claude-skills",
        "license": "MIT",
        "domains": ("skills", "coding", "devops", "security"),
        "keywords": ("skill", "plugin", "coding", "security", "devops", "testing", "architecture"),
        "patterns": ("reusable skill packages", "progressive disclosure", "multi-agent tool compatibility"),
    },
    {
        "repository": "davila7/claude-code-templates",
        "license": "MIT",
        "domains": ("agents", "skills", "hooks", "mcp", "development"),
        "keywords": ("agent", "skill", "hook", "mcp", "template", "testing", "plugin"),
        "patterns": ("agent/command/hook catalogs", "health checks", "configuration templates"),
    },
    {
        "repository": "debpalash/VoiceStudio",
        "license": "AGPL-3.0",
        "domains": ("voice", "tts", "stt", "dictation", "voice agents"),
        "keywords": ("voice", "tts", "stt", "speech", "dictation", "transcription", "audio"),
        "patterns": ("local voice engine adapters", "voice profiles", "agent-facing local API/MCP"),
    },
    {
        "repository": "vercel/next.js",
        "license": "MIT",
        "domains": ("web workspace", "frontend", "web applications"),
        "keywords": ("web", "frontend", "react", "workspace", "dashboard", "website"),
        "patterns": ("full-stack web workspace patterns", "React/TypeScript integration", "production web tooling"),
    },
    {
        "repository": "anthropics/financial-services",
        "license": "Apache-2.0",
        "domains": ("workflow", "connectors", "review", "specialists"),
        "keywords": ("workflow", "agent", "connector", "review", "approval", "research", "specialist"),
        "patterns": ("self-contained workflow agents", "connector boundaries", "human sign-off"),
    },
    {
        "repository": "rohitg00/ai-engineering-from-scratch",
        "license": "MIT",
        "domains": ("ai engineering", "agents", "mcp", "skills"),
        "keywords": ("agent", "mcp", "skill", "llm", "engineering", "testing", "evaluation"),
        "patterns": ("evidence-driven engineering", "agent loop foundations", "skills and MCP practices"),
    },
)


def matching_sources(goal: str) -> list[dict]:
    text = str(goal or "").casefold()
    terms = {x for x in text.replace("/", " ").replace("-", " ").split() if len(x) > 2}
    results = []
    for source in CURATED_SOURCES:
        hits = sum(1 for keyword in source["keywords"] if keyword in terms or keyword in text)
        if hits:
            item = dict(source)
            item["match_score"] = hits
            results.append(item)
    results.sort(key=lambda item: (item["match_score"], item["license"] in {"MIT", "Apache-2.0"}), reverse=True)
    return results
