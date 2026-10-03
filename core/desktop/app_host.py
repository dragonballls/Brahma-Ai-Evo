from __future__ import annotations

import os
import platform
import subprocess
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .window_manager import WindowInfo, WindowManager


class ApplicationHost:
    """Universal application facade.

    Phase one deliberately uses real Windows applications and manages their
    native windows instead of pretending to reimplement the applications.
    The docking API makes an application part of a Brahma workspace even when
    native child-window hosting is not safe for that application.
    """

    def list_windows(self) -> list[dict[str, Any]]:
        return [asdict(window) for window in WindowManager.enumerate_windows()]

    def open(self, target: str) -> dict[str, Any]:
        target = str(target or "").strip()
        if not target:
            return {"ok": False, "error": "No application or URL supplied."}

        if target.startswith(("http://", "https://", "www.")):
            url = target if "://" in target else f"https://{target}"
            try:
                from .web_host import web_application_host
                return web_application_host.open(url)
            except Exception:
                try:
                    webbrowser.open(url, new=0)
                    return {"ok": True, "type": "web", "embedded": False, "target": url}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

        try:
            if platform.system() == "Windows" and Path(target).exists():
                process = subprocess.Popen(
                    [str(Path(target).resolve())],
                    cwd=str(Path(target).resolve().parent),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            elif platform.system() == "Windows":
                # Reuse Brahma's existing application alias/launcher first so
                # commands such as "open Chrome" continue to use the same
                # tested resolution path as the normal assistant tool.
                try:
                    from actions.open_app import open_app as existing_open_app
                    existing_result = existing_open_app(
                        parameters={"app_name": target},
                        response=None,
                        player=None,
                    )
                    lowered = str(existing_result or "").lower()
                    if not lowered.startswith(("could not", "error", "failed")):
                        return {"ok": True, "type": "native", "target": target, "launcher_result": str(existing_result)}
                except Exception:
                    pass
                # Final Windows-native fallback for registered applications,
                # URI handlers, and .lnk files.
                os.startfile(target)  # type: ignore[attr-defined]
                process = None
            else:
                process = subprocess.Popen([target])
            return {
                "ok": True,
                "type": "native",
                "target": target,
                "pid": getattr(process, "pid", None),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def find_after_launch(self, target: str, *, baseline_pids: set[int] | None = None,
                         timeout: float = 6.0) -> WindowInfo | None:
        """Find the launched application's real top-level window without guessing forever."""
        import time as _time
        target_text = str(target or "").strip().lower()
        baseline = set(baseline_pids or set())
        deadline = _time.monotonic() + max(0.5, min(float(timeout), 10.0))

        while _time.monotonic() < deadline:
            windows = WindowManager.enumerate_windows()
            candidates: list[WindowInfo] = []
            for window in windows:
                if not window.visible or not window.title:
                    continue
                if window.pid in baseline:
                    continue
                text = f"{window.title} {window.exe}".lower()
                if target_text and target_text in text:
                    candidates.append(window)
                elif is_game_window(window) and target_text in {"minecraft", "roblox"}:
                    candidates.append(window)

            if candidates:
                return candidates[0]

            # Existing-window fallback: useful when the launcher correctly
            # focuses an already-running application instead of spawning a PID.
            for window in windows:
                if not window.visible or not window.title:
                    continue
                text = f"{window.title} {window.exe}".lower()
                if target_text and target_text in text:
                    return window

            _time.sleep(0.25)
        return None

    def _find(self, hwnd_or_title: str) -> WindowInfo | None:
        text = str(hwnd_or_title or "").strip()
        if not text:
            return None
        try:
            hwnd = int(text)
        except Exception:
            hwnd = None
        windows = WindowManager.enumerate_windows()
        if hwnd is not None:
            for window in windows:
                if window.hwnd == hwnd:
                    return window
        lower = text.lower()
        for window in windows:
            if lower in window.title.lower() or lower == window.exe.lower():
                return window
        return None

    def control(self, action: str, target: str, *, x: int | None = None,
                y: int | None = None, width: int | None = None,
                height: int | None = None) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        window = self._find(target)
        if window is None:
            return {"ok": False, "error": f"Window '{target}' was not found."}

        fn = {
            "focus": WindowManager.focus,
            "minimize": WindowManager.minimize,
            "maximize": WindowManager.maximize,
            "restore": WindowManager.restore,
            "close": WindowManager.close,
        }.get(action)
        if fn:
            return {"ok": bool(fn(window.hwnd)), "hwnd": window.hwnd, "action": action}

        if action in {"move", "resize", "dock"}:
            rect = WindowManager.get_rect(window.hwnd)
            if rect is None:
                return {"ok": False, "error": "The window geometry could not be read safely."}
            left, top, right, bottom = rect
            current_width = max(1, right - left)
            current_height = max(1, bottom - top)

            if action == "move":
                if x is None or y is None:
                    return {"ok": False, "error": "move requires x and y."}
                target_x, target_y = int(x), int(y)
                target_width, target_height = current_width, current_height
            elif action == "resize":
                if width is None or height is None:
                    return {"ok": False, "error": "resize requires width and height."}
                target_x, target_y = left, top
                target_width, target_height = max(120, int(width)), max(120, int(height))
            else:
                target_x = left if x is None else int(x)
                target_y = top if y is None else int(y)
                target_width = current_width if width is None else max(120, int(width))
                target_height = current_height if height is None else max(120, int(height))

            return {
                "ok": WindowManager.set_position(
                    window.hwnd,
                    target_x,
                    target_y,
                    target_width,
                    target_height,
                ),
                "hwnd": window.hwnd,
                "action": action,
                "geometry": {
                    "x": target_x,
                    "y": target_y,
                    "width": target_width,
                    "height": target_height,
                },
            }

        return {"ok": False, "error": f"Unsupported window action: {action}."}


application_host = ApplicationHost()
