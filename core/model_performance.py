"""Measured model-performance memory used for adaptive routing."""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from core.user_paths import get_user_data_dir

_PATH = get_user_data_dir() / "intelligence" / "model_performance.json"
_LOCK = RLock()


def _load() -> dict[str, Any]:
    try:
        if _PATH.is_file():
            payload = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {"schema_version": 1, "models": {}}


def _save(payload: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _PATH.with_name(f".{_PATH.name}.{__import__('os').getpid()}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(_PATH)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def record(*, provider: str, model: str, profile: str, score: float, latency_ms: float = 0.0) -> None:
    score = max(0.0, min(100.0, float(score)))
    key = f"{provider}|{model}|{profile}"
    with _LOCK:
        data = _load()
        item = data.setdefault("models", {}).setdefault(
            key,
            {"provider": provider, "model": model, "profile": profile,
             "samples": 0, "mean_score": 0.0, "mean_latency_ms": 0.0},
        )
        samples = int(item.get("samples", 0))
        item["mean_score"] = ((float(item.get("mean_score", 0.0)) * samples) + score) / (samples + 1)
        item["mean_latency_ms"] = ((float(item.get("mean_latency_ms", 0.0)) * samples) + max(0.0, float(latency_ms))) / (samples + 1)
        item["samples"] = samples + 1
        _save(data)


def lookup(*, provider: str, model: str, profile: str) -> dict[str, Any] | None:
    key = f"{provider}|{model}|{profile}"
    with _LOCK:
        item = _load().get("models", {}).get(key)
        return dict(item) if isinstance(item, dict) else None


def routing_bonus(*, provider: str, model: str, profile: str) -> float:
    item = lookup(provider=provider, model=model, profile=profile)
    if not item or int(item.get("samples", 0)) <= 0:
        return 0.0
    # Keep learned routing bounded so a small, noisy history cannot overpower
    # the base quality heuristics or eliminate unseen models.
    reliability = min(1.0, int(item["samples"]) / 8.0)
    score = float(item.get("mean_score", 0.0))
    return max(-6.0, min(6.0, (score - 75.0) * 0.12 * reliability))
