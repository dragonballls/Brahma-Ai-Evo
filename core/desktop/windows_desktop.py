from __future__ import annotations

import ctypes
import platform

if platform.system() == "Windows":
    try:
        import win32gui
        _WIN32_AVAILABLE = True
    except Exception:
        win32gui = None
        _WIN32_AVAILABLE = False
else:
    win32gui = None
    _WIN32_AVAILABLE = False


class WindowsDesktopHost:
    """Optional WorkerW bridge with validation and clean detachment.

    It is deliberately not the default backend because SetParent across
    processes has DPI-awareness implications. The caller must opt in.
    """

    _PROGMAN_MESSAGE = 0x052C
    _SMTO_ABORTIFHUNG = 0x0002
    _GWL_STYLE = -16
    _GWL_EXSTYLE = -20
    _WS_CHILD = 0x40000000
    _WS_POPUP = 0x80000000
    _WS_EX_TOOLWINDOW = 0x00000080
    _WS_EX_NOACTIVATE = 0x08000000
    _WS_EX_TRANSPARENT = 0x00000020
    _HWND_BOTTOM = 1
    _SWP_NOMOVE = 0x0002
    _SWP_NOSIZE = 0x0001
    _SWP_NOACTIVATE = 0x0010
    _SWP_FRAMECHANGED = 0x0020
    _SWP_SHOWWINDOW = 0x0040

    def __init__(self):
        self.attached = False
        self._workerw = 0
        self._original_parent = 0
        self._original_style = None
        self._original_exstyle = None

    @staticmethod
    def available() -> bool:
        return _WIN32_AVAILABLE

    @classmethod
    def _send_desktop_message(cls, progman: int) -> None:
        try:
            result = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(
                ctypes.c_void_p(int(progman)),
                ctypes.c_uint(cls._PROGMAN_MESSAGE),
                ctypes.c_size_t(0),
                ctypes.c_ssize_t(0),
                ctypes.c_uint(cls._SMTO_ABORTIFHUNG),
                ctypes.c_uint(1000),
                ctypes.byref(result),
            )
        except Exception:
            try:
                win32gui.SendMessage(int(progman), cls._PROGMAN_MESSAGE, 0, 0)
            except Exception:
                pass

    @classmethod
    def find_workerw(cls) -> int:
        if not _WIN32_AVAILABLE:
            return 0

        progman = int(win32gui.FindWindow("Progman", "Program Manager") or 0)
        if not progman:
            progman = int(win32gui.FindWindow("Progman", None) or 0)
        if progman:
            cls._send_desktop_message(progman)

        workerw = 0

        def callback(hwnd, _):
            nonlocal workerw
            if workerw:
                return
            try:
                definition_view = win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None)
                if definition_view:
                    candidate = win32gui.FindWindowEx(0, hwnd, "WorkerW", None)
                    if candidate:
                        workerw = int(candidate)
            except Exception:
                pass

        try:
            win32gui.EnumWindows(callback, None)
        except Exception:
            return 0
        return workerw

    def client_size(self, hwnd: int) -> tuple[int, int]:
        if not _WIN32_AVAILABLE or not hwnd:
            return (0, 0)
        try:
            left, top, right, bottom = win32gui.GetClientRect(int(hwnd))
            return (max(0, right - left), max(0, bottom - top))
        except Exception:
            return (0, 0)

    def attach(self, hwnd: int, virtual_width: int, virtual_height: int) -> bool:
        if not _WIN32_AVAILABLE or not hwnd or self.attached:
            return False

        workerw = self.find_workerw()
        if not workerw:
            return False

        worker_width, worker_height = self.client_size(workerw)
        if worker_width < int(virtual_width) or worker_height < int(virtual_height):
            return False

        try:
            hwnd = int(hwnd)
            self._workerw = int(workerw)
            self._original_parent = int(win32gui.GetParent(hwnd) or 0)
            self._original_style = int(win32gui.GetWindowLong(hwnd, self._GWL_STYLE))
            self._original_exstyle = int(win32gui.GetWindowLong(hwnd, self._GWL_EXSTYLE))

            win32gui.SetWindowLong(
                hwnd,
                self._GWL_STYLE,
                (self._original_style | self._WS_CHILD) & ~self._WS_POPUP,
            )
            win32gui.SetWindowLong(
                hwnd,
                self._GWL_EXSTYLE,
                self._original_exstyle
                | self._WS_EX_TOOLWINDOW
                | self._WS_EX_NOACTIVATE
                | self._WS_EX_TRANSPARENT,
            )

            win32gui.SetParent(hwnd, self._workerw)
            if int(win32gui.GetParent(hwnd) or 0) != self._workerw:
                raise RuntimeError("WorkerW parent was not established.")

            win32gui.SetWindowPos(
                hwnd,
                self._HWND_BOTTOM,
                0,
                0,
                worker_width,
                worker_height,
                self._SWP_NOACTIVATE | self._SWP_FRAMECHANGED | self._SWP_SHOWWINDOW,
            )
            self.attached = True
            return True
        except Exception:
            self.detach(hwnd)
            return False

    def detach(self, hwnd: int) -> bool:
        if not _WIN32_AVAILABLE or not hwnd:
            self.attached = False
            return False

        ok = True
        try:
            win32gui.SetParent(int(hwnd), int(self._original_parent or 0))
        except Exception:
            ok = False
        try:
            if self._original_style is not None:
                win32gui.SetWindowLong(int(hwnd), self._GWL_STYLE, int(self._original_style))
            if self._original_exstyle is not None:
                win32gui.SetWindowLong(int(hwnd), self._GWL_EXSTYLE, int(self._original_exstyle))
            win32gui.SetWindowPos(
                int(hwnd),
                self._HWND_BOTTOM,
                0,
                0,
                0,
                0,
                self._SWP_NOMOVE | self._SWP_NOSIZE | self._SWP_NOACTIVATE | self._SWP_FRAMECHANGED,
            )
        except Exception:
            ok = False

        self.attached = False
        self._workerw = 0
        self._original_parent = 0
        self._original_style = None
        self._original_exstyle = None
        return ok

    def status(self) -> dict:
        return {
            "available": self.available(),
            "attached": self.attached,
            "workerw": self._workerw or None,
            "backend": "WorkerW" if self.attached else "bottommost-window",
        }
