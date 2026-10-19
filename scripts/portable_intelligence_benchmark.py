"""Opt-in, local benchmark runner for configured real provider routes.

This script never reads API keys from JSON or logs them. It requires an explicit
--confirm-live-calls flag because model calls can incur provider charges.
"""
from __future__ import annotations

import argparse
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
from core.portable_intelligence.evaluation import EvaluationHarness, load_benchmark_cases
from core.portable_intelligence.providers import (
    AnthropicMessagesProvider,
    GeminiContentProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
)

ADAPTERS = {
    "openai_responses": OpenAIResponsesProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic_messages": AnthropicMessagesProvider,
    "gemini_generate_content": GeminiContentProvider,
}


def _route_from_config(row: dict[str, Any]) -> tuple[str, Any, Route] | None:
    provider_id = str(row.get("id") or "").strip()
    adapter_kind = str(row.get("adapter") or "").strip()
    key_env = str(row.get("api_key_env") or "").strip()
    model_env = str(row.get("model_env") or "").strip()
    model = str(os.environ.get(model_env, row.get("model") or "")).strip() if model_env else str(row.get("model") or "").strip()
    api_key = os.environ.get(key_env, "").strip() if key_env else ""
    if not provider_id or adapter_kind not in ADAPTERS:
        raise ValueError("Each provider needs a unique id and a supported adapter kind.")
    if not api_key:
        return None
    if not model or model.startswith(("YOUR_", "REPLACE_")):
        return None
    cls = ADAPTERS[adapter_kind]
    kwargs: dict[str, Any] = {}
    if row.get("base_url"):
        kwargs["base_url"] = str(row["base_url"])
    if adapter_kind == "openai_compatible" and "base_url" not in kwargs:
        raise ValueError(f"Provider {provider_id} requires base_url.")
    provider = cls(api_key, **kwargs)
    pricing = row.get("pricing") or {}
    route = Route(
        provider_id=provider_id,
        model=model,
        priority=int(row.get("priority", 100)),
        capabilities=frozenset(row.get("capabilities") or getattr(provider, "capabilities", {"text"})),
        input_usd_per_million=pricing.get("input_usd_per_million"),
        output_usd_per_million=pricing.get("output_usd_per_million"),
        diversity_group=str(row.get("diversity_group") or provider_id),
    )
    return provider_id, provider, route


def _run_case(
    providers: dict[str, Any],
    routes: tuple[Route, ...],
    case,
    *,
    mode: str,
    max_calls: int,
    max_candidates: int,
    max_critics: int,
    max_estimated_cost_usd: float | None,
):
    policy = RunPolicy(
        strategy="single" if mode == "each" else "ensemble",
        max_candidate_calls=max(1, min(max_candidates, len(routes))),
        max_critic_calls=max_critics,
        max_total_model_calls=1 if mode == "each" else max_calls,
        max_parallel_calls=1 if mode == "each" else max(1, min(3, len(routes))),
        max_tool_rounds=0,
        max_tool_calls=0,
        max_cost_usd=max_estimated_cost_usd,
        retry_attempts=1,
    )
    engine = ReasoningEngine(providers, routes, policy=policy)
    return EvaluationHarness(engine).run(
        [case],
        strategy_override="single" if mode == "each" else "ensemble",
        complexity_override="complex" if mode == "ensemble" else None,
    )[0]


