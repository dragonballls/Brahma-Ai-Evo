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
    from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, QApplication
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - keeps lightweight imports available to CI/tools
    QUrl = Qt = QFont = QFrame = QHBoxLayout = QLabel = QPushButton = QVBoxLayout = QWidget = QApplication = QWebEngineView = None
    _QT_AVAILABLE = False


if _QT_AVAILABLE:
    class WebApplicationWindow(QWidget):
        """Brahma-styled browser panel backed by the real Qt Chromium/WebEngine."""

        def __init__(
            self,
            url: str,
            title: str | None = None,
            on_closed: Callable | None = None,
            parent=None,
            accent: str = "#00e5ff",
        ):
            super().__init__(parent)
            self._on_closed = on_closed
            self._url = normalize_web_url(url)
            self._accent = str(accent or "#00e5ff")

            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
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
                f"""
                QFrame#BrahmaWebApp {{
                    background: rgba(7, 10, 16, 248);
                    border: 1px solid {self._accent};
                    border-radius: 16px;
                }}
                QLabel {{
                    background: transparent;
                    color: rgba(255,255,255,0.85);
                }}
                QPushButton {{
                    background: rgba(255,255,255,0.05);
                    color: rgba(255,255,255,0.88);
                    border: 1px solid rgba(255,255,255,0.09);
                    border-radius: 7px;
                    padding: 3px 8px;
                }}
                QPushButton:hover {{
                    background: rgba(0,229,255,0.14);
                    border-color: {self._accent};
                    color: #ffffff;
                }}
                """
            )
            root.addWidget(frame)

            layout = QVBoxLayout(frame)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)

            header = QHBoxLayout()
            header.setSpacing(6)

            icon = QLabel("◉")
            icon.setStyleSheet(f"color:{self._accent}; font-weight:700;")
            header.addWidget(icon)

            self._title = QLabel(title or self._url)
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
            self._web.setUrl(QUrl(self._url))
            self._web.visibilityChanged.connect(self._sync_lifecycle) if hasattr(self._web, "visibilityChanged") else None

        @staticmethod
        def _normalize_url(url: str) -> str:
            return normalize_web_url(url)

        def _set_lifecycle(self, state_name: str) -> bool:
            try:
                page = self._web.page()
                lifecycle = getattr(page, "LifecycleState", None)
                setter = getattr(page, "setLifecycleState", None)
                if lifecycle is None or setter is None:
                    return False
                state = getattr(lifecycle, state_name, None)
                if state is None:
                    return False
                setter(state)
                return True
            except Exception:
                return False

        def _sync_lifecycle(self) -> None:
            # Visible pages must remain Active in Qt WebEngine. Minimized/hidden
            # panels may safely be Frozen; Discarded is reserved for explicit
            # deep-idle cleanup because returning from it reloads the page.
            try:
                if self.isVisible() and not self.isMinimized():
                    self._set_lifecycle("Active")
                else:
                    self._set_lifecycle("Frozen")
            except Exception:
                pass

        def set_deep_idle(self, enabled: bool, *, discard: bool = False) -> bool:
            try:
                if enabled:
                    if self.isVisible() and not self.isMinimized():
                        return False
                    return self._set_lifecycle("Discarded" if discard else "Frozen")
                return self._set_lifecycle("Active")
            except Exception:
                return False

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


else:
    class WebApplicationWindow:  # type: ignore[no-redef]
        @staticmethod
        def _normalize_url(url: str) -> str:
            return normalize_web_url(url)

        def __init__(self, *args, **kwargs):
            raise RuntimeError("Qt WebEngine is unavailable.")


class WebApplicationHost:
    """Owns Brahma web panels and keeps them out of the normal browser process."""

    def __init__(self):
        self._windows: list[WebApplicationWindow] = []

    def open(self, url: str, *, accent: str = "#00e5ff") -> dict:
        if not _QT_AVAILABLE:
            return {
                "ok": False,
                "type": "web",
                "embedded": False,
                "error": "Qt WebEngine is unavailable.",
            }

        value = str(url or "").strip()
        if not value:
            return {"ok": False, "error": "No URL supplied."}

        target = normalize_web_url(value)

        for window in list(self._windows):
            try:
                if not window.isVisible():
                    continue
                if window._url.startswith(target):
                    window.show()
                    window.raise_()
                    window.activateWindow()
                    return {
                        "ok": True,
                        "type": "web",
                        "embedded": True,
                        "url": window._url,
                    }
            except RuntimeError:
                continue

        window = WebApplicationWindow(
            target,
            on_closed=self._on_closed,
            accent=accent,
        )
        self._windows = [w for w in self._windows if w is not None]
        self._windows.append(window)

        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            count = len(self._windows)
            offset = min(48 * (count - 1), 180)
            width = min(1100, max(720, area.width() - 180))
            height = min(780, max(520, area.height() - 150))
            width = min(width, max(420, area.width() - 30))
            height = min(height, max(300, area.height() - 70))
            x = area.left() + max(20, (area.width() - width) // 2) + offset
            y = area.top() + max(20, (area.height() - height) // 2) + offset
            window.setGeometry(x, y, width, height)

        window.show()
        window.raise_()
        window.activateWindow()
        return {"ok": True, "type": "web", "embedded": True, "url": target}

    def set_low_power(self, enabled: bool, *, discard: bool = False) -> int:
        changed = 0
        for window in list(self._windows):
            try:
                if window.set_deep_idle(bool(enabled), discard=discard):
                    changed += 1
            except RuntimeError:
                continue
        return changed

    def lifecycle_status(self) -> list[dict]:
        result = []
        for window in list(self._windows):
            try:
                state = "unknown"
                page = window._web.page()
                current = getattr(page, "lifecycleState", None)
                if callable(current):
                    current = current()
                state = getattr(current, "name", str(current))
                result.append({
                    "url": window._url,
                    "visible": window.isVisible(),
                    "minimized": window.isMinimized(),
                    "lifecycle": state,
                })
            except Exception:
                continue
        return result

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
                    result.append(
                        {
                            "url": window._url,
                            "title": window._title.text(),
                            "visible": True,
                        }
                    )
            except RuntimeError:
                continue
        return result


web_application_host = WebApplicationHost()
