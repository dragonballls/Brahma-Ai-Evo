from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir


def classify_application(exe: str, title: str = "") -> str:
    text = f"{exe} {title}".lower()
    if any(x in text for x in (
        "minecraft", "roblox", "fortnite", "valorant", "apex", "steamvr",
        "overwatch", "counter-strike", "cs2", "league of legends", "elden ring",
    )):
        return "game"
    if any(x in text for x in ("chrome", "msedge", "firefox", "brave", "opera")):
        return "browser"
    if any(x in text for x in ("code", "devenv", "pycharm", "idea", "node", "python", "git")):
        return "development"
    if any(x in text for x in ("discord", "teams", "slack", "telegram", "zoom")):
        return "communication"
    if any(x in text for x in ("spotify", "vlc", "obs64", "potplayer")):
        return "media"
    return "generic"


@dataclass
class ApplicationProfile:
    exe: str
    role: str = "generic"
    samples: int = 0
    ewma_cpu: float = 0.0
    ewma_memory_mb: float = 0.0
    ewma_read_mb_s: float = 0.0
    ewma_write_mb_s: float = 0.0
    ewma_active_ratio: float = 0.0
    last_seen: float = 0.0
    last_foreground: float = 0.0

    def observe(
        self,
        *,
        cpu: float,
        memory_mb: float,
        read_mb_s: float,
        write_mb_s: float,
        foreground: bool,
    ) -> None:
        self.samples += 1
        alpha = 0.18 if self.samples > 1 else 1.0
        self.ewma_cpu = (self.ewma_cpu * (1 - alpha)) + (max(0.0, cpu) * alpha)
        self.ewma_memory_mb = (self.ewma_memory_mb * (1 - alpha)) + (max(0.0, memory_mb) * alpha)
        self.ewma_read_mb_s = (self.ewma_read_mb_s * (1 - alpha)) + (max(0.0, read_mb_s) * alpha)
        self.ewma_write_mb_s = (self.ewma_write_mb_s * (1 - alpha)) + (max(0.0, write_mb_s) * alpha)
        self.ewma_active_ratio = (self.ewma_active_ratio * (1 - alpha)) + ((1.0 if foreground else 0.0) * alpha)
        now = time.time()
        self.last_seen = now
        if foreground:
            self.last_foreground = now


