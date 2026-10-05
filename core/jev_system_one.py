"""Optional TypeSafe Jev System-One decision layer for Brahma Evo.

Jev is used only for bounded, structured decisions. Brahma remains the executor
and falls back to its existing deterministic paths whenever Jev is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any

import requests

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "typesafe/jev-1.13"


class JevUnavailable(RuntimeError):
    """Raised when Jev cannot be used for this decision."""


class JevSystemOne:
    def __init__(self, *, base_url: str | None = None, model: str | None = None, cache_size: int = 512):
        self.base_url = (base_url or os.getenv("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("TYPESAFE_MODEL") or DEFAULT_MODEL
        self.cache_size = max(32, int(cache_size))
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return os.getenv("BRAHMA_JEV_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}

    def _api_key(self) -> str:
        # Prefer Brahma's persistent credential store, with the documented
        # environment variable as a deployment/CI override.
        try:
            from config import get_api_key
            configured = get_api_key("TypeSafe") or get_api_key("Jev")
            if configured:
                return configured.strip()
        except Exception:
            pass
        return os.getenv("TYPESAFE_API_KEY", "").strip()

    def evaluate(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
        timeout: float = 3.0,
        cache_key: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled or not self._api_key() or not questions:
            return None

        body = {"model": self.model, "state": state, "questions": questions}
        key = cache_key or hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return dict(cached)

        started = time.perf_counter()
        try:
            response = requests.post(
                self.base_url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key()}"},
                timeout=max(0.5, float(timeout)),
            )
            if response.status_code >= 400:
                raise JevUnavailable(f"http_{response.status_code}")
            payload = response.json()
            answers = payload.get("answers")
            if not isinstance(answers, dict):
                raise JevUnavailable("invalid_answers")

            result = {
                "model": payload.get("model", self.model),
                "answers": answers,
                "usage": payload.get("usage") or {},
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            with self._lock:
                self._cache[key] = dict(result)
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return result
        except JevUnavailable:
            raise
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise JevUnavailable(type(exc).__name__) from exc


jev = JevSystemOne()
