"""Deterministic benchmark harness for real route comparisons."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

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
    error_type: str = ""


class EvaluationHarness:
    """Reports objective checks; does not pretend string matching proves intelligence."""
    def __init__(self, engine: ReasoningEngine) -> None:
        self.engine = engine

    def run(self, cases: Sequence[BenchmarkCase], *,
            system: str = "Answer accurately. State uncertainty; do not invent actions or evidence.",
            before_case: Callable[[BenchmarkCase], None] | None = None) -> tuple[BenchmarkResult, ...]:
        results = []
        for case in cases:
            if before_case:
                before_case(case)
            start = time.monotonic()
            try:
                answer = self.engine.answer(
                    case.prompt, system=system, strategy=case.strategy, complexity=case.complexity
                ).text
                lowered = answer.casefold()
                required = tuple(p for p in case.required_phrases if p.casefold() in lowered)
                forbidden = tuple(p for p in case.forbidden_phrases if p.casefold() in lowered)
                results.append(BenchmarkResult(
                    case.case_id, case.category,
                    len(required) == len(case.required_phrases) and not forbidden,
                    int((time.monotonic() - start) * 1000), required, forbidden,
                ))
            except Exception as exc:
                results.append(BenchmarkResult(
                    case.case_id, case.category, False,
                    int((time.monotonic() - start) * 1000), (), (), type(exc).__name__,
                ))
        return tuple(results)
