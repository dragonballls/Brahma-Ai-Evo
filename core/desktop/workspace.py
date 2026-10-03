from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir


class WorkspaceStore:
    """Small, crash-safe persistent store for Brahma's desktop workspace.

    Only declarative state is persisted.  Native HWNDs/PIDs are never treated as
    durable identifiers because Windows can recycle them after a reboot.
    """

    VERSION = 1

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else get_user_data_dir() / "config" / "desktop_workspace.json"
        self._lock = threading.RLock()

    def _default(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "updated_at": time.time(),
            "desktop_mode": False,
            "performance_profile": "adaptive",
            "show_performance_overlay": False,
            "workspaces": {
                "main": {
                    "name": "Main",
                    "windows": [],
                }
            },
            "app_preferences": {},
        }

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                if not self.path.exists():
                    return self._default()
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return self._default()
                merged = self._default()
                merged.update(raw)
                workspaces = raw.get("workspaces")
                if isinstance(workspaces, dict):
                    merged["workspaces"] = workspaces
                return merged
            except Exception:
                return self._default()

    def save(self, state: dict[str, Any]) -> bool:
        with self._lock:
            try:
                payload = dict(state)
                payload["version"] = self.VERSION
                payload["updated_at"] = time.time()
                self.path.parent.mkdir(parents=True, exist_ok=True)

                fd, tmp_name = tempfile.mkstemp(
                    prefix=f"{self.path.name}.",
                    suffix=".tmp",
                    dir=str(self.path.parent),
                    text=True,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2, ensure_ascii=False)
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp_name, self.path)
                finally:
                    try:
                        Path(tmp_name).unlink(missing_ok=True)
                    except Exception:
                        pass
                return True
            except Exception:
                return False

    def set(self, **changes: Any) -> dict[str, Any]:
        state = self.load()
        state.update(changes)
        self.save(state)
        return state

    def upsert_window(self, workspace: str, item: dict[str, Any]) -> bool:
        state = self.load()
        workspaces = state.setdefault("workspaces", {})
        ws = workspaces.setdefault(workspace, {"name": workspace.title(), "windows": []})
        windows = ws.setdefault("windows", [])
        identity = (
            str(item.get("identity") or "").strip()
            or str(item.get("exe") or "").strip().lower()
            or str(item.get("title") or "").strip()
        )
        item = dict(item)
        item["identity"] = identity
        windows[:] = [
            existing for existing in windows
            if str(existing.get("identity") or "") != identity
        ]
        windows.append(item)
        return self.save(state)

    def remove_window(self, workspace: str, identity: str) -> bool:
        state = self.load()
        ws = state.get("workspaces", {}).get(workspace)
        if not isinstance(ws, dict):
            return False
        windows = ws.get("windows")
        if not isinstance(windows, list):
            return False
        before = len(windows)
        ws["windows"] = [
            item for item in windows
            if str(item.get("identity") or "") != str(identity)
        ]
        return before != len(ws["windows"]) and self.save(state)

    def snapshot(self) -> dict[str, Any]:
        return self.load()


store = WorkspaceStore()
