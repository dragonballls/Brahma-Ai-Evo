from __future__ import annotations

import atexit
import os
from pathlib import Path


class SingleInstance:
    """Process-wide singleton guard using an OS-native named lock."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str, lock_path: Path | None = None):
        self.name = str(name)
        self._handle = None
        self._lock_file = None
        self._acquired = False
        self._lock_path = lock_path or (
            Path(os.environ.get("LOCALAPPDATA", Path.home()))
            / "Brahma Evo"
            / f"{self._safe_name()}.lock"
        )

    def _safe_name(self) -> str:
        return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in self.name)

    def acquire(self) -> bool:
        if self._acquired:
            return True

        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise OSError(ctypes.get_last_error() or 1, "CreateMutexW failed")

            if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False

            self._handle = handle
            self._acquired = True
            atexit.register(self.release)
            return True

        # POSIX fallback keeps development/CI behavior deterministic.
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            return False

        self._acquired = True
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if not self._acquired:
            return

        if os.name == "nt":
            try:
                import ctypes

                if self._handle:
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                    kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        else:
            try:
                import fcntl

                if self._lock_file:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                    self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None

        self._acquired = False

