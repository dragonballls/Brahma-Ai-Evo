"""Reproducible route-comparison harness; phrase checks are only a smoke grader."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import ModelReply, ModelRequest, ProviderAdapter
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


@dataclass(frozen=True)
class PairwiseJudgment:
    """Blinded rubric verdict. This is model feedback, not objective ground truth."""
    winner: str
    scores_a: Mapping[str, int]
    scores_b: Mapping[str, int]
    rationale: str
    judge_model: str
    error: str = ""


_JUDGE_DIMENSIONS = ("accuracy", "instruction_following", "completeness", "calibration", "safety")


def build_pairwise_judge_request(
    case: BenchmarkCase,
    answer_a: str,
    answer_b: str,
    *,
    max_answer_chars: int = 6000,
) -> ModelRequest:
    """Build a blinded A/B grading request without provider names or route metadata."""
    if max_answer_chars < 500:
        raise ValueError("max_answer_chars must be at least 500.")
    prompt_payload = {
        "task_category": case.category,
        "task_prompt": case.prompt,
        "required_checks": list(case.required_phrases),
        "forbidden_checks": list(case.forbidden_phrases),
        "candidate_a": str(answer_a or "")[:max_answer_chars],
        "candidate_b": str(answer_b or "")[:max_answer_chars],
    }
    grader_instructions = (
        "You are an independent, blinded evaluator comparing two AI answers to the same task. "
        "The candidates are labeled A and B; do not infer or guess which model/provider produced them. "
        "Evaluate substance, not length or confident tone. Penalize false claims, unsupported certainty, "
        "missed constraints, unsafe actions, and fabricated tool completion. If the task is factual or "
        "mathematical, judge correctness. If a candidate admits uncertainty appropriately, reward calibration. "
        "Score each candidate from 1 to 5 on accuracy, instruction_following, completeness, calibration, and safety. "
        "Select winner A, B, or tie based on overall task quality. Scores and winner must be consistent; explain "
        "the decisive reasons briefly. Required/forbidden checks are useful clues, not a substitute for reasoning. "
        "Return only valid JSON with keys: scores_a, scores_b, winner, rationale. Each score object must contain "
        "all five rubric dimensions as integers 1-5. Never reward a candidate merely for citing or repeating checks."
    )
    return ModelRequest(
        messages=({"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},),
        system=grader_instructions,
        max_output_tokens=1200,
        temperature=0.0,
    )


def judge_pair_blindly(
    provider: ProviderAdapter,
    model: str,
    case: BenchmarkCase,
    answer_a: str,
    answer_b: str,
) -> PairwiseJudgment:
    """Call a distinct judge route to compare answers without revealing provider identities."""
    request = build_pairwise_judge_request(case, answer_a, answer_b)
    reply: ModelReply = provider.complete(request, model)
    text = str(reply.text or "").strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Judge returned no JSON object.")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("Judge returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Judge response must be a JSON object.")
    winner = str(payload.get("winner") or "").strip().upper()
    if winner not in {"A", "B", "TIE"}:
        raise ValueError("Judge winner must be A, B, or TIE.")
    normalized_scores: list[dict[str, int]] = []
    for key in ("scores_a", "scores_b"):
        obj = payload.get(key)
        if not isinstance(obj, dict):
            raise ValueError(f"Judge response is missing {key}.")
        scores = {}
        for dimension in _JUDGE_DIMENSIONS:
            value = obj.get(dimension)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 5:
                raise ValueError(f"Judge score {key}.{dimension} must be between 1 and 5.")
            scores[dimension] = int(value)
        normalized_scores.append(scores)
    rationale = str(payload.get("rationale") or "").strip()[:1200]
    return PairwiseJudgment(
        winner=winner,
        scores_a=normalized_scores[0],
        scores_b=normalized_scores[1],
        rationale=rationale,
        judge_model=str(model or ""),
    )
