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
from .web_host import web_application_host
from .native_host import native_window_host


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
        self._use_workerw = False
        self._last_workspace_identity = ""
        self._last_workspace_geometry: tuple[int, int, int, int] | None = None
        self._last_workspace_persist_at = 0.0

    def _ensure_layer(self) -> DesktopLayer:
        if self.layer is None:
            self.layer = DesktopLayer(self.base_dir, use_workerw=self._use_workerw)
        return self.layer

    def configure(
        self,
        profile: str | None = None,
        show_overlay: bool | None = None,
        use_workerw: bool | None = None,
    ) -> dict[str, Any]:
        if profile is not None:
            try:
                self.performance.set_profile(profile)
            except ValueError:
                self.performance.set_profile("adaptive")
                self.last_error = f"Unknown performance profile '{profile}'; using adaptive."
        if show_overlay is not None:
            self._show_overlay = bool(show_overlay)
        if use_workerw is not None:
            self._use_workerw = bool(use_workerw)
            # Recreate only if not active; live backend switching is deliberately
            # avoided because SetParent is a Windows-native boundary.
            if not self.enabled and self.layer is not None:
                try:
                    self.layer.close()
                except Exception:
                    pass
                self.layer = None
        self.workspace.set(
            performance_profile=self.performance.profile,
            show_performance_overlay=self._show_overlay,
            use_workerw=self._use_workerw,
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
                state["use_workerw"] = self._use_workerw
                self.workspace.save(state)
                try:
                    if hasattr(self.ui, "_load_app_settings") and hasattr(self.ui, "_save_app_settings"):
                        settings = self.ui._load_app_settings()
                        settings["desktop_mode_enabled"] = True
                        settings["desktop_performance_profile"] = self.performance.profile
                        settings["show_desktop_performance_overlay"] = self._show_overlay
                        self.ui._save_app_settings(settings)
                except Exception:
                    pass
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
            try:
                if hasattr(self.ui, "_load_app_settings") and hasattr(self.ui, "_save_app_settings"):
                    settings = self.ui._load_app_settings()
                    settings["desktop_mode_enabled"] = False
                    self.ui._save_app_settings(settings)
            except Exception:
                pass
            result = self.status()
            result["restored_process_priorities"] = restored
            return result

    def shutdown(self) -> dict[str, Any]:
        """Release runtime resources without clearing the user's desktop-mode preference."""
        with self._lock:
            was_enabled = self.enabled
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
                native_window_host.close_all()
            except Exception:
                pass
            try:
                web_application_host.close_all()
            except Exception:
                pass
            return {
                "enabled_before_shutdown": was_enabled,
                "restored_process_priorities": restored,
                "desktop_preference_preserved": True,
            }

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
            decision = status.get("decision") or {}
            game_mode = game_active or str(decision.get("mode") or "").lower() == "game"
            if self.layer:
                self.layer.set_low_power(game_mode)
                pressure = str(decision.get("pressure") or "normal").lower()
                if game_mode:
                    quality = "low"
                elif pressure == "high" or (
                    snapshot.get("gpu_percent") is not None
                    and float(snapshot.get("gpu_percent") or 0.0) >= 92.0
                ):
                    quality = "low"
                elif pressure == "elevated":
                    quality = "balanced"
                else:
                    quality = "full"
                self.layer.set_render_quality(quality)
                self.layer.set_performance_status(
                    self.performance.profile,
                    snapshot.get("cpu_percent"),
                    snapshot.get("memory_percent"),
                    game_active,
                    visible=self._show_overlay,
                )
                self.layer.restack()
            # Hidden/minimized web panels can be frozen safely while a game is
            # foreground; visible panels remain Active per Qt's lifecycle rules.
            try:
                web_application_host.set_low_power(game_mode, discard=False)
            except Exception:
                pass
            self._reconcile_workspace(snapshot)
            return self.status()

    def _reconcile_workspace(self, snapshot: dict[str, Any]) -> None:
        foreground = WindowManager.foreground()
        if not foreground or not foreground.visible or not foreground.title:
            return

        identity = f"{foreground.exe}:{foreground.title}".strip(":")
        now = time.time()
        rect = WindowManager.get_rect(foreground.hwnd)
        is_surface = bool(rect and rect[2] - rect[0] >= 160 and rect[3] - rect[1] >= 120)
        try:
            fullscreen = WindowManager.is_fullscreen_or_borderless(foreground.hwnd)
        except Exception:
            fullscreen = False

        stored_geometry = None
        try:
            state = self.workspace.snapshot()
            windows = state.get("workspaces", {}).get("main", {}).get("windows", [])
            for item in windows if isinstance(windows, list) else []:
                if str(item.get("identity") or "") != identity:
                    continue
                values = [item.get("x"), item.get("y"), item.get("width"), item.get("height")]
                if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                    stored_geometry = tuple(int(value) for value in values)
                break
        except Exception:
            pass

        if is_surface and not fullscreen and rect is not None:
            geometry = (int(rect[0]), int(rect[1]), int(rect[2] - rect[0]), int(rect[3] - rect[1]))
        else:
            geometry = stored_geometry

        geometry_changed = geometry != self._last_workspace_geometry
        identity_changed = identity != self._last_workspace_identity
        if not identity_changed:
            if not geometry_changed and now - self._last_workspace_persist_at < 30.0:
                return
            if geometry_changed and now - self._last_workspace_persist_at < 5.0:
                return

        self.workspace.upsert_window("main", {
            "identity": identity,
            "title": foreground.title,
            "exe": foreground.exe,
            "last_seen": now,
            "game": is_game_window(foreground),
            "x": geometry[0] if geometry else None,
            "y": geometry[1] if geometry else None,
            "width": geometry[2] if geometry else None,
            "height": geometry[3] if geometry else None,
        })
        self._last_workspace_identity = identity
        self._last_workspace_geometry = geometry
        self._last_workspace_persist_at = now

    def restore_workspace_geometry(self) -> int:
        """Restore saved windowed geometry for an unambiguous live user window."""
        restored = 0
        try:
            state = self.workspace.snapshot()
            saved = state.get("workspaces", {}).get("main", {}).get("windows", [])
            if not isinstance(saved, list):
                return restored
            live = WindowManager.enumerate_windows()
        except Exception:
            return restored

        for item in saved:
            if not isinstance(item, dict):
                continue
            exe = str(item.get("exe") or "").strip().lower()
            title = str(item.get("title") or "").strip()
            values = [item.get("x"), item.get("y"), item.get("width"), item.get("height")]
            if not exe or not title or not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                continue
            x, y, width, height = (int(value) for value in values)
            if not (-20000 <= x <= 20000 and -20000 <= y <= 20000):
                continue
            if not (160 <= width <= 10000 and 120 <= height <= 10000):
                continue
            matches = [
                window for window in live
                if window.visible and not window.minimized
                and window.exe.lower() == exe and window.title.strip() == title
                and not is_game_window(window)
            ]
            if len(matches) != 1:
                continue
            window = matches[0]
            try:
                if WindowManager.is_fullscreen_or_borderless(window.hwnd):
                    continue
            except Exception:
                continue
            if WindowManager.set_position(window.hwnd, x, y, width, height):
                restored += 1
        return restored
    def open(self, target: str, *, embed: bool = False) -> dict[str, Any]:
        value = str(target or "").strip()
        if not value:
            return {"ok": False, "error": "No application or URL supplied."}

        if value.startswith(("http://", "https://", "www.")):
            accent = "#00e5ff"
            try:
                import ui as ui_module
                accent = str(getattr(getattr(ui_module, "C", None), "PRI", accent) or accent)
            except Exception:
                pass
            url = value if "://" in value else f"https://{value}"
            return web_application_host.open(url, accent=accent)

        if not embed:
            return application_host.open(value)

        # Launch/reuse the actual application first, then wait briefly for its
        # top-level HWND. Hosting a process that has not created a window yet
        # would otherwise fail immediately for games and normal GUI apps.
        baseline = {
            item.pid
            for item in WindowManager.enumerate_windows()
            if item.pid
        }
        launch_result = application_host.open(value)
        if not launch_result.get("ok"):
            return launch_result

        window = application_host.find_after_launch(
            value,
            baseline_pids=baseline,
            timeout=6.0,
        )
        if window is None:
            return {
                **launch_result,
                "embedded": False,
                "embedding": "window-not-found",
                "message": "Application launched, but its window could not be safely hosted yet.",
            }

        hosted = native_window_host.host(str(window.hwnd))
        if hosted.get("ok"):
            return {
                **launch_result,
                **hosted,
                "embedded": True,
            }

        # Safe fallback: never kill or repeatedly reparent a program that refused
        # native hosting. The real process remains open normally.
        return {
            **launch_result,
            "embedded": False,
            "embedding": "fallback-external",
            "window": {
                "hwnd": window.hwnd,
                "pid": window.pid,
                "title": window.title,
                "exe": window.exe,
            },
            "message": hosted.get("error") or "Native hosting was not compatible; application remains external.",
        }

    def host_native(self, target: str) -> dict[str, Any]:
        return native_window_host.host(target)

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
            "use_workerw": self._use_workerw,
            "desktop_backend": self.layer.desktop_backend() if self.layer else ("WorkerW" if self._use_workerw else "bottommost-window"),
            "last_error": self.last_error,
            "performance": self.performance.status(),
            "web_panels": web_application_host.status(),
            "web_lifecycle": web_application_host.lifecycle_status(),
            "native_panels": native_window_host.status(),
        }
        return result
