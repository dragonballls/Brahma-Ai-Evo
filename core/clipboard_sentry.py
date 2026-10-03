"""
Clipboard Sentry for Brahma AI.
Monitors the Windows clipboard in the background for actionable technical content:
- Tracebacks / Exceptions
- JSON structures
- URLs or SQL queries
- Code snippets
Notifies Brahma so it can offer quick contextual assistance.
"""

import time
import threading
import json
import re
from typing import Optional, Callable


class ClipboardSentry:
    def __init__(self, callback: Optional[Callable[[str, str], None]] = None):
        self._callback = callback
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._last_clip = ""
        self._last_callback_at = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="clipboard-sentry",
        )
        self._thread.start()

    def stop(self):
        self._running = False
        self._wake.set()

    def _classify_content(self, text: str) -> Optional[str]:
        text = text.strip()
        if len(text) < 15:
            return None

        # Check for python / node / java traceback
        if (
            "Traceback (most recent call last):" in text
            or "Exception:" in text
            or ("Error:" in text and "\n" in text)
        ):
            return "error_traceback"

        # Check for JSON
        if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
            try:
                json.loads(text)
                return "json_data"
            except Exception:
                pass

        # Check for SQL queries
        if re.search(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s+", text, re.IGNORECASE):
            return "sql_query"

        # Check for code blocks
        if any(keyword in text for keyword in ["def ", "class ", "function ", "import ", "const ", "let ", "var "]) and "\n" in text:
            return "code_snippet"

        return None

    def _monitor_loop(self):
        import pyperclip
        try:
            self._last_clip = (pyperclip.paste() or "").strip()
        except Exception:
            self._last_clip = ""

        while self._running:
            # Keep the existing lightweight polling fallback, but wake instantly
            # when stop() is called instead of leaving a worker sleeping.
            if self._wake.wait(2.5):
                break
            try:
                current = (pyperclip.paste() or "").strip()
                if not current or current == self._last_clip:
                    continue
                self._last_clip = current
                # Avoid retaining or processing enormous clipboard payloads.
                if len(current) > 100_000:
                    continue
                category = self._classify_content(current)
                now = time.monotonic()
                if (
                    category
                    and self._callback
                    and (now - self._last_callback_at) >= 0.75
                ):
                    self._last_callback_at = now
                    self._callback(category, current)
            except Exception:
                pass
