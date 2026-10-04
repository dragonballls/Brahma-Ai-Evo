"""Agent-native software harness registry inspired by CLI-Anything.

Brahma does not vendor third-party applications. Instead, it keeps a lightweight
registry describing how a program can expose deterministic, inspectable actions.
The registry becomes context for universal tasks and future generated harnesses.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class HarnessSpec:
    name: str
    category: str
    command: str
    capabilities: tuple[str, ...]
    output: str = "json"
    source: str = "builtin"


BUILTIN_HARNESSES = (
    HarnessSpec("git", "development", "git", ("status", "diff", "log", "branch"), source="native"),
    HarnessSpec("python", "development", "python", ("run", "compile", "test"), source="native"),
    HarnessSpec("powershell", "system", "powershell", ("system_tasks", "processes", "files"), source="native"),
    HarnessSpec("ffmpeg", "media", "ffmpeg", ("transcode", "inspect", "extract"), source="optional"),
    HarnessSpec("gh", "development", "gh", ("github", "issues", "pull_requests", "actions"), source="optional"),
)


class SoftwareHarnessRegistry:
    def __init__(self) -> None:
        self._custom: list[HarnessSpec] = []

    def register(self, spec: HarnessSpec) -> None:
        if not spec.name.strip() or not spec.command.strip():
            raise ValueError("Harness name and command are required.")
        if not spec.capabilities:
            raise ValueError("A harness must expose at least one capability.")
        self._custom = [item for item in self._custom if item.name != spec.name]
        self._custom.append(spec)

    def discover(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for spec in (*BUILTIN_HARNESSES, *self._custom):
            executable = shutil.which(spec.command)
            results.append(
                {
                    **asdict(spec),
                    "available": bool(executable),
                    "resolved_path": executable or "",
                }
            )
        return results

    def match(self, request: str) -> list[dict[str, Any]]:
        terms = {x for x in request.casefold().split() if len(x) > 2}
        scored = []
        for item in self.discover():
            hay = {item["name"].casefold(), item["category"].casefold(), *(
                capability.casefold() for capability in item["capabilities"]
            )}
            score = len(terms & hay)
            if score or any(token in item["name"].casefold() for token in terms):
                scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored]

    def context_for(self, request: str, *, max_chars: int = 2600) -> str:
        matches = self.match(request)
        if not matches:
            matches = [item for item in self.discover() if item["available"]][:6]
        lines = ["AGENT-NATIVE SOFTWARE HARNESS CONTEXT"]
        for item in matches[:8]:
            state = "available" if item["available"] else "not-installed"
            lines.append(
                f"- {item['name']} ({item['category']}) [{state}] -> {item['command']} "
                f"capabilities={','.join(item['capabilities'])}; output={item['output']}"
            )
        return "\n".join(lines)[:max_chars]


software_harnesses = SoftwareHarnessRegistry()
