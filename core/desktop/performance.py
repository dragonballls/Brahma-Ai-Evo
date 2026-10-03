from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

from .window_manager import WindowManager, WindowInfo, is_game_window


@dataclass(frozen=True)
class PerformanceSnapshot:
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_available_mb: float
    gpu_percent: float | None
    foreground_pid: int | None
    foreground_title: str
    foreground_exe: str
    game_active: bool


@dataclass(frozen=True)
class PerformanceDecision:
    mode: str
    pressure: str
    game_active: bool
    reduce_background_work: bool
    prioritize_foreground: bool
    trim_background_memory: bool
    reason: str


class AdaptivePerformanceEngine:
    """Low-overhead, reversible performance policy.

    It does not fabricate "AI optimization" by constantly mutating processes.
    Instead it observes workload state, waits for sustained pressure, applies a
    small set of reversible Windows-safe actions, and restores the original
    state when pressure clears.
    """

    PROFILES = {"adaptive", "balanced", "performance", "game", "efficiency"}

    def __init__(self):
        self._lock = threading.RLock()
        self.profile = "adaptive"
        self._original_priority: dict[tuple[int, float], object] = {}
        self._managed_target: dict[tuple[int, float], object] = {}
        self._last_adjustment: dict[int, float] = {}
        self._last_trim: dict[tuple[int, float], float] = {}
        self._min_adjustment_interval = 20.0
        self._min_trim_interval = 180.0
        self._gpu_cache: tuple[float, float] = (0.0, -1.0)
        self.last_decision: PerformanceDecision | None = None
        self.last_snapshot: PerformanceSnapshot | None = None
        self.action_count = 0

    def set_profile(self, profile: str) -> str:
        normalized = str(profile or "").strip().lower()
        if normalized not in self.PROFILES:
            raise ValueError(f"Unknown performance profile: {profile}")
        self.profile = normalized
        return normalized

    def _gpu_percent(self) -> float | None:
        now = time.monotonic()
        cached_at, cached = self._gpu_cache
        if now - cached_at < 10.0:
            return None if cached < 0 else cached
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=0.75,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                values = [
                    float(line.strip())
                    for line in completed.stdout.splitlines()
                    if line.strip()
                ]
                if values:
                    value = max(0.0, min(100.0, sum(values) / len(values)))
                    self._gpu_cache = (now, value)
                    return value
        except Exception:
            pass
        self._gpu_cache = (now, -1.0)
        return None

    def snapshot(self) -> PerformanceSnapshot:
        if psutil is None:
            snap = PerformanceSnapshot(
                timestamp=time.time(),
                cpu_percent=0.0,
                memory_percent=0.0,
                memory_available_mb=0.0,
                gpu_percent=None,
                foreground_pid=None,
                foreground_title="",
                foreground_exe="",
                game_active=False,
            )
            self.last_snapshot = snap
            return snap

        cpu = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        foreground = WindowManager.foreground()
        gpu = self._gpu_percent()
        snap = PerformanceSnapshot(
            timestamp=time.time(),
            cpu_percent=cpu,
            memory_percent=float(memory.percent),
            memory_available_mb=float(memory.available) / (1024 * 1024),
            gpu_percent=gpu,
            foreground_pid=foreground.pid if foreground else None,
            foreground_title=foreground.title if foreground else "",
            foreground_exe=foreground.exe if foreground else "",
            game_active=is_game_window(foreground),
        )
        self.last_snapshot = snap
        return snap

    def decide(self, snapshot: PerformanceSnapshot) -> PerformanceDecision:
        if self.profile == "game" or snapshot.game_active:
            decision = PerformanceDecision(
                mode="game",
                pressure="high" if snapshot.cpu_percent >= 90 or snapshot.memory_percent >= 85 else "normal",
                game_active=True,
                reduce_background_work=True,
                prioritize_foreground=True,
                trim_background_memory=snapshot.memory_percent >= 88,
                reason="Foreground game detected; Brahma is minimizing background competition.",
            )
        elif self.profile == "efficiency":
            decision = PerformanceDecision(
                mode="efficiency",
                pressure="high" if snapshot.memory_percent >= 85 or snapshot.cpu_percent >= 90 else "normal",
                game_active=False,
                reduce_background_work=True,
                prioritize_foreground=False,
                trim_background_memory=snapshot.memory_percent >= 90,
                reason="Efficiency profile requested; background work is reduced conservatively.",
            )
        elif self.profile == "performance":
            decision = PerformanceDecision(
                mode="performance",
                pressure="high" if snapshot.cpu_percent >= 92 or snapshot.memory_percent >= 88 else "normal",
                game_active=False,
                reduce_background_work=snapshot.cpu_percent >= 85 or snapshot.memory_percent >= 82,
                prioritize_foreground=True,
                trim_background_memory=snapshot.memory_percent >= 92,
                reason="Performance profile requested; foreground responsiveness is prioritized.",
            )
        else:
            pressure = "high" if snapshot.memory_percent >= 92 or snapshot.cpu_percent >= 95 else (
                "elevated" if snapshot.memory_percent >= 82 or snapshot.cpu_percent >= 88 else "normal"
            )
            decision = PerformanceDecision(
                mode="adaptive" if self.profile == "adaptive" else "balanced",
                pressure=pressure,
                game_active=False,
                reduce_background_work=pressure in {"high", "elevated"},
                prioritize_foreground=False,
                trim_background_memory=pressure == "high",
                reason="Adaptive policy reacts only after sustained resource pressure.",
            )
        self.last_decision = decision
        return decision

    def _restore_background_when_normal(self, decision: PerformanceDecision) -> None:
        if decision.pressure != "normal" or decision.mode not in {"adaptive", "balanced", "efficiency"}:
            return
        if psutil is None:
            return
        with self._lock:
            tracked = list(self._original_priority.items())
        for (pid, created), priority in tracked:
            try:
                proc = psutil.Process(pid)
                if abs(float(proc.create_time()) - created) > 0.5:
                    continue
                if WindowManager.is_user_process(proc) and WindowManager.set_priority(proc, priority):
                    with self._lock:
                        self._original_priority.pop((pid, created), None)
                        self._managed_target.pop((pid, created), None)
            except Exception:
                continue

    @staticmethod
    def _safe_background_candidates(foreground_pid: int | None) -> list[tuple[object, WindowInfo]]:
        if psutil is None:
            return []
        by_pid: dict[int, WindowInfo] = {}
        for window in WindowManager.enumerate_windows():
            if not window.visible or window.pid == foreground_pid:
                continue
            by_pid.setdefault(window.pid, window)

        candidates: list[tuple[object, WindowInfo]] = []
        for pid, window in by_pid.items():
            try:
                proc = psutil.Process(pid)
                if not WindowManager.is_user_process(proc):
                    continue
                if proc.status() in {"zombie", "dead"}:
                    continue
                candidates.append((proc, window))
            except Exception:
                continue
        return candidates

    def _remember_priority(self, proc: object) -> tuple[int, float] | None:
        try:
            pid = int(proc.pid)
            created = float(proc.create_time())
            key = (pid, created)
            with self._lock:
                if key not in self._original_priority:
                    self._original_priority[key] = proc.nice()
            return key
        except Exception:
            return None

    def _restore_demoted_foreground(self, proc: object) -> bool:
        if psutil is None:
            return False
        try:
            key = (int(proc.pid), float(proc.create_time()))
            with self._lock:
                original = self._original_priority.get(key)
                target = self._managed_target.get(key)
            if original is None or target != getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None):
                return False
            if not WindowManager.set_priority(proc, original):
                return False
            with self._lock:
                self._original_priority.pop(key, None)
                self._managed_target.pop(key, None)
            return True
        except Exception:
            return False

    def _set_foreground_priority(self, snapshot: PerformanceSnapshot, decision: PerformanceDecision) -> None:
        if psutil is None or not snapshot.foreground_pid:
            return
        try:
            proc = psutil.Process(snapshot.foreground_pid)
            if not WindowManager.is_user_process(proc):
                return
            self._restore_demoted_foreground(proc)
            key = self._remember_priority(proc)
            if key is None:
                return
            target = psutil.ABOVE_NORMAL_PRIORITY_CLASS if decision.prioritize_foreground else psutil.NORMAL_PRIORITY_CLASS
            now = time.monotonic()
            if now - self._last_adjustment.get(snapshot.foreground_pid, 0.0) < self._min_adjustment_interval:
                return
            if WindowManager.set_priority(proc, target):
                with self._lock:
                    self._managed_target[key] = target
                self._last_adjustment[snapshot.foreground_pid] = now
                self.action_count += 1
        except Exception:
            pass

    def _demote_background(self, snapshot: PerformanceSnapshot, decision: PerformanceDecision) -> None:
        if psutil is None or not decision.reduce_background_work:
            return

        now = time.monotonic()
        # Only tune the heaviest few visible background processes.  This avoids
        # creating a priority storm and lets Windows retain ordinary scheduling.
        ranked: list[tuple[float, object, WindowInfo]] = []
        for proc, window in self._safe_background_candidates(snapshot.foreground_pid):
            try:
                cpu = float(proc.cpu_percent(interval=None))
                ranked.append((cpu, proc, window))
            except Exception:
                continue
        ranked.sort(key=lambda item: item[0], reverse=True)

        for cpu, proc, window in ranked[:4]:
            if cpu < 2.0 and not window.minimized:
                continue
            try:
                key = self._remember_priority(proc)
                if key is None:
                    continue
                pid = int(proc.pid)
                if now - self._last_adjustment.get(pid, 0.0) < self._min_adjustment_interval:
                    continue
                # BELOW_NORMAL is deliberately used instead of IDLE: background
                # applications still make progress and can recover quickly.
                if WindowManager.set_priority(proc, psutil.BELOW_NORMAL_PRIORITY_CLASS):
                    with self._lock:
                        self._managed_target[key] = psutil.BELOW_NORMAL_PRIORITY_CLASS
                    self._last_adjustment[pid] = now
                    self.action_count += 1

                if decision.trim_background_memory and window.minimized:
                    if now - self._last_trim.get(key, 0.0) >= self._min_trim_interval:
                        if WindowManager.trim_working_set(pid):
                            self._last_trim[key] = now
                            self.action_count += 1
            except Exception:
                continue

    def tick(self) -> dict[str, Any]:
        snap = self.snapshot()
        decision = self.decide(snap)
        if snap.foreground_pid and psutil is not None:
            try:
                self._restore_demoted_foreground(psutil.Process(snap.foreground_pid))
            except Exception:
                pass
        if decision.prioritize_foreground or decision.mode == "game":
            self._set_foreground_priority(snap, decision)
        self._demote_background(snap, decision)
        self._restore_background_when_normal(decision)
        return self.status()

    def restore(self) -> int:
        restored = 0
        if psutil is None:
            return restored
        with self._lock:
            original = dict(self._original_priority)
            self._original_priority.clear()
            self._managed_target.clear()
        for (pid, created), priority in original.items():
            try:
                proc = psutil.Process(pid)
                if abs(float(proc.create_time()) - created) > 0.5:
                    continue
                if WindowManager.set_priority(proc, priority):
                    restored += 1
            except Exception:
                continue
        return restored

    def status(self) -> dict[str, Any]:
        snap = self.last_snapshot
        decision = self.last_decision
        return {
            "profile": self.profile,
            "snapshot": asdict(snap) if snap else None,
            "decision": asdict(decision) if decision else None,
            "tracked_processes": len(self._original_priority),
            "actions_applied": self.action_count,
        }
