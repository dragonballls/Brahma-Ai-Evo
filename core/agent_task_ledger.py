"""Lightweight, persistent task ledger for Brahma's agentic work.

Inspired by Paperclip's heartbeat/run model, but implemented with Brahma's
existing Python/AppData architecture and no new runtime service.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir

LEDGER_PATH = get_user_data_dir() / "agent_tasks" / "ledger.json"
MAX_RECORDS = 500
ACTIVE_STATES = {"queued", "running", "blocked"}


class AgentTaskLedger:
    """Durable task/run records with short-lived heartbeat semantics."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LEDGER_PATH)
        self._lock = threading.RLock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return list(data) if isinstance(data, list) else []
        except Exception:
            return []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records[-MAX_RECORDS:], indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def create(self, title: str, goal: str, *, source: str = "brahma", metadata: dict[str, Any] | None = None) -> str:
        now = time.time()
        task_id = f"task-{int(now * 1000)}-{uuid.uuid4().hex[:8]}"
        record = {
            "task_id": task_id,
            "title": str(title).strip()[:200],
            "goal": str(goal).strip()[:2000],
            "source": str(source).strip()[:80],
            "state": "queued",
            "created_at": now,
            "updated_at": now,
            "heartbeats": 0,
            "attempts": 0,
            "evidence": [],
            "metadata": dict(metadata or {}),
        }
        with self._lock:
            records = self._load()
            records.append(record)
            self._save(records)
        return task_id

    def heartbeat(self, task_id: str, *, state: str = "running", evidence: str = "", metadata: dict[str, Any] | None = None) -> bool:
        state = state if state in {"queued", "running", "blocked", "completed", "failed", "cancelled"} else "running"
        with self._lock:
            records = self._load()
            for record in reversed(records):
                if record.get("task_id") != task_id:
                    continue
                record["state"] = state
                record["updated_at"] = time.time()
                record["heartbeats"] = int(record.get("heartbeats", 0)) + 1
                if state == "running":
                    record["attempts"] = int(record.get("attempts", 0)) + 1
                if evidence:
                    record.setdefault("evidence", []).append(str(evidence)[:4000])
                    record["evidence"] = record["evidence"][-12:]
                if metadata:
                    record.setdefault("metadata", {}).update(metadata)
                self._save(records)
                return True
        return False

    def complete(self, task_id: str, *, evidence: str = "", metadata: dict[str, Any] | None = None) -> bool:
        return self.heartbeat(task_id, state="completed", evidence=evidence, metadata=metadata)

    def fail(self, task_id: str, error: str) -> bool:
        return self.heartbeat(task_id, state="failed", evidence=f"error: {error}")

    def recover_stale(self, *, max_age_seconds: int = 6 * 60 * 60) -> int:
        cutoff = time.time() - max(60, int(max_age_seconds))
        changed = 0
        with self._lock:
            records = self._load()
            for record in records:
                if record.get("state") in ACTIVE_STATES and float(record.get("updated_at", 0) or 0) < cutoff:
                    record["state"] = "blocked"
                    record["updated_at"] = time.time()
                    record["metadata"] = {**dict(record.get("metadata") or {}), "recovered": True}
                    changed += 1
            if changed:
                self._save(records)
        return changed

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            records = self._load()
        return [dict(item) for item in records[-max(1, min(int(limit), 100)):]][::-1]


ledger = AgentTaskLedger()
