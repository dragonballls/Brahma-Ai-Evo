from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QWindow
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .window_manager import WindowInfo, WindowManager


@dataclass
class NativeHostRecord:
    hwnd: int
    pid: int
    title: str
    exe: str


class NativeWindowPanel(QFrame):
    """Brahma-styled host for a foreign Windows HWND.

    Qt handles the foreign-window container/reparenting path. The panel owns
    the QWindow wrapper, not the underlying application process/window.
    """

    def __init__(self, info: WindowInfo, on_closed=None, parent=None):
        super().__init__(parent)
        self.info = info
        self._on_closed = on_closed
        self._foreign: QWindow | None = None
        self._container: QWidget | None = None

        self.setObjectName("NativeWindowPanel")
        self.setStyleSheet(
            """
            QFrame#NativeWindowPanel {
                background: rgba(5, 8, 14, 248);
                border: 1px solid rgba(0, 229, 255, 0.35);
                border-radius: 16px;
            }
            QLabel {
                background: transparent;
                color: rgba(255,255,255,0.86);
            }
            QPushButton {
                background: rgba(255,255,255,0.05);
                color: rgba(255,255,255,0.88);
                border: 1px solid rgba(255,255,255,0.09);
                border-radius: 7px;
                padding: 3px 8px;
            }
            QPushButton:hover {
                background: rgba(0,229,255,0.14);
                border-color: rgba(0,229,255,0.42);
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        icon = QLabel("◈")
        icon.setStyleSheet("color:#00e5ff; font-weight:700;")
        header.addWidget(icon)

        label = QLabel(info.title or info.exe or f"Window {info.hwnd}")
        label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        header.addWidget(label, 1)

        status = QLabel(f"{info.exe} • PID {info.pid}")
        status.setStyleSheet("color:rgba(255,255,255,0.48); font:8pt 'Segoe UI';")
        header.addWidget(status)

        close = QPushButton("×")
        close.setFixedSize(30, 28)
        close.setToolTip("Close hosted workspace")
        close.clicked.connect(self.close)
        header.addWidget(close)
        root.addLayout(header)

        foreign = QWindow.fromWinId(int(info.hwnd))
        if foreign is None:
            raise RuntimeError("Windows/Qt could not wrap the target HWND.")
        self._foreign = foreign

        self._container = QWidget.createWindowContainer(foreign, self)
        self._container.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._container.setMinimumSize(320, 220)
        self._container.setStyleSheet(
            "background:#000000; border:1px solid rgba(255,255,255,0.08); border-radius:10px;"
        )
        root.addWidget(self._container, 1)

    def focus_foreign(self) -> bool:
        try:
            if self._foreign is None:
                return False
            self._foreign.requestActivate()
            return WindowManager.focus(self.info.hwnd)
        except Exception:
            return False

    def closeEvent(self, event):
        try:
            # Detaching the QWindow wrapper removes the container relationship;
            # the native application/window remains alive.
            if self._foreign is not None:
                self._foreign.setParent(None)
        except Exception:
            pass
        try:
            if self._on_closed:
                self._on_closed(self)
        except Exception:
            pass
        super().closeEvent(event)


class NativeWindowHost:
    """Creates isolated Brahma workspace panels for native Windows windows."""

    def __init__(self):
        self._panels: list[NativeWindowPanel] = []

    @staticmethod
    def supported() -> bool:
        return platform.system() == "Windows" and WindowManager.available()

    @staticmethod
    def find_target(target: str) -> WindowInfo | None:
        text = str(target or "").strip().lower()
        if not text:
            return None
        windows = WindowManager.enumerate_windows()
        try:
            hwnd = int(text)
        except Exception:
            hwnd = None
        if hwnd is not None:
            return next((item for item in windows if item.hwnd == hwnd), None)
        exact = next(
            (
                item for item in windows
                if text == item.title.lower() or text == item.exe.lower()
            ),
            None,
        )
        if exact:
            return exact
        return next(
            (
                item for item in windows
                if text in item.title.lower() or text in item.exe.lower()
            ),
            None,
        )

    @staticmethod
    def _safe_target(info: WindowInfo | None) -> tuple[bool, str]:
        if info is None:
            return False, "Target window was not found."
        if not info.hwnd or not info.pid:
            return False, "Target window does not have a valid native handle."
        try:
            import psutil
            proc = psutil.Process(info.pid)
            if not WindowManager.is_user_process(proc):
                return False, "Protected or Windows-owned processes cannot be hosted."
        except Exception as exc:
            return False, f"Process could not be validated: {exc}"
        return True, ""

    def host(self, target: str) -> dict[str, Any]:
        if not self.supported():
            return {"ok": False, "embedded": False, "error": "Native window hosting is only available on Windows."}

        info = self.find_target(target)
        safe, error = self._safe_target(info)
        if not safe:
            return {"ok": False, "embedded": False, "error": error}

        assert info is not None
        existing = next((panel for panel in self._panels if panel.info.hwnd == info.hwnd), None)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.focus_foreign()
            return {"ok": True, "embedded": True, "hwnd": info.hwnd, "title": info.title}

        try:
            panel = NativeWindowPanel(info, on_closed=self._on_closed)
            self._panels.append(panel)
            panel.setMinimumSize(640, 420)
            panel.resize(1180, 760)
            panel.show()
            panel.raise_()
            panel.focus_foreign()
            return {
                "ok": True,
                "embedded": True,
                "hwnd": info.hwnd,
                "pid": info.pid,
                "title": info.title,
                "exe": info.exe,
            }
        except Exception as exc:
            return {
                "ok": False,
                "embedded": False,
                "error": f"Native hosting failed safely; the application was not terminated. {exc}",
            }

    def _on_closed(self, panel: NativeWindowPanel):
        self._panels = [item for item in self._panels if item is not panel]

    def close_all(self) -> int:
        count = 0
        for panel in list(self._panels):
            try:
                panel.close()
                count += 1
            except Exception:
                pass
        self._panels.clear()
        return count

    def status(self) -> list[dict[str, Any]]:
        result = []
        for panel in list(self._panels):
            try:
                if panel.isVisible():
                    result.append({
                        "hwnd": panel.info.hwnd,
                        "pid": panel.info.pid,
                        "title": panel.info.title,
                        "exe": panel.info.exe,
                        "embedded": True,
                    })
            except RuntimeError:
                continue
        return result


native_window_host = NativeWindowHost()
