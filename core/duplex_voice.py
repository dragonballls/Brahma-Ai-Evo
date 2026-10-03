"""Low-cost duplex voice coordination primitives for Brahma Evo."""

from __future__ import annotations

import threading


class BargeInGate:
    """Debounces likely user speech while Brahma is speaking.

    The server-side Live API remains authoritative for activity/turn detection.
    This gate exists only to stop local playback quickly, before the server's
    interruption event arrives.
    """

    def __init__(self, *, required_blocks: int = 2, minimum_level: float = 20.0) -> None:
        self.required_blocks = max(1, int(required_blocks))
        self.minimum_level = max(0.0, float(minimum_level))
        self._lock = threading.Lock()
        self._positive_blocks = 0

    def reset(self) -> None:
        with self._lock:
            self._positive_blocks = 0

    @property
    def positive_blocks(self) -> int:
        with self._lock:
            return self._positive_blocks

    def observe(self, *, is_user_speech: bool, level: float) -> bool:
        """Return True exactly when a likely interruption is established."""
        with self._lock:
            if bool(is_user_speech) and float(level) >= self.minimum_level:
                self._positive_blocks += 1
            else:
                self._positive_blocks = 0
            return self._positive_blocks >= self.required_blocks


class PlaybackGeneration:
    """Monotonic generation marker used to discard stale audio after interruption."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0

    def current(self) -> int:
        with self._lock:
            return self._generation

    def bump(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation
