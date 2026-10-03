from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

_OS = platform.system()

if _OS == "Windows":
    try:
        import win32con
        import win32gui
        import win32process
        _WIN32_AVAILABLE = True
    except Exception:
        win32con = win32gui = win32process = None
        _WIN32_AVAILABLE = False
else:
    _WIN32_AVAILABLE = False


PROTECTED_PROCESS_NAMES = {
    "system",
    "system idle process",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "dwm.exe",
    "explorer.exe",
    "fontdrvhost.exe",
    "sihost.exe",
    "taskhostw.exe",
    "ctfmon.exe",
    "runtimebroker.exe",
    "securityhealthservice.exe",
    "msmpeng.exe",
    "microsoft.securityhealthsystray.exe",
}


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    exe: str
    minimized: bool
    visible: bool


class WindowManager:
    """Thin Windows adapter.

    It deliberately contains no policy.  The performance governor decides when
    a process may be adjusted; this layer only performs individual operations.
    """

    @staticmethod
    def available() -> bool:
        return _WIN32_AVAILABLE

    @staticmethod
    def enumerate_windows() -> list[WindowInfo]:
        if not _WIN32_AVAILABLE or psutil is None:
            return []

        items: list[WindowInfo] = []

        def callback(hwnd, _extra):
            try:
                if not win32gui.IsWindow(hwnd):
                    return
                title = (win32gui.GetWindowText(hwnd) or "").strip()
                visible = bool(win32gui.IsWindowVisible(hwnd))
                if not title and not visible:
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if not pid:
                    return
                try:
                    proc = psutil.Process(pid)
                    exe = Path(proc.exe()).name.lower()
                except Exception:
                    exe = ""
                items.append(
                    WindowInfo(
                        hwnd=int(hwnd),
                        pid=int(pid),
                        title=title,
                        exe=exe,
                        minimized=bool(win32gui.IsIconic(hwnd)),
                        visible=visible,
                    )
                )
            except Exception:
                return

        try:
            win32gui.EnumWindows(callback, None)
        except Exception:
            return []

        return items

    @staticmethod
    def foreground() -> WindowInfo | None:
        if not _WIN32_AVAILABLE or psutil is None:
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            exe = ""
            try:
                exe = Path(psutil.Process(pid).exe()).name.lower()
            except Exception:
                pass
            return WindowInfo(
                hwnd=int(hwnd),
                pid=int(pid),
                title=title,
                exe=exe,
                minimized=bool(win32gui.IsIconic(hwnd)),
                visible=bool(win32gui.IsWindowVisible(hwnd)),
            )
        except Exception:
            return None

    @staticmethod
    def is_protected_process(proc: object) -> bool:
        try:
            name = str(proc.name() or "").lower()
        except Exception:
            return True
        return name in PROTECTED_PROCESS_NAMES

    @staticmethod
    def is_user_process(proc: object) -> bool:
        if psutil is None:
            return False
        if WindowManager.is_protected_process(proc):
            return False
        try:
            exe = str(proc.exe() or "").lower()
        except Exception:
            exe = ""
        if not exe:
            return False

        # Never tune Windows system binaries or processes from Windows folders.
        windows_root = os.environ.get("WINDIR", r"C:\Windows").lower().rstrip("\\/")
        try:
            if exe.startswith(windows_root + "\\"):
                return False
        except Exception:
            pass
        return True

    @staticmethod
    def get_priority(proc: object):
        try:
            return proc.nice()
        except Exception:
            return None

    @staticmethod
    def set_priority(proc: object, priority) -> bool:
        try:
            proc.nice(priority)
            return True
        except Exception:
            return False

    @staticmethod
    def get_memory_priority(pid: int) -> int | None:
        if _OS != "Windows" or not pid:
            return None
        try:
            import ctypes
            class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
                _fields_ = [("MemoryPriority", ctypes.c_ulong)]
            GetProcessInformation = ctypes.windll.kernel32.GetProcessInformation
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ProcessMemoryPriority = 0
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(pid),
            )
            if not handle:
                return None
            try:
                info = MEMORY_PRIORITY_INFORMATION()
                ok = GetProcessInformation(
                    handle,
                    ProcessMemoryPriority,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
                return int(info.MemoryPriority) if ok else None
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return None

    @staticmethod
    def set_memory_priority(pid: int, priority: int) -> bool:
        if _OS != "Windows" or not pid:
            return False
        try:
            import ctypes
            class MEMORY_PRIORITY_INFORMATION(ctypes.Structure):
                _fields_ = [("MemoryPriority", ctypes.c_ulong)]
            SetProcessInformation = ctypes.windll.kernel32.SetProcessInformation
            PROCESS_SET_INFORMATION = 0x0200
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ProcessMemoryPriority = 0
            value = max(1, min(5, int(priority)))
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(pid),
            )
            if not handle:
                return False
            try:
                info = MEMORY_PRIORITY_INFORMATION(value)
                return bool(
                    SetProcessInformation(
                        handle,
                        ProcessMemoryPriority,
                        ctypes.byref(info),
                        ctypes.sizeof(info),
                    )
                )
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False

    @staticmethod
    def trim_working_set(pid: int) -> bool:
        """Best-effort working-set trim; never raises and never terminates a process."""
        if _OS != "Windows" or not pid:
            return False
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            PROCESS_SET_QUOTA = 0x0100
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA,
                False,
                int(pid),
            )
            if not handle:
                return False
            try:
                psapi = ctypes.WinDLL("psapi.dll")
                result = psapi.EmptyWorkingSet(handle)
                return bool(result)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False

    @staticmethod
    def is_fullscreen_or_borderless(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        rect = WindowManager.get_rect(hwnd)
        if rect is None:
            return False
        try:
            import win32api
            monitor = win32api.MonitorFromPoint((rect[0], rect[1]), 1)
            if not monitor:
                return False
            info = win32api.GetMonitorInfo(monitor)
            mon_left, mon_top, mon_right, mon_bottom = info.get("Monitor", (0, 0, 0, 0))
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            monitor_width = mon_right - mon_left
            monitor_height = mon_bottom - mon_top
            # Treat exact monitor coverage (including borderless) as a game/fullscreen
            # surface. Tiny title-bar differences do not qualify.
            return (
                abs(rect[0] - mon_left) <= 2
                and abs(rect[1] - mon_top) <= 2
                and abs(width - monitor_width) <= 2
                and abs(height - monitor_height) <= 2
            )
        except Exception:
            return False

    @staticmethod
    def get_rect(hwnd: int) -> tuple[int, int, int, int] | None:
        if not _WIN32_AVAILABLE:
            return None
        try:
            left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
            return int(left), int(top), int(right), int(bottom)
        except Exception:
            return None

    @staticmethod
    def set_position(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.SetWindowPos(
                int(hwnd),
                win32con.HWND_TOP,
                int(x),
                int(y),
                max(1, int(width)),
                max(1, int(height)),
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def focus(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.ShowWindow(int(hwnd), win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(int(hwnd))
            return True
        except Exception:
            return False

    @staticmethod
    def minimize(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.ShowWindow(int(hwnd), win32con.SW_MINIMIZE)
            return True
        except Exception:
            return False

    @staticmethod
    def maximize(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.ShowWindow(int(hwnd), win32con.SW_MAXIMIZE)
            return True
        except Exception:
            return False

    @staticmethod
    def restore(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.ShowWindow(int(hwnd), win32con.SW_RESTORE)
            return True
        except Exception:
            return False

    @staticmethod
    def close(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.PostMessage(int(hwnd), win32con.WM_CLOSE, 0, 0)
            return True
        except Exception:
            return False

    @staticmethod
    def send_to_bottom(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            win32gui.SetWindowPos(
                int(hwnd),
                win32con.HWND_BOTTOM,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE |
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def set_desktop_layer_style(hwnd: int) -> bool:
        if not _WIN32_AVAILABLE:
            return False
        try:
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TRANSPARENT = 0x00000020
            style = win32gui.GetWindowLong(int(hwnd), GWL_EXSTYLE)
            style |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
            win32gui.SetWindowLong(int(hwnd), GWL_EXSTYLE, style)
            return WindowManager.send_to_bottom(int(hwnd))
        except Exception:
            return False


def is_game_window(window: WindowInfo | None) -> bool:
    if not window:
        return False
    text = f"{window.title} {window.exe}".lower()
    markers = (
        "minecraft", "roblox", "fortnite", "valorant", "apex legends",
        "counter-strike", "cs2", "overwatch", "league of legends",
        "elden ring", "grand theft auto", "gta v", "cyberpunk",
        "fallout", "skyrim", "rocket league", "steamvr",
    )
    return any(marker in text for marker in markers)
