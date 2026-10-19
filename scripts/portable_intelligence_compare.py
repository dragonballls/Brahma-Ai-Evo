"""Opt-in blind comparison of a direct Astra baseline versus a portable ensemble.

This script makes paid provider requests only when --confirm-live-calls is present.
API keys are read from environment variables; never put secrets in the JSON config.
The pairwise LLM judge is heuristic feedback, not ground truth. Review reports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.portable_intelligence.contracts import Route, RunPolicy
from core.portable_intelligence.engine import ReasoningEngine
from core.portable_intelligence.evaluation import (
    BenchmarkCase,
    EvaluationHarness,
    build_pairwise_judge_request,
    judge_pair_blindly,
    load_benchmark_cases,
)
from core.portable_intelligence.key_pool import (
    CredentialPoolState, build_credential_pools, load_credentials_from_environment, _normalize_endpoint,
)
from scripts.portable_intelligence_benchmark import ADAPTERS, _route_from_config




def _comparison_call_limits(
    total_calls: int,
    max_candidates: int,
    available_candidate_routes: int,
    max_critics: int = 0,
) -> tuple[int, int]:
    """Return (ensemble_call_budget, candidate_budget) inside a total per-case cap.

    Two calls are reserved for the direct baseline and independent blind judge.
    At least two independent candidates and synthesis are reserved, plus a critic
    call when the configured budget allows one.
    """
    if total_calls < 5:
        raise ValueError("A comparison needs at least five calls: baseline, two candidates, synthesis, and judge.")
    if max_candidates < 2 or available_candidate_routes < 2 or max_critics < 0:
        raise ValueError("At least two candidate routes/slots and a non-negative critic limit are required.")
    ensemble_budget = total_calls - 2
    reserved_critics = min(max_critics, max(0, ensemble_budget - 3))
    candidate_budget = min(
        max_candidates,
        available_candidate_routes,
        ensemble_budget - 1 - reserved_critics,
    )
    if candidate_budget < 2:
        raise ValueError("The total call budget is too small to leave two candidates and a synthesis call.")
    return ensemble_budget, candidate_budget



def _route_diversity_group(route: Route, providers: Mapping[str, Any]) -> str:
    provider = providers[route.provider_id]
    return str(route.diversity_group or getattr(provider, "diversity_group", route.provider_id) or route.provider_id)


def _independent_candidate_routes(
    routes: Mapping[str, Route],
    providers: Mapping[str, Any],
    *,
    baseline_id: str,
    judge_id: str,
) -> tuple[Route, ...]:
    """Exclude baseline/judge and any aliases of their pool or diversity groups."""
    baseline_provider = providers[baseline_id]
    judge_provider = providers[judge_id]
    blocked_object_ids = {id(baseline_provider), id(judge_provider)}
    blocked_groups = {
        _route_diversity_group(routes[baseline_id], providers),
        _route_diversity_group(routes[judge_id], providers),
    }
    result = []
    for route_id, route in routes.items():
        if route_id in {baseline_id, judge_id}:
            continue
        if id(providers[route_id]) in blocked_object_ids:
            continue
        if _route_diversity_group(route, providers) in blocked_groups:
            continue
        result.append(route)
    return tuple(result)

def _load_routes(config_path: Path) -> tuple[dict[str, Any], dict[str, Route], dict[str, dict[str, Any]]]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read provider config: {type(exc).__name__}") from exc
    if not isinstance(config, dict):
        raise ValueError("Provider config must be a JSON object.")

    rows = config.get("providers")
    if isinstance(rows, list):
        # Existing simple config: one environment key per logical provider.
        providers: dict[str, Any] = {}
        routes: dict[str, Route] = {}
        skipped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Every provider entry must be an object.")
            entry_id = str(row.get("id") or "").strip()
            if not entry_id or entry_id in providers or entry_id in skipped:
                raise ValueError("Provider IDs must be non-empty and unique.")
            try:
                built = _route_from_config(row)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid provider entry {entry_id}: {type(exc).__name__}") from exc
            if built is None:
                skipped[entry_id] = {"reason": "missing API-key environment variable or model"}
                continue
            provider_id, provider, route = built
            if provider_id != entry_id:
                raise ValueError("Provider loader returned an unexpected ID.")
            providers[provider_id] = provider
            routes[provider_id] = route
        return providers, routes, skipped

    credential_rows = config.get("credentials")
    route_rows = config.get("routes")
    if not isinstance(credential_rows, list) or not isinstance(route_rows, list):
        raise ValueError("Provider config must contain either providers[] or credentials[] plus routes[].")
    loaded = load_credentials_from_environment(credential_rows)
    pool_state = CredentialPoolState()
    try:
        pools = build_credential_pools(loaded.credentials, state=pool_state)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Credential pool configuration is invalid: {type(exc).__name__}") from exc

    pool_index = {
        (pool.family, _normalize_endpoint(pool.base_url)): pool
        for pool in pools.values()
    }
    providers = {}
    routes = {}
    skipped = {
        str(row.get("credential_id") or "unknown credential"): {
            "reason": str(row.get("reason") or "credential not active"),
            "api_key_env": str(row.get("api_key_env") or ""),
        }
        for row in loaded.skipped
    }
    for row in route_rows:
        if not isinstance(row, dict):
            raise ValueError("Every model route must be an object.")
        route_id = str(row.get("id") or "").strip()
        family = str(row.get("provider_family") or "").strip().casefold()
        base_url = str(row.get("base_url") or "").strip()
        model_env = str(row.get("model_env") or "").strip()
        model = str(os.environ.get(model_env, row.get("model") or "") if model_env else row.get("model") or "").strip()
        if not route_id or route_id in routes or route_id in skipped:
            raise ValueError("Model route IDs must be non-empty and unique.")
        if not family or not base_url:
            raise ValueError(f"Model route {route_id} must specify provider_family and base_url.")
        if not model or model.startswith(("YOUR_", "REPLACE_")):
            skipped[route_id] = {"reason": "model environment variable missing or model is a placeholder"}
            continue
        pool = pool_index.get((family, _normalize_endpoint(base_url)))
        if pool is None:
            skipped[route_id] = {"reason": "no configured credential pool for this provider family/endpoint"}
            continue
        pricing = row.get("pricing") or {}
        route = Route(
            provider_id=route_id,
            model=model,
            priority=int(row.get("priority", 100)),
            capabilities=frozenset(row.get("capabilities") or getattr(pool, "capabilities", {"text"})),
            input_usd_per_million=pricing.get("input_usd_per_million"),
            output_usd_per_million=pricing.get("output_usd_per_million"),
            diversity_group=str(row.get("diversity_group") or pool.diversity_group),
        )
        # Several logical model routes may share a pool object. Key rotation and quota
        # cooldown are still centralized inside that pool; model choice remains route-owned.
        providers[route_id] = pool
        routes[route_id] = route
    return providers, routes, skipped


def _task_result(engine: ReasoningEngine, case: BenchmarkCase, strategy: str, complexity: str):
    return EvaluationHarness(engine).run(
        [case], strategy_override=strategy, complexity_override=complexity
    )[0]


def _result_dict(result) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "category": result.category,
        "passed_smoke_checks": result.passed,
        "elapsed_ms": result.elapsed_ms,
        "matched_required": list(result.matched_required),
        "matched_forbidden": list(result.matched_forbidden),
        "answer_text": result.answer_preview[:12000],
        "selected_routes": list(result.selected_routes),
        "model_call_count": result.model_call_count,
        "estimated_cost_usd": result.estimated_cost_usd,
        "warnings": list(result.warnings),
        "trace_summary": list(result.trace_summary),
        "error_type": result.error_type,
    }


def _randomized_order(case_id: str) -> bool:
    # Stable shuffle for reproducibility without always putting the baseline first.
    return hashlib.sha256(case_id.encode("utf-8")).digest()[0] % 2 == 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "benchmarks" / "provider_routes.example.json")
    parser.add_argument("--cases", type=Path, default=ROOT / "benchmarks" / "portable_intelligence_cases.json")
    parser.add_argument("--baseline-provider-id", default="openai_astra",
                        help="Direct, single-model baseline route ID; usually the GPT-6 Astra route.")
    parser.add_argument("--judge-provider-id", required=True,
                        help="A third, distinct provider used for blinded pairwise rubric scoring.")
    parser.add_argument("--case-limit", type=int, default=4,
                        help="Maximum cases to run; 0 means all checked-in cases.")
    parser.add_argument("--max-calls", type=int, default=6,
                        help="Maximum total model calls per case, including baseline, ensemble candidates/review/synthesis, and blind judge. Minimum 5.")
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--max-critics", type=int, default=1)
    parser.add_argument("--max-estimated-cost-usd", type=float, default=None,
                        help="Optional hard ceiling on estimated total cost; all active routes need configured prices.")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--confirm-live-calls", action="store_true",
                        help="Required acknowledgement: this sends real provider requests and may incur charges.")
    args = parser.parse_args(argv)
    if not args.confirm_live_calls:
        parser.error("Add --confirm-live-calls only when ready to make real provider requests.")
    if args.case_limit < 0 or args.max_calls < 5 or args.max_candidates < 2 or args.max_critics < 0:
        parser.error("Invalid case/call limits; comparison requires baseline, two candidates, synthesis, and judge (at least five calls).")
    if args.max_estimated_cost_usd is not None and args.max_estimated_cost_usd < 0:
        parser.error("--max-estimated-cost-usd cannot be negative.")
    try:
        cases = load_benchmark_cases(args.cases)
        providers, routes, skipped = _load_routes(args.config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    baseline_id = str(args.baseline_provider_id).strip()
    judge_id = str(args.judge_provider_id).strip()
    if baseline_id == judge_id:
        print("Baseline and judge must use different provider IDs.", file=sys.stderr)
        return 2
    if baseline_id not in routes:
        print(f"Baseline route {baseline_id!r} is not configured with a key and model.", file=sys.stderr)
        print("Configured active routes: " + ", ".join(sorted(routes)), file=sys.stderr)
        return 2
    if judge_id not in routes:
        print(f"Judge route {judge_id!r} is not configured with a key and model.", file=sys.stderr)
        print("Configured active routes: " + ", ".join(sorted(routes)), file=sys.stderr)
        return 2

    candidate_routes = _independent_candidate_routes(
        routes, providers, baseline_id=baseline_id, judge_id=judge_id
    )
    candidate_provider_ids = {route.provider_id for route in candidate_routes}
    candidate_providers = {
        provider_id: providers[provider_id] for provider_id in candidate_provider_ids
    }
    candidate_diversity = {
        str(route.diversity_group or getattr(candidate_providers[route.provider_id], "diversity_group", route.provider_id))
        for route in candidate_routes
    }
    if len(candidate_diversity) < 2:
        print(
            "At least two additional candidate diversity groups must be configured besides baseline and judge.",
            file=sys.stderr,
        )
        return 2
    if not candidate_routes:
        print("No candidate routes are active.", file=sys.stderr)
        return 2

    try:
        ensemble_call_budget, candidate_call_budget = _comparison_call_limits(
            args.max_calls, args.max_candidates, len(candidate_routes), args.max_critics
        )
    except ValueError as exc:
        print(f"Comparison call budget invalid: {exc}", file=sys.stderr)
        return 2

    baseline_route = routes[baseline_id]
    judge_route = routes[judge_id]
    if args.max_estimated_cost_usd is not None:
        active_routes = [baseline_route, judge_route, *candidate_routes]
        missing_prices = [
            route.provider_id for route in active_routes
            if route.input_usd_per_million is None or route.output_usd_per_million is None
        ]
        if missing_prices:
            print(
                "A hard spend ceiling needs input/output token prices for every active route: "
                + ", ".join(missing_prices),
                file=sys.stderr,
            )
            return 2

    selected_cases = cases if args.case_limit == 0 else cases[:args.case_limit]
    spend = 0.0
    calls = 0
    report_cases: list[dict[str, Any]] = []
    budget_stopped = False
    for case in selected_cases:
        record: dict[str, Any] = {
            "case_id": case.case_id,
            "category": case.category,
            "prompt": case.prompt,
        }
        remaining = None
        if args.max_estimated_cost_usd is not None:
            remaining = max(0.0, args.max_estimated_cost_usd - spend)
            if remaining <= 0:
                budget_stopped = True
                record["status"] = "stopped_before_case_budget_exhausted"
                report_cases.append(record)
                break

        # Direct single-model baseline: exactly one route and one model call.
        baseline_engine = ReasoningEngine(
            {baseline_id: providers[baseline_id]},
            (baseline_route,),
            policy=RunPolicy(strategy="single", max_total_model_calls=1, max_parallel_calls=1,
                             max_tool_rounds=0, max_tool_calls=0, max_cost_usd=remaining, retry_attempts=1),
        )
        baseline_result = _task_result(baseline_engine, case, "single", case.complexity)
        record["baseline"] = _result_dict(baseline_result)
        calls += baseline_result.model_call_count
        if baseline_result.estimated_cost_usd is not None:
            spend += baseline_result.estimated_cost_usd
        if baseline_result.error_type:
            record["baseline_error"] = baseline_result.error_type

        remaining = None
        if args.max_estimated_cost_usd is not None:
            remaining = max(0.0, args.max_estimated_cost_usd - spend)
        if remaining is not None and remaining <= 0:
            budget_stopped = True
            record["status"] = "budget_exhausted_after_baseline"
            report_cases.append(record)
            break

        # Run the ensemble without the baseline or judge route. Each candidate
        # provider contributes at most one independent candidate in ReasoningEngine.
        ensemble_engine = ReasoningEngine(
            candidate_providers,
            candidate_routes,
            policy=RunPolicy(
                strategy="ensemble",
                max_candidate_calls=candidate_call_budget,
                max_critic_calls=args.max_critics,
                max_total_model_calls=ensemble_call_budget,
                max_parallel_calls=max(1, min(3, len(candidate_routes))),
                max_tool_rounds=0,
                max_tool_calls=0,
                max_cost_usd=remaining,
                retry_attempts=1,
            ),
        )
        ensemble_result = _task_result(ensemble_engine, case, "ensemble", "complex")
        record["ensemble"] = _result_dict(ensemble_result)
        calls += ensemble_result.model_call_count
        if ensemble_result.estimated_cost_usd is not None:
            spend += ensemble_result.estimated_cost_usd
        if ensemble_result.error_type:
            record["ensemble_error"] = ensemble_result.error_type

        baseline_answer = baseline_result.answer_text
        ensemble_answer = ensemble_result.answer_text
        swap = _randomized_order(case.case_id)
        answer_a, answer_b = (ensemble_answer, baseline_answer) if swap else (baseline_answer, ensemble_answer)
        mapping = {"A": "ensemble" if swap else "baseline", "B": "baseline" if swap else "ensemble"}
        judge_request = build_pairwise_judge_request(case, answer_a, answer_b)
        estimate = judge_route.estimate_cost(judge_request)
        if args.max_estimated_cost_usd is not None:
            if estimate is None:
                record["judge"] = {"error": "judge route cost is unpriced; stopped before request"}
                budget_stopped = True
                report_cases.append(record)
                break
            if spend + estimate > args.max_estimated_cost_usd:
                record["judge"] = {"error": "estimated spend ceiling would be exceeded; judge call skipped"}
                budget_stopped = True
                record["status"] = "budget_exhausted_before_judge"
                report_cases.append(record)
                break
        try:
            judgment = judge_pair_blindly(providers[judge_id], judge_route.model, case, answer_a, answer_b)
            calls += 1
            if estimate is not None:
                spend += estimate
            winner = "tie" if judgment.winner == "TIE" else mapping[judgment.winner]
            record["judge"] = {
                "provider_id": judge_id,
                "model": judgment.judge_model,
                "winner": winner,
                "scores_baseline": judgment.scores_b if mapping["B"] == "baseline" else judgment.scores_a,
                "scores_ensemble": judgment.scores_b if mapping["B"] == "ensemble" else judgment.scores_a,
                "rationale": judgment.rationale,
                "note": "Blind model-based scoring is subjective feedback, not ground truth.",
                "estimated_cost_usd": estimate,
            }
            record["status"] = "judged"
        except Exception as exc:
            record["judge"] = {"provider_id": judge_id, "error": type(exc).__name__}
            record["status"] = "judge_failed"
        report_cases.append(record)
        if budget_stopped:
            break

    judged = [row for row in report_cases if isinstance(row.get("judge"), dict) and row["judge"].get("winner") in {"baseline", "ensemble", "tie"}]
    report = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Controlled comparison of a direct single-model baseline and a provider-diverse ensemble.",
        "baseline": {"provider_id": baseline_id, "model": baseline_route.model},
        "ensemble_routes": [f"{route.provider_id}/{route.model}" for route in candidate_routes],
        "judge": {"provider_id": judge_id, "model": judge_route.model},
        "cases_requested": [case.case_id for case in selected_cases],
        "cases_reported": len(report_cases),
        "cases_judged": len(judged),
        "judge_wins": {
            "baseline": sum(1 for row in judged if row["judge"]["winner"] == "baseline"),
            "ensemble": sum(1 for row in judged if row["judge"]["winner"] == "ensemble"),
            "tie": sum(1 for row in judged if row["judge"]["winner"] == "tie"),
        },
        "model_calls_reported": calls,
        "max_total_model_calls_per_case": args.max_calls,
        "ensemble_call_budget_per_case": ensemble_call_budget,
        "candidate_call_budget_per_case": candidate_call_budget,
        "estimated_cost_usd_sum": round(spend, 8) if spend else None,
        "estimated_cost_is_not_actual_billing": True,
        "budget_stopped": budget_stopped,
        "skipped_providers": skipped,
        "limitations": [
            "The checked-in cases are a small smoke set, not a validated intelligence benchmark.",
            "The pairwise judge is a model and can be biased or wrong; human review remains necessary.",
            "Use matched context, tools, permissions, model snapshots, and multiple runs before drawing general conclusions.",
            "A phrase-check pass is not proof of correctness or GPT-6 Astra parity.",
        ],
        "cases": report_cases,
    }
    output = args.output or ROOT / "reports" / f"portable-intelligence-compare-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write report: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"Report: {output}")
    print(
        f"Pairwise judge counts — baseline: {report['judge_wins']['baseline']}, "
        f"ensemble: {report['judge_wins']['ensemble']}, tie: {report['judge_wins']['tie']}"
    )
    print(f"Model calls reported: {calls}")
    print("The judge is not ground truth; inspect answers and use repeated benchmark runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
