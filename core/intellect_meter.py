"""Measured Brahma capability/intellect reporting.

This module deliberately separates architectural capability from a verified
numeric intelligence score. A numeric score is only reported from recorded
benchmark evidence; it is never guessed from model names, API-key count, or
self-description.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir

SCHEMA_VERSION = 1
DIMENSION_WEIGHTS = {
    "reasoning": 0.16,
    "coding": 0.16,
    "research": 0.12,
    "planning": 0.12,
    "tool_use": 0.12,
    "verification": 0.12,
    "multimodal": 0.08,
    "recovery": 0.07,
    "efficiency": 0.05,
}
DIMENSION_LABELS = {
    "reasoning": "Reasoning",
    "coding": "Coding",
    "research": "Research",
    "planning": "Planning",
    "tool_use": "Tool use",
    "verification": "Verification",
    "multimodal": "Multimodal",
    "recovery": "Self-recovery",
    "efficiency": "Efficiency",
}

_DATA_PATH = get_user_data_dir() / "intelligence" / "intellect_benchmark.json"


@dataclass(frozen=True)
class IntellectSnapshot:
    score: float | None
    coverage: float
    measured_dimensions: tuple[str, ...]
    strongest: tuple[str, ...]
    weakest: tuple[str, ...]
    benchmark_count: int
    astra_comparison: str
    architecture_level: str

    @property
    def confidence_label(self) -> str:
        if self.coverage >= 0.90:
            return "high"
        if self.coverage >= 0.60:
            return "medium"
        if self.coverage > 0:
            return "low"
        return "none"


def is_intellect_query(prompt: str) -> bool:
    text = " ".join(str(prompt or "").casefold().split())
    phrases = (
        "intellect level",
        "what is your intellect",
        "intellect",
        "intelligence level",
        "how intelligent are you",
        "how smart are you",
        "how smart is brahma",
        "how intelligent is brahma",
        "capability score",
        "intelligence score",
        "cognitive score",
        "how capable are you",
        "how capable is brahma",
    )
    return any(p in text for p in phrases)


def _read_store() -> dict[str, Any]:
    try:
        if _DATA_PATH.is_file():
            payload = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {"schema_version": SCHEMA_VERSION, "benchmarks": []}


def _benchmarks() -> list[dict[str, Any]]:
    rows = _read_store().get("benchmarks", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _score_value(row: dict[str, Any]) -> float | None:
    try:
        value = float(row.get("score"))
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 100:
        return None
    return value


def _latest_by_dimension(system: str = "brahma") -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    target = str(system or "").strip().casefold()
    for row in _benchmarks():
        row_system = str(row.get("system", "brahma")).strip().casefold()
        if row_system != target:
            continue
        dimension = str(row.get("dimension", "")).strip().casefold()
        score = _score_value(row)
        if dimension not in DIMENSION_WEIGHTS or score is None:
            continue
        previous = latest.get(dimension)
        if previous is None:
            latest[dimension] = row
            continue
        latest_ts = str(previous.get("timestamp", ""))
        row_ts = str(row.get("timestamp", ""))
        if row_ts >= latest_ts:
            latest[dimension] = row
    return latest


def snapshot() -> IntellectSnapshot:
    latest = _latest_by_dimension("brahma")
    measured = tuple(sorted(latest))
    total_weight = sum(DIMENSION_WEIGHTS.values())
    measured_weight = sum(DIMENSION_WEIGHTS[d] for d in measured)
    coverage = (measured_weight / total_weight) if total_weight else 0.0

    score = None
    if latest and measured_weight:
        score = sum(
            float(_score_value(latest[d])) * DIMENSION_WEIGHTS[d]
            for d in measured
        ) / measured_weight

    ranked = sorted(
        ((d, float(_score_value(row))) for d, row in latest.items()),
        key=lambda item: item[1],
    )
    weakest = tuple(DIMENSION_LABELS[d] for d, _ in ranked[:2])
    strongest = tuple(DIMENSION_LABELS[d] for d, _ in reversed(ranked[-2:]))

    # A comparison to Astra is only valid when the same benchmark suite contains
    # a recorded Astra result. Public headline scores are heterogeneous, so this
    # module never fabricates a cross-benchmark "Astra percentage."
    astra_rows = [
        row for row in _benchmarks()
        if str(row.get("system", "")).strip().casefold() in {
            "gpt-6-astra", "astra", "openai/gpt-6-astra"
        }
        and _score_value(row) is not None
        and str(row.get("suite", "")).strip()
    ]
    astra_comparison = "not established"
    if astra_rows and latest:
        ratios: list[float] = []
        for row in astra_rows:
            dim = str(row.get("dimension", "")).strip().casefold()
            suite = str(row.get("suite", "")).strip().casefold()
            candidates = [
                item for item in _benchmarks()
                if str(item.get("system", "")).strip().casefold() == "brahma"
                and str(item.get("dimension", "")).strip().casefold() == dim
                and str(item.get("suite", "")).strip().casefold() == suite
            ]
            brahma_score = _score_value(sorted(
                candidates,
                key=lambda item: str(item.get("timestamp", "")),
            )[-1]) if candidates else None
            astra_score = _score_value(row)
            if astra_score is not None and brahma_score is not None and astra_score > 0:
                ratios.append((brahma_score / astra_score) * 100.0)
        if ratios:
            astra_comparison = f"{sum(ratios) / len(ratios):.1f}% of recorded Astra baseline"

    return IntellectSnapshot(
        score=score,
        coverage=coverage,
        measured_dimensions=measured,
        strongest=strongest,
        weakest=weakest,
        benchmark_count=len(_benchmarks()),
        astra_comparison=astra_comparison,
        architecture_level="Apex Cognitive Mesh",
    )


def format_status() -> str:
    state = snapshot()
    coverage_pct = state.coverage * 100.0
    if state.score is None:
        score_line = "Verified intelligence score: UNMEASURED"
        evidence_line = (
            f"Benchmark coverage: {coverage_pct:.0f}% "
            f"({len(state.measured_dimensions)}/{len(DIMENSION_WEIGHTS)} capability domains)."
        )
        comparison = (
            "GPT-6 Astra parity: not established yet; "
            "no like-for-like benchmark evidence is recorded."
        )
    else:
        score_line = f"Brahma Intelligence Index: {state.score:.1f}/100"
        evidence_line = (
            f"Benchmark coverage: {coverage_pct:.0f}% • evidence confidence: "
            f"{state.confidence_label} • benchmark records: {state.benchmark_count}."
        )
        comparison = f"GPT-6 Astra comparison: {state.astra_comparison}."

    strongest = ", ".join(state.strongest) if state.strongest else "not measured"
    weakest = ", ".join(state.weakest) if state.weakest else "not measured"
    return (
        f"Brahma capability level: {state.architecture_level}.\n"
        f"{score_line}.\n"
        f"{evidence_line}\n"
        f"Strongest measured domains: {strongest}.\n"
        f"Least-tested domains: {weakest}.\n"
        f"{comparison}\n"
        "This is a benchmark-based system capability score, not an IQ score."
    )


def answer_intellect_query(prompt: str) -> str | None:
    if not is_intellect_query(prompt):
        return None
    return format_status()


def record_benchmark(
    *,
    suite: str,
    dimension: str,
    score: float,
    system: str = "brahma",
    evidence: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist one benchmark observation for later capability scoring."""
    dimension = str(dimension).strip().casefold()
    score = float(score)
    if dimension not in DIMENSION_WEIGHTS:
        raise ValueError(f"Unknown intellect dimension: {dimension}")
    if not 0 <= score <= 100:
        raise ValueError("Benchmark score must be between 0 and 100.")
    data = _read_store()
    rows = data.setdefault("benchmarks", [])
    rows.append({
        "suite": str(suite).strip() or "unnamed",
        "dimension": dimension,
        "score": score,
        "system": str(system).strip() or "brahma",
        "evidence": str(evidence or "").strip(),
        "metadata": dict(metadata or {}),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    data["schema_version"] = SCHEMA_VERSION
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DATA_PATH.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )
