"""Reusable efficiency policy for Brahma self-coding and self-healing.

This is guidance for autonomous engineering decisions, not permission to weaken
verification. Optimizations must preserve correctness, safety, and quality.
"""

from __future__ import annotations

EFFICIENCY_POLICY_VERSION = 1

EFFICIENCY_DIRECTIVE = """
EFFICIENCY-FIRST ENGINEERING POLICY:
- Measure or inspect the expensive step before optimizing it; do not optimize by guesswork.
- Reuse work when the relevant inputs are unchanged. Prefer exact content/configuration hashes,
  manifests, or other deterministic cache keys.
- Validate cache hits before trusting them. Stale, partial, incompatible, or corrupted caches
  must be rejected and rebuilt rather than silently used.
- Prefer incremental verification: run the smallest authoritative checks for a local change,
  then retain broader release gates when they protect system integrity.
- Do not repeat the same scan, archive traversal, compilation, or verification twice in one path
  unless the second pass has a distinct correctness purpose.
- Stream large files and archives instead of loading whole payloads into memory.
- Bound retries, polling, waits, and recovery loops while preserving diagnostics.
- Keep expensive optional work lazy and off the hot path until it is actually needed.
- Never cache API keys, tokens, credentials, private data, browser profiles, or user secrets.
- Keep cache invalidation conservative and tied to every input that can affect correctness.
- Optimize total user-visible latency and resource usage, not merely one local step.
- Never trade away correctness, security, safety, voice quality, feature behavior, or verification
  coverage merely to make a task faster.
- When performance changes materially, add a regression test or measurable guard proving equivalence.
- For autonomous repairs, make the smallest safe change first and reuse valid evidence; rerun
  affected verification when that evidence is invalidated.
""".strip()

def get_efficiency_directive(context: str = "") -> str:
    suffix = "\nContext: " + context.strip() if context and context.strip() else ""
    return EFFICIENCY_DIRECTIVE + suffix


__all__ = ["EFFICIENCY_POLICY_VERSION", "EFFICIENCY_DIRECTIVE", "get_efficiency_directive"]