def _serialize(result) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "category": result.category,
        "passed": result.passed,
        "elapsed_ms": result.elapsed_ms,
        "matched_required": list(result.matched_required),
        "matched_forbidden": list(result.matched_forbidden),
        "answer_preview": result.answer_preview,
        "selected_routes": list(result.selected_routes),
        "model_call_count": result.model_call_count,
        "estimated_cost_usd": result.estimated_cost_usd,
        "warnings": list(result.warnings),
        "trace_summary": list(result.trace_summary),
        "error_type": result.error_type,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "benchmarks" / "provider_routes.example.json")
    parser.add_argument("--cases", type=Path, default=ROOT / "benchmarks" / "portable_intelligence_cases.json")
    parser.add_argument("--mode", choices=("each", "ensemble"), default="each",
                        help="Run each configured provider separately or test the multi-provider ensemble.")
    parser.add_argument("--case-limit", type=int, default=4,
                        help="Maximum cases to run; use 0 to run the full case set. Default 4 to limit calls.")
    parser.add_argument("--max-calls", type=int, default=6,
                        help="Total model-call cap per ensemble case.")
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--max-critics", type=int, default=2)
    parser.add_argument("--max-estimated-cost-usd", type=float, default=None,
                        help="Optional estimated-spend preflight ceiling; every route needs prices for this.")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--confirm-live-calls", action="store_true",
                        help="Required acknowledgement: this runner sends real API requests and may incur charges.")
    args = parser.parse_args(argv)

    if not args.confirm_live_calls:
        parser.error("Add --confirm-live-calls only when you are ready to send real provider requests.")
    if args.case_limit < 0 or args.max_calls < 1 or args.max_candidates < 1 or args.max_critics < 0:
        parser.error("Call limits and case-limit are invalid.")
    if args.max_estimated_cost_usd is not None and args.max_estimated_cost_usd < 0:
        parser.error("--max-estimated-cost-usd cannot be negative.")

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        cases = load_benchmark_cases(args.cases)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Configuration error: {type(exc).__name__}", file=sys.stderr)
        return 2
    rows = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(rows, list):
        print("Configuration error: providers must be a JSON array.", file=sys.stderr)
        return 2

    providers: dict[str, Any] = {}
    routes: list[Route] = []
    skipped: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            print("Configuration error: each provider entry must be an object.", file=sys.stderr)
            return 2
        try:
            built = _route_from_config(row)
        except (TypeError, ValueError) as exc:
            print(f"Provider configuration error: {type(exc).__name__}", file=sys.stderr)
            return 2
        if built is None:
            skipped.append(str(row.get("id") or "unknown provider"))
            continue
        provider_id, provider, route = built
        if provider_id in providers:
            print(f"Configuration error: duplicate provider id {provider_id}.", file=sys.stderr)
            return 2
        providers[provider_id] = provider
        routes.append(route)
    if not routes:
        print("No complete provider routes found. Set each provider's API-key environment variable and model.", file=sys.stderr)
        print("Skipped routes: " + ", ".join(skipped), file=sys.stderr)
        return 2
    if args.max_estimated_cost_usd is not None:
        missing_prices = [
            route.provider_id for route in routes
            if route.input_usd_per_million is None or route.output_usd_per_million is None
        ]
        if missing_prices:
            print(
                "A spend ceiling requires input/output token prices for every active route: "
                + ", ".join(missing_prices),
                file=sys.stderr,
            )
            return 2

    selected_cases = cases if args.case_limit == 0 else cases[:args.case_limit]
    groups: list[dict[str, Any]] = []
    total_calls = 0
    total_estimated_cost = 0.0
    # The engine performs a per-case preflight. This outer loop also prevents the
    # total estimated spend over the whole run from exceeding the remaining budget.
    if args.mode == "each":
        run_groups = [(route.provider_id, {route.provider_id: providers[route.provider_id]}, (route,)) for route in routes]
    else:
        run_groups = [("ensemble", providers, tuple(routes))]
    budget_exhausted = False
    for label, group_providers, group_routes in run_groups:
        results: list[dict[str, Any]] = []
        for case in selected_cases:
            remaining = None
            if args.max_estimated_cost_usd is not None:
                remaining = max(0.0, args.max_estimated_cost_usd - total_estimated_cost)
                if remaining <= 0:
                    budget_exhausted = True
                    break
            try:
                result = _run_case(
                    group_providers, group_routes, case,
                    mode=args.mode,
                    max_calls=args.max_calls,
                    max_candidates=args.max_candidates,
                    max_critics=args.max_critics,
                    max_estimated_cost_usd=remaining,
                )
            except Exception as exc:
                print(f"Evaluation failed for case {case.case_id}: {type(exc).__name__}", file=sys.stderr)
                continue
            rendered = _serialize(result)
            results.append(rendered)
            total_calls += result.model_call_count
            if result.estimated_cost_usd is not None:
                total_estimated_cost += result.estimated_cost_usd
            if result.error_type == "BudgetExceeded":
                budget_exhausted = True
                break
        groups.append({
            "name": label,
            "routes": [f"{route.provider_id}/{route.model}" for route in group_routes],
            "passed": sum(1 for result in results if result["passed"]),
            "total_cases": len(results),
            "results": results,
        })
        if budget_exhausted:
            break

    report = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "note": "Phrase checks are regression smoke tests, not a validated intelligence benchmark or proof of GPT-6 Astra parity.",
        "providers_skipped_for_missing_key_or_model": skipped,
        "cases_selected": [case.case_id for case in selected_cases],
        "model_calls_reported": total_calls,
        "estimated_cost_usd_sum": round(total_estimated_cost, 8) if total_estimated_cost else None,
        "estimated_cost_is_not_actual_billing": True,
        "budget_exhausted": budget_exhausted,
        "groups": groups,
    }
    output = args.output or (ROOT / "reports" / f"portable-intelligence-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write report: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"Report: {output}")
    for group in groups:
        print(f"{group['name']}: {group['passed']}/{group['total_cases']} phrase checks passed")
    print(f"Model calls reported: {total_calls}")
    print("Estimated cost is not actual billing. Review the report before drawing quality conclusions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
