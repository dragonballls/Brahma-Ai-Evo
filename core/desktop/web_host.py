from __future__ import annotations

from typing import Callable


def normalize_web_url(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith(("http://", "https://", "file://")):
        return value
    return f"https://{value}"


try:
    from PyQt6.QtCore import QUrl, Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - GUI dependency is optional for lightweight tooling/tests
    QUrl = Qt = QFont = QFrame = QHBoxLayout = QLabel = QPushButton = QVBoxLayout = QWidget = QWebEngineView = None
    _QT_AVAILABLE = False

if _QT_AVAILABLE:
    class WebApplicationWindow(QWidget):
        """A Brahma-styled web application surface using Qt WebEngine."""
    
        def __init__(self, url: str, title: str | None = None, on_closed: Callable | None = None, parent=None):
            super().__init__(parent)
            self._on_closed = on_closed
            self._url = normalize_web_url(url)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowStaysOnTopHint
            )
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self.setMinimumSize(420, 300)
            self.resize(980, 720)
    
            root = QVBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(0)
    
            frame = QFrame()
            frame.setObjectName("BrahmaWebApp")
            frame.setStyleSheet(
                """
                QFrame#BrahmaWebApp {
                    background: rgba(7, 10, 16, 248);
                    border: 1px solid rgba(0, 229, 255, 0.35);
                    border-radius: 16px;
                }
                QLabel { background: transparent; color: rgba(255,255,255,0.85); }
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
                    color: #ffffff;
                }
                """
            )
            root.addWidget(frame)
    
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)
    
            header = QHBoxLayout()
            header.setSpacing(6)
            icon = QLabel("◉")
            icon.setStyleSheet("color:#00e5ff; font-weight:700;")
            header.addWidget(icon)
    
            self._title = QLabel(title or url)
            self._title.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            header.addWidget(self._title, 1)
    
            back = QPushButton("‹")
            back.setToolTip("Back")
            back.clicked.connect(lambda: self._web.back())
            forward = QPushButton("›")
            forward.setToolTip("Forward")
            forward.clicked.connect(lambda: self._web.forward())
            reload_btn = QPushButton("↻")
            reload_btn.setToolTip("Reload")
            reload_btn.clicked.connect(lambda: self._web.reload())
            close = QPushButton("×")
            close.setToolTip("Close")
            close.clicked.connect(self.close)
            for btn in (back, forward, reload_btn, close):
                btn.setFixedSize(30, 28)
                header.addWidget(btn)
            layout.addLayout(header)
    
            self._web = QWebEngineView(frame)
            settings = self._web.settings()
            settings.setAttribute(settings.WebAttribute.WebGLEnabled, True)
            settings.setAttribute(settings.WebAttribute.Accelerated2dCanvasEnabled, True)
            settings.setAttribute(settings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            settings.setAttribute(settings.WebAttribute.LocalContentCanAccessFileUrls, True)
            self._web.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
            self._web.setStyleSheet(
                "background:#020306; border:1px solid rgba(255,255,255,0.08); border-radius:10px;"
            )
            self._web.urlChanged.connect(self._on_url_changed)
            layout.addWidget(self._web, 1)
            self._web.setUrl(QUrl(self._normalize_url(url)))
    
        @staticmethod
        def _normalize_url(url: str) -> str:
            return normalize_web_url(url)
    
        def _on_url_changed(self, qurl: QUrl):
            self._url = qurl.toString()
            try:
                self._title.setText(self._url[:120])
            except Exception:
                pass
    
        def closeEvent(self, event):
            try:
                if self._on_closed:
                    self._on_closed(self)
            except Exception:
                pass
            super().closeEvent(event)
    class WebApplicationHost:
    """Owns Brahma web application windows and keeps them visually consistent."""

    def __init__(self):
        self._windows: list[WebApplicationWindow] = []

    def open(self, url: str) -> dict:
        if not _QT_AVAILABLE:
            return {"ok": False, "type": "web", "embedded": False, "error": "Qt WebEngine is unavailable."}
        value = str(url or "").strip()
        if not value:
            return {"ok": False, "error": "No URL supplied."}

        target = value if value.startswith(("http://", "https://", "file://")) else f"https://{value}"
        for window in list(self._windows):
            if window is None:
                continue
            try:
                if not window.isVisible():
                    continue
                current = window._url
                if current.startswith(target):
                    window.show()
                    window.raise_()
                    window.activateWindow()
                    return {"ok": True, "type": "web", "embedded": True, "url": current}
            except RuntimeError:
                continue

        window = WebApplicationWindow(target, on_closed=self._on_closed)
        self._windows = [w for w in self._windows if w is not None]
        self._windows.append(window)

        count = len(self._windows)
        screen = window.screen()
        if screen is None:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            offset = min(48 * (count - 1), 180)
            width = min(1100, max(720, area.width() - 180))
            height = min(780, max(520, area.height() - 150))
            x = area.left() + max(40, (area.width() - width) // 2) + offset
            y = area.top() + max(30, (area.height() - height) // 2) + offset
            window.setGeometry(x, y, min(width, area.width() - 30), min(height, area.height() - 70))

        window.show()
        window.raise_()
        window.activateWindow()
        return {"ok": True, "type": "web", "embedded": True, "url": target}

    def _on_closed(self, widget: WebApplicationWindow):
        self._windows = [w for w in self._windows if w is not widget]

    def close_all(self):
        for window in list(self._windows):
            try:
                window.close()
            except Exception:
                pass
        self._windows.clear()

    def status(self) -> list[dict]:
        result = []
        for window in list(self._windows):
            try:
                if window.isVisible():
                    result.append({"url": window._url, "title": window._title.text(), "visible": True})
            except RuntimeError:
                continue
        return result


web_application_host = WebApplicationHost()


if not _QT_AVAILABLE:
    class WebApplicationWindow:  # type: ignore[no-redef]
        @staticmethod
        def _normalize_url(url: str) -> str:
            return normalize_web_url(url)


web_application_host = WebApplicationHost()
