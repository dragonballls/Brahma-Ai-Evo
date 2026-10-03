from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QTimer

from .app_host import application_host
from .layer import DesktopLayer
from .performance import AdaptivePerformanceEngine
from .workspace import WorkspaceStore
from .window_manager import WindowManager, is_game_window


class DesktopModeController:
    """Composes desktop rendering, native window management, and performance policy.

    Explorer remains untouched.  Desktop mode only changes Brahma's own
    presentation and manages application windows above the desktop surface.
    """

    def __init__(self, ui, base_dir: Path):
        self.ui = ui
        self.base_dir = Path(base_dir)
        self.workspace = WorkspaceStore()
        self.performance = AdaptivePerformanceEngine()
        self.layer: DesktopLayer | None = None
        self._timer: QTimer | None = None
        self._lock = threading.RLock()
        self.enabled = False
        self.last_error = ""
        self._show_overlay = False

    def _ensure_layer(self) -> DesktopLayer:
        if self.layer is None:
            self.layer = DesktopLayer(self.base_dir)
        return self.layer

    def configure(self, profile: str | None = None, show_overlay: bool | None = None) -> dict[str, Any]:
        if profile is not None:
            self.performance.set_profile(profile)
        if show_overlay is not None:
            self._show_overlay = bool(show_overlay)
        self.workspace.set(
            performance_profile=self.performance.profile,
            show_performance_overlay=self._show_overlay,
        )
        return self.status()

    def enable(self) -> dict[str, Any]:
        with self._lock:
            if self.enabled:
                return self.status()
            try:
                layer = self._ensure_layer()
                layer.show()
                # Hide only Brahma's normal main window.  Windows Explorer,
                # taskbar, Start, and native applications remain untouched.
                if hasattr(self.ui, "enter_desktop_mode"):
                    self.ui.enter_desktop_mode()
                else:
                    self.ui._win.hide()
                    self.ui._show_floating_icon()
                self.enabled = True
                state = self.workspace.load()
                state["desktop_mode"] = True
                state["performance_profile"] = self.performance.profile
                state["show_performance_overlay"] = self._show_overlay
                self.workspace.save(state)
                self._start_timer()
                self.tick()
                return self.status()
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                try:
                    if self.layer:
                        self.layer.hide()
                except Exception:
                    pass
                return self.status()

    def disable(self) -> dict[str, Any]:
        with self._lock:
            self.enabled = False
            self._stop_timer()
            try:
                if self.layer:
                    self.layer.set_low_power(True)
                    self.layer.hide()
            except Exception:
                pass
            restored = self.performance.restore()
            try:
                if hasattr(self.ui, "exit_desktop_mode"):
                    self.ui.exit_desktop_mode()
                else:
                    self.ui.show_main()
            except Exception as exc:
                self.last_error = str(exc)
            state = self.workspace.load()
            state["desktop_mode"] = False
            self.workspace.save(state)
            result = self.status()
            result["restored_process_priorities"] = restored
            return result

    def toggle(self) -> dict[str, Any]:
        return self.disable() if self.enabled else self.enable()

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = QTimer()
        self._timer.setInterval(2500)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    def _stop_timer(self) -> None:
        if self._timer is None:
            return
        try:
            self._timer.stop()
            self._timer.deleteLater()
        except Exception:
            pass
        self._timer = None

    def tick(self) -> dict[str, Any]:
        with self._lock:
            if not self.enabled:
                return self.status()
            status = self.performance.tick()
            snapshot = status.get("snapshot") or {}
            game_active = bool(snapshot.get("game_active"))
            if self.layer:
                self.layer.set_low_power(game_active)
                self.layer.set_performance_status(
                    self.performance.profile,
                    snapshot.get("cpu_percent"),
                    snapshot.get("memory_percent"),
                    game_active,
                    visible=self._show_overlay,
                )
                self.layer._keep_bottom()
            self._reconcile_workspace(snapshot)
            return self.status()

    def _reconcile_workspace(self, snapshot: dict[str, Any]) -> None:
        foreground_pid = snapshot.get("foreground_pid")
        foreground = WindowManager.foreground()
        if not foreground:
            return
        identity = f"{foreground.exe}:{foreground.title}".strip(":")
        self.workspace.upsert_window(
            "main",
            {
                "identity": identity,
                "title": foreground.title,
                "exe": foreground.exe,
                "last_seen": time.time(),
                "game": is_game_window(foreground),
                "x": None,
                "y": None,
                "width": None,
                "height": None,
            },
        )

    def open(self, target: str) -> dict[str, Any]:
        return application_host.open(target)

    def windows(self) -> list[dict[str, Any]]:
        return application_host.list_windows()

    def control_window(self, action: str, target: str, **kwargs: Any) -> dict[str, Any]:
        return application_host.control(action, target, **kwargs)

    def status(self) -> dict[str, Any]:
        result = {
            "enabled": self.enabled,
            "windows_supported": WindowManager.available(),
            "profile": self.performance.profile,
            "show_performance_overlay": self._show_overlay,
            "last_error": self.last_error,
            "performance": self.performance.status(),
        }
        return result