class PerformanceBrain:
    """Small online learner; intentionally no LLM or always-running model.

    The brain learns workload baselines cheaply, persists them atomically, and
    provides confidence-weighted recommendations to the deterministic governor.
    """

    VERSION = 1

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else get_user_data_dir() / "config" / "performance_profiles.json"
        self._lock = threading.RLock()
        self._profiles: dict[str, ApplicationProfile] = {}
        self._dirty = False
        self._last_save = 0.0
        self.load()

    @staticmethod
    def _key(exe: str) -> str:
        return str(exe or "").strip().lower()

    def load(self) -> None:
        with self._lock:
            self._profiles = {}
            try:
                if not self.path.exists():
                    return
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                profiles = raw.get("profiles") if isinstance(raw, dict) else None
                if not isinstance(profiles, dict):
                    return
                for key, value in profiles.items():
                    if not isinstance(value, dict):
                        continue
                    try:
                        item = ApplicationProfile(
                            exe=str(value.get("exe") or key),
                            role=str(value.get("role") or "generic"),
                            samples=int(value.get("samples") or 0),
                            ewma_cpu=float(value.get("ewma_cpu") or 0.0),
                            ewma_memory_mb=float(value.get("ewma_memory_mb") or 0.0),
                            ewma_read_mb_s=float(value.get("ewma_read_mb_s") or 0.0),
                            ewma_write_mb_s=float(value.get("ewma_write_mb_s") or 0.0),
                            ewma_active_ratio=float(value.get("ewma_active_ratio") or 0.0),
                            last_seen=float(value.get("last_seen") or 0.0),
                            last_foreground=float(value.get("last_foreground") or 0.0),
                        )
                        self._profiles[self._key(item.exe)] = item
                    except Exception:
                        continue
            except Exception:
                return

    def _save(self, *, force: bool = False) -> bool:
        with self._lock:
            if not self._dirty and not force:
                return True
            now = time.time()
            if not force and now - self._last_save < 30.0:
                return True
            payload: dict[str, Any] = {
                "version": self.VERSION,
                "updated_at": now,
                "profiles": {
                    key: asdict(profile)
                    for key, profile in self._profiles.items()
                },
            }
            try:
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
                self._dirty = False
                self._last_save = now
                return True
            except Exception:
                return False

    def observe_process(
        self,
        *,
        exe: str,
        title: str,
        cpu: float,
        memory_mb: float,
        read_mb_s: float,
        write_mb_s: float,
        foreground: bool,
    ) -> ApplicationProfile:
        key = self._key(exe)
        with self._lock:
            profile = self._profiles.get(key)
            if profile is None:
                profile = ApplicationProfile(
                    exe=str(exe or "unknown").strip().lower(),
                    role=classify_application(exe, title),
                )
                self._profiles[key] = profile
            elif profile.role == "generic":
                profile.role = classify_application(exe, title)
            profile.observe(
                cpu=cpu,
                memory_mb=memory_mb,
                read_mb_s=read_mb_s,
                write_mb_s=write_mb_s,
                foreground=foreground,
            )
            self._dirty = True
            self._save()
            return profile

    def profile_for(self, exe: str) -> ApplicationProfile | None:
        with self._lock:
            profile = self._profiles.get(self._key(exe))
            if profile is None:
                return None
            return ApplicationProfile(**asdict(profile))

    def confidence(self, profile: ApplicationProfile | None) -> float:
        if profile is None:
            return 0.0
        return min(1.0, max(0.0, profile.samples / 40.0))

    def recommend_background_action(
        self,
        profile: ApplicationProfile | None,
        *,
        cpu: float,
        memory_mb: float,
        minimized: bool,
        system_memory_percent: float,
        game_active: bool,
    ) -> dict[str, Any]:
        if profile is None or self.confidence(profile) < 0.20:
            return {"action": "observe", "confidence": 0.0, "reason": "Not enough history yet."}
        confidence = self.confidence(profile)

        if game_active:
            return {
                "action": "observe",
                "confidence": confidence,
                "reason": "Foreground game takes precedence over background tuning.",
            }

        cpu_baseline = max(2.0, profile.ewma_cpu)
        memory_baseline = max(128.0, profile.ewma_memory_mb)

        if minimized and system_memory_percent >= 90 and memory_mb >= memory_baseline * 1.20:
            return {
                "action": "trim_memory",
                "confidence": confidence,
                "reason": "Minimized application is using substantially more memory than its learned baseline during high RAM pressure.",
            }
        if cpu >= max(10.0, cpu_baseline * 1.60):
            return {
                "action": "lower_priority",
                "confidence": confidence,
                "reason": "Background CPU use is materially above the application's learned baseline.",
            }
        return {
            "action": "observe",
            "confidence": confidence,
            "reason": "Current behavior is within the learned workload envelope.",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            profiles = list(self._profiles.values())
            return {
                "version": self.VERSION,
                "profile_count": len(profiles),
                "learned_samples": sum(item.samples for item in profiles),
                "profiles": [
                    {
                        "exe": item.exe,
                        "role": item.role,
                        "samples": item.samples,
                        "confidence": round(self.confidence(item), 3),
                        "ewma_cpu": round(item.ewma_cpu, 2),
                        "ewma_memory_mb": round(item.ewma_memory_mb, 1),
                    }
                    for item in sorted(profiles, key=lambda p: p.last_seen, reverse=True)[:20]
                ],
            }

    def flush(self) -> bool:
        return self._save(force=True)


brain = PerformanceBrain()
