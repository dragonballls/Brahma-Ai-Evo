from __future__ import annotations

import platform
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QFrame, QLabel, QMainWindow, QVBoxLayout, QWidget

from .window_manager import WindowManager

if platform.system() == "Windows":
    _OS = "Windows"
else:
    _OS = platform.system()


class DesktopLayer(QMainWindow):
    """A non-activating, click-through Brahma desktop surface."""

    def __init__(self, base_dir: Path, parent=None):
        super().__init__(parent)
        self._base_dir = Path(base_dir)
        self.setWindowTitle("Brahma Evo Desktop")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        central = QWidget(self)
        central.setStyleSheet("background:#020306;")
        central.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        html_path = self._base_dir / "assets" / "web_background" / "index.html"
        self._web = QWebEngineView(central)
        self._web.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._web.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._web.setStyleSheet("background:#020306; border:none;")
        self._web.settings().setAttribute(
            self._web.settings().WebAttribute.WebGLEnabled, True
        )
        self._web.settings().setAttribute(
            self._web.settings().WebAttribute.Accelerated2dCanvasEnabled, True
        )
        if html_path.exists():
            self._web.load(QUrl.fromLocalFile(str(html_path.resolve())))
        root.addWidget(self._web, 1)

        self._status = QFrame(central)
        self._status.setObjectName("DesktopStatus")
        self._status.setStyleSheet(
            """
            QFrame#DesktopStatus {
                background: rgba(5, 8, 14, 180);
                border: 1px solid rgba(0, 229, 255, 0.24);
                border-radius: 12px;
            }
            QLabel {
                background: transparent;
                color: rgba(255,255,255,0.76);
                font: 600 8pt "Segoe UI";
            }
            """
        )
        status_layout = QVBoxLayout(self._status)
        status_layout.setContentsMargins(10, 7, 10, 7)
        status_layout.setSpacing(2)
        self._mode = QLabel("BRAHMA • DESKTOP", self._status)
        self._detail = QLabel("Adaptive performance", self._status)
        status_layout.addWidget(self._mode)
        status_layout.addWidget(self._detail)
        self._status.setFixedWidth(220)
        self._status.move(18, 18)
        self._status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        central.setParent(self)
        self.setCentralWidget(central)

        self._restack = QTimer(self)
        self._restack.setInterval(2000)
        self._restack.timeout.connect(self._keep_bottom)
        self._restack.start()

    def _virtual_geometry(self):
        from PyQt6.QtWidgets import QApplication
        screens = QApplication.screens()
        if not screens:
            return QApplication.primaryScreen().availableGeometry()
        rect = screens[0].geometry()
        for screen in screens[1:]:
            rect = rect.united(screen.geometry())
        return rect

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_geometry()
        QTimer.singleShot(0, self._keep_bottom)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._status.move(18, 18)

    def _apply_geometry(self):
        self.setGeometry(self._virtual_geometry())

    def restack(self) -> None:
        self._keep_bottom()

    def _keep_bottom(self):
        try:
            self._apply_geometry()
            if WindowManager.available():
                WindowManager.set_desktop_layer_style(int(self.winId()))
        except Exception:
            pass

    def set_performance_status(self, profile: str, cpu: float | None, memory: float | None,
                               game_active: bool, visible: bool = False):
        if not visible:
            self._status.hide()
            return
        self._status.show()
        state = "GAME" if game_active else str(profile or "adaptive").upper()
        self._mode.setText(f"BRAHMA • {state}")
        cpu_text = "—" if cpu is None else f"{cpu:.0f}%"
        mem_text = "—" if memory is None else f"{memory:.0f}%"
        self._detail.setText(f"CPU {cpu_text}  •  RAM {mem_text}")

    def set_low_power(self, enabled: bool) -> None:
        try:
            page = self._web.page()
            page.runJavaScript(
                f"if(window.setDeepIdle) window.setDeepIdle({str(bool(enabled)).lower()});"
            )
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            self._restack.stop()
        except Exception:
            pass
        super().closeEvent(event)
