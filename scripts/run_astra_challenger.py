"""Run Brahma and GPT-6 Astra through the same Challenger suite.

Examples:
  python scripts/run_astra_challenger.py --system brahma
  python scripts/run_astra_challenger.py --system astra
  python scripts/run_astra_challenger.py --system both

The Astra model ID is configurable because gateways may expose provider aliases
differently. Results are written to benchmarks/results.json and to the local
intellect benchmark store.
"""
from __future__ import annotations

import argparse
import json

from core.cognitive_challenger import CASES, run, compare, RESULTS_PATH, grade_with_model
from core.intelligence_orchestrator import orchestrator
from llm_client import client as cloud_client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", choices=("brahma", "astra", "both"), default="brahma")
    parser.add_argument("--astra-model", default="openai/gpt-6-astra")
    parser.add_argument("--grader-model", default="")
    parser.add_argument("--semantic-grade", action="store_true")
    parser.add_argument("--no-persist-intellect", action="store_true")
    args = parser.parse_args()

    def brahma_solver(prompt: str, dimension: str) -> str:
        return orchestrator.respond(prompt, profile=dimension if dimension in {"coding", "research", "planning"} else "smart")

    def astra_solver(prompt: str, _dimension: str) -> str:
        return cloud_client.chat(
            prompt,
            system="Solve the benchmark task directly and accurately.",
            model=args.astra_model,
            max_tokens=4096,
            temperature=0.0,
        )

    grader = None
    if args.semantic_grade and args.grader_model:
        grader = lambda prompt, answer: grade_with_model(
            prompt, answer, grader_model=args.grader_model, client=cloud_client
        )

    results = {}
    if args.system in {"brahma", "both"}:
        results["brahma"] = run(
            brahma_solver,
            system="brahma",
            cases=CASES,
            persist_intellect=not args.no_persist_intellect,
            grader=grader,
        )
    if args.system in {"astra", "both"}:
        results["astra"] = run(
            astra_solver,
            system="gpt-6-astra",
            cases=CASES,
            persist_intellect=not args.no_persist_intellect,
        )
    if args.system == "both":
        results["comparison"] = compare(results["brahma"], results["astra"])

    print(json.dumps(results, indent=2))
    print(f"Results file: {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
