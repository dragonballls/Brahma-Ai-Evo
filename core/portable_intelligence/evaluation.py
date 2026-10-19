"""Reproducible route-comparison harness; phrase checks are only a smoke grader."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .engine import ReasoningEngine


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    category: str
    prompt: str
    required_phrases: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()
    strategy: str = "single"
    complexity: str = "normal"


@dataclass(frozen=True)
class BenchmarkResult:
    case_id: str
    category: str
    passed: bool
    elapsed_ms: int
    matched_required: tuple[str, ...]
    matched_forbidden: tuple[str, ...]
    answer_preview: str = ""
    selected_routes: tuple[str, ...] = ()
    model_call_count: int = 0
    estimated_cost_usd: float | None = None
    warnings: tuple[str, ...] = ()
    trace_summary: tuple[dict[str, Any], ...] = ()
    error_type: str = ""


def load_benchmark_cases(path: str | Path) -> tuple[BenchmarkCase, ...]:
    """Load checked-in evaluation cases and validate their public schema."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read benchmark cases: {type(exc).__name__}") from exc
    rows = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("Benchmark file must contain a non-empty cases array.")
    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every benchmark case must be an object.")
        case_id = str(row.get("case_id") or "").strip()
        category = str(row.get("category") or "").strip()
        prompt = str(row.get("prompt") or "").strip()
        if not case_id or not category or not prompt:
            raise ValueError("Every benchmark case needs case_id, category, and prompt.")
        if case_id in seen:
            raise ValueError(f"Duplicate benchmark case_id: {case_id}")
        seen.add(case_id)
        required = row.get("required_phrases", [])
        forbidden = row.get("forbidden_phrases", [])
        if not isinstance(required, list) or not isinstance(forbidden, list):
            raise ValueError(f"Phrase rules must be lists in case {case_id}.")
        strategy = str(row.get("strategy") or "single")
        complexity = str(row.get("complexity") or "normal")
        if strategy not in {"auto", "single", "ensemble"}:
            raise ValueError(f"Invalid strategy in case {case_id}.")
        if complexity not in {"simple", "normal", "complex"}:
            raise ValueError(f"Invalid complexity in case {case_id}.")
        cases.append(BenchmarkCase(
            case_id=case_id,
            category=category,
            prompt=prompt,
            required_phrases=tuple(str(x) for x in required),
            forbidden_phrases=tuple(str(x) for x in forbidden),
            strategy=strategy,
            complexity=complexity,
        ))
    return tuple(cases)


class EvaluationHarness:
    """Reports task outcomes and cost/latency traces without claiming IQ equivalence."""
    def __init__(self, engine: ReasoningEngine) -> None:
        self.engine = engine

    def run(
        self,
        cases: Sequence[BenchmarkCase],
        *,
        system: str = "Answer accurately. State uncertainty; do not invent actions or evidence.",
        before_case: Callable[[BenchmarkCase], None] | None = None,
        strategy_override: str | None = None,
        complexity_override: str | None = None,
    ) -> tuple[BenchmarkResult, ...]:
        results: list[BenchmarkResult] = []
        for case in cases:
            if before_case:
                before_case(case)
            start = time.monotonic()
            try:
                task = self.engine.answer(
                    case.prompt,
                    system=system,
                    strategy=strategy_override or case.strategy,
                    complexity=complexity_override or case.complexity,
                )
                answer = str(task.text or "")
                lowered = answer.casefold()
                required = tuple(p for p in case.required_phrases if p.casefold() in lowered)
                forbidden = tuple(p for p in case.forbidden_phrases if p.casefold() in lowered)
                traces = tuple(asdict(trace) for trace in task.traces)
                results.append(BenchmarkResult(
                    case_id=case.case_id,
                    category=case.category,
                    passed=len(required) == len(case.required_phrases) and not forbidden,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    matched_required=required,
                    matched_forbidden=forbidden,
                    answer_preview=answer[:2000],
                    selected_routes=tuple(task.selected_routes),
                    model_call_count=len(task.traces),
                    estimated_cost_usd=task.estimated_cost_usd,
                    warnings=tuple(task.warnings),
                    trace_summary=traces,
                ))
            except Exception as exc:
                results.append(BenchmarkResult(
                    case_id=case.case_id,
                    category=case.category,
                    passed=False,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    matched_required=(),
                    matched_forbidden=(),
                    error_type=type(exc).__name__,
                ))
        return tuple(results)
