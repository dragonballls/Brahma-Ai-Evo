"""Measured model-performance memory used for adaptive routing."""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any

from core.user_paths import get_user_data_dir

_PATH = get_user_data_dir() / "intelligence" / "model_performance.json"
_LOCK = RLock()


def _safe_performance_text() -> str:
    if os.name == "nt":
        from core.windows_file_safety import read_text
        text, _size = read_text(_PATH, max_chars=4 * 1024 * 1024)
        return text
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(_PATH, flags)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(4 * 1024 * 1024 + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _load() -> dict[str, Any]:
    if not _PATH.is_file():
        return {"schema_version": 1, "models": {}}
    try:
        raw = _safe_performance_text()
        if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("Model-performance state exceeds the 4 MiB safety limit.")
        payload = json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("models", {}), dict):
            return payload
        raise ValueError("Model-performance state has an invalid schema.")
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        quarantine = _PATH.with_name(
            f"{_PATH.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        )
        try:
            _PATH.replace(quarantine)
        except OSError as quarantine_exc:
            raise RuntimeError(
                f"Model-performance state is corrupt and could not be quarantined: {_PATH}"
            ) from quarantine_exc
        raise RuntimeError(
            f"Model-performance state was corrupt and has been quarantined: {quarantine.name}"
        ) from exc
    except OSError:
        raise


def _save(payload: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _PATH.with_name(f".{_PATH.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
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
    score = float(score)
    latency_ms = float(latency_ms)
    if not math.isfinite(score) or not math.isfinite(latency_ms):
        raise ValueError("Model-performance score and latency must be finite numbers.")
    score = max(0.0, min(100.0, score))
    latency_ms = max(0.0, latency_ms)
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
    if not item:
        return 0.0
    try:
        samples = int(item.get("samples", 0))
        score = float(item.get("mean_score", 0.0))
        latency = float(item.get("mean_latency_ms", 0.0))
    except (TypeError, ValueError):
        return 0.0
    if samples <= 0 or not math.isfinite(score) or not math.isfinite(latency):
        return 0.0
    # Keep learned routing bounded so a small, noisy history cannot overpower
    # the base quality heuristics or eliminate unseen models.
    reliability = min(1.0, samples / 8.0)
    return max(-6.0, min(6.0, (score - 75.0) * 0.12 * reliability))
