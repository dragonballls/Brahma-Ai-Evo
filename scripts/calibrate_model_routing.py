"""Calibrate Brahma model routing from verified Challenger outcomes.

Run this locally with configured provider/API credentials. It benchmarks each
selected provider model independently on the same cases, records the result in
the model-performance ledger, and leaves the main conversational path unchanged.
"""
from __future__ import annotations

import argparse
import json

from core.cognitive_challenger import CASES, _score
from core.intelligence_orchestrator import _catalog_models, _configured_providers, _select_provider_model
from core.model_performance import record
from or_client import client as cloud_client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="smart")
    parser.add_argument("--max-models", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--include-astra", action="store_true")
    args = parser.parse_args()

    models = _catalog_models()
    providers = _configured_providers()
    selected = []
    for provider in providers:
        model = _select_provider_model(provider, models, {}, args.profile)
        if model:
            selected.append((provider, model))

    if args.include_astra and "openai/gpt-6-astra" in models:
        selected.append(("openai", "openai/gpt-6-astra"))

    selected = list(dict.fromkeys(selected))[: max(1, args.max_models)]
    output = []

    for provider, model in selected:
        scores = []
        for case in CASES:
            try:
                answer = cloud_client.chat(
                    case.prompt,
                    system="Solve the benchmark task directly and accurately. Do not mention this benchmark.",
                    model=model,
                    max_tokens=args.max_tokens,
                    temperature=0.0,
                )
                scores.append(_score(case, answer))
            except Exception:
                scores.append(0.0)
        mean = round(sum(scores) / len(scores), 2) if scores else 0.0
        for case, score in zip(CASES, scores):
            record(
                provider=provider,
                model=model,
                profile=args.profile,
                score=score,
            )
        output.append({
            "provider": provider,
            "model": model,
            "profile": args.profile,
            "mean_score": mean,
            "cases": len(scores),
        })

    print(json.dumps({
        "profile": args.profile,
        "models": output,
        "selection": "evidence-calibrated",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
