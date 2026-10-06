from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
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

    def _quarantine_corrupt(self) -> Path:
        quarantine = self.path.with_name(
            f"{self.path.name}.corrupt-{uuid.uuid4().hex[:8]}"
        )
        self.path.replace(quarantine)
        return quarantine

    def _validate_loaded_state(self, raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Workspace state root must be an object.")
        version = raw.get("version", self.VERSION)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("Workspace state version is invalid.")
        if version > self.VERSION:
            raise ValueError("Workspace state was created by a newer version.")
        workspaces = raw.get("workspaces")
        if not isinstance(workspaces, dict):
            raise ValueError("Workspace state workspaces must be an object.")
        for name, workspace in workspaces.items():
            if not isinstance(name, str) or not isinstance(workspace, dict):
                raise ValueError("Workspace state contains an invalid workspace.")
            windows = workspace.get("windows", [])
            if not isinstance(windows, list) or any(not isinstance(item, dict) for item in windows):
                raise ValueError("Workspace state contains invalid window data.")
        preferences = raw.get("app_preferences", {})
        if not isinstance(preferences, dict):
            raise ValueError("Workspace app preferences must be an object.")
        merged = self._default()
        merged.update(raw)
        merged["workspaces"] = workspaces
        return merged

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                if not self.path.exists():
                    return self._default()
                if self.path.is_symlink():
                    raise RuntimeError("Workspace state path must not be a symlink.")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return self._validate_loaded_state(raw)
            except RuntimeError:
                raise
            except Exception as exc:
                try:
                    self._quarantine_corrupt()
                except OSError as quarantine_exc:
                    raise RuntimeError(
                        "Workspace state is corrupt and could not be quarantined safely."
                    ) from quarantine_exc
                raise RuntimeError(
                    "Workspace state was corrupt; the original was quarantined."
                ) from exc

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
        with self._lock:
            state = self.load()
            state.update(changes)
            if not self.save(state):
                raise RuntimeError("Workspace state could not be persisted.")
            return state

    def upsert_window(self, workspace: str, item: dict[str, Any]) -> bool:
        with self._lock:
            state = self.load()
            workspaces = state.setdefault("workspaces", {})
            ws = workspaces.setdefault(workspace, {"name": workspace.title(), "windows": []})
            if not isinstance(ws, dict):
                raise RuntimeError("Workspace state contains invalid workspace data.")
            windows = ws.setdefault("windows", [])
            if not isinstance(windows, list):
                raise RuntimeError("Workspace state contains invalid window data.")
            identity = (
                str(item.get("identity") or "").strip()
                or str(item.get("exe") or "").strip().lower()
                or str(item.get("title") or "").strip()
            )
            item = dict(item)
            item["identity"] = identity
            windows[:] = [
                existing for existing in windows
                if isinstance(existing, dict) and str(existing.get("identity") or "") != identity
            ]
            windows.append(item)
            return self.save(state)

    def remove_window(self, workspace: str, identity: str) -> bool:
        with self._lock:
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
                if isinstance(item, dict) and str(item.get("identity") or "") != str(identity)
            ]
            return before != len(ws["windows"]) and self.save(state)

    def snapshot(self) -> dict[str, Any]:
        return self.load()


store = WorkspaceStore()
