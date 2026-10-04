"""Astra Challenger benchmark harness for Brahma Evo.

The suite is intentionally small, deterministic, and versioned. It is designed
to compare systems under the same prompts and scoring rules. It does not claim
Astra parity unless both systems have been measured by the same suite.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import time
from typing import Callable, Iterable

from core.intellect_meter import record_benchmark

SUITE_ID = "brahma-astra-challenger-v1"
RESULTS_PATH = Path(__file__).resolve().parent.parent / "benchmarks" / "results.json"

@dataclass(frozen=True)
class ChallengeCase:
    case_id: str
    dimension: str
    prompt: str
    keywords: tuple[str, ...] = ()
    exact: str | None = None

CASES: tuple[ChallengeCase, ...] = (
    ChallengeCase(
        "reasoning-01", "reasoning",
        "What is the smallest positive integer divisible by every integer from 1 through 10?",
        exact="2520",
    ),
    ChallengeCase(
        "reasoning-02", "reasoning",
        "A box contains 3 red, 4 blue, and 5 green balls. What is the minimum number "
        "drawn without looking that guarantees two balls of the same color?",
        exact="4",
    ),
    ChallengeCase(
        "planning-01", "planning",
        "Design a safe 5-step plan for diagnosing a software failure without changing "
        "production state before evidence is collected.",
        keywords=("evidence", "reproduce", "logs", "rollback"),
    ),
    ChallengeCase(
        "coding-01", "coding",
        "Explain how to fix a flaky concurrent test without adding an arbitrary sleep.",
        keywords=("synchron", "event", "condition", "determin"),
    ),
    ChallengeCase(
        "research-01", "research",
        "When a factual claim may be outdated, describe a reliable verification strategy "
        "using primary sources and publication dates.",
        keywords=("primary", "date", "source"),
    ),
    ChallengeCase(
        "tool-use-01", "tool_use",
        "Given a task that requires current filesystem state, explain why inventing a "
        "result is inferior to inspecting the filesystem first.",
        keywords=("inspect", "filesystem", "actual", "verify"),
    ),
    ChallengeCase(
        "verification-01", "verification",
        "Why should a proposed code fix be run through tests and a runtime check before "
        "being declared successful?",
        keywords=("test", "runtime", "regression"),
    ),
    ChallengeCase(
        "multimodal-01", "multimodal",
        "For an image-analysis request, explain what additional evidence becomes available "
        "when the image itself is supplied instead of only a text description.",
        keywords=("image", "visual", "pixel"),
    ),
    ChallengeCase(
        "recovery-01", "recovery",
        "A desktop agent crashes after an update. Give a recovery sequence that prefers "
        "a known-good checkpoint and records the failure before retrying.",
        keywords=("checkpoint", "rollback", "log"),
    ),
    ChallengeCase(
        "efficiency-01", "efficiency",
        "How should a system make repeated expensive builds faster without reducing correctness?",
        keywords=("cache", "incremental", "reuse", "verify"),
    ),
)

def _score(case: ChallengeCase, answer: str) -> float:
    text = " ".join(str(answer or "").casefold().split())
    if case.exact is not None:
        return 100.0 if case.exact.casefold() in text else 0.0
    if not case.keywords:
        return 0.0
    hits = sum(1 for word in case.keywords if word.casefold() in text)
    return round(100.0 * hits / len(case.keywords), 2)


def _write_results(rows: list[dict]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "suite_id": SUITE_ID,
        "schema_version": 1,
        "results": rows,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run(
    solver: Callable[[str, str], str],
    *,
    system: str = "brahma",
    cases: Iterable[ChallengeCase] = CASES,
    persist_intellect: bool = True,
) -> dict:
    rows: list[dict] = []
    started = time.perf_counter()
    for case in cases:
        case_started = time.perf_counter()
        try:
            answer = str(solver(case.prompt, case.dimension) or "")
            score = _score(case, answer)
            error = ""
        except Exception as exc:
            answer = ""
            score = 0.0
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = round((time.perf_counter() - case_started) * 1000.0, 2)
        row = {
            **asdict(case),
            "keywords": list(case.keywords),
            "system": system,
            "score": score,
            "latency_ms": latency_ms,
            "answer_excerpt": answer[:500],
            "error": error,
            "suite_id": SUITE_ID,
        }
        rows.append(row)
        if persist_intellect:
            record_benchmark(
                suite=SUITE_ID,
                dimension=case.dimension,
                score=score,
                system=system,
                evidence="automated challenge case",
                metadata={"case_id": case.case_id, "latency_ms": latency_ms},
            )

    scores = [float(row["score"]) for row in rows]
    summary = {
        "suite_id": SUITE_ID,
        "system": system,
        "case_count": len(rows),
        "mean_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
        "passed_cases": sum(1 for score in scores if score >= 80.0),
        "total_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "rows": rows,
    }
    _write_results(rows)
    return summary


def compare(brahma: dict, astra: dict) -> dict:
    """Compare results only when case IDs and suite IDs are identical."""
    if brahma.get("suite_id") != astra.get("suite_id"):
        raise ValueError("Cannot compare different benchmark suites.")
    bmap = {row["case_id"]: row for row in brahma.get("rows", [])}
    amap = {row["case_id"]: row for row in astra.get("rows", [])}
    common = sorted(set(bmap) & set(amap))
    if not common:
        return {"suite_id": SUITE_ID, "comparison": "insufficient-data", "cases": []}
    deltas = []
    wins = losses = ties = 0
    for case_id in common:
        b = float(bmap[case_id]["score"])
        a = float(amap[case_id]["score"])
        delta = round(b - a, 2)
        deltas.append(delta)
        if delta > 0:
            wins += 1
        elif delta < 0:
            losses += 1
        else:
            ties += 1
    mean_delta = round(sum(deltas) / len(deltas), 2)
    return {
        "suite_id": SUITE_ID,
        "comparison": "brahma-beats-astra" if mean_delta > 0 else (
            "astra-beats-brahma" if mean_delta < 0 else "tie"
        ),
        "common_cases": len(common),
        "brahma_mean": round(sum(float(bmap[c]["score"]) for c in common) / len(common), 2),
        "astra_mean": round(sum(float(amap[c]["score"]) for c in common) / len(common), 2),
        "mean_delta": mean_delta,
        "wins": wins,
        "losses": losses,
        "ties": ties,
    }
