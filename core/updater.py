"""Background update detection and safe fast-forward application for Brahma Evo."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal


class UpdateChecker(QObject):
    update_available_sig = pyqtSignal(str)

    def __init__(self, repo_owner: str = "", repo_name: str = "", branch: str = "main", base_dir: str | Path | None = None):
        super().__init__()
        self.branch = branch or os.environ.get("BRAHMA_UPDATE_BRANCH", "main")
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent.parent).resolve()
        self._stop_event = threading.Event()
        self._check_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._check_thread and self._check_thread.is_alive():
            return
        self._stop_event.clear()
        self._check_thread = threading.Thread(
            target=self._check_loop,
            daemon=True,
            name="brahma-updater",
        )
        self._check_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._check_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)

    def _get_local_hash(self) -> str | None:
        try:
            output = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.base_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return output.stdout.strip() if output.returncode == 0 else None
        except Exception:
            return None

    def _get_remote_hash(self) -> str | None:
        try:
            output = subprocess.run(
                ["git", "ls-remote", "origin", f"refs/heads/{self.branch}"],
                cwd=self.base_dir,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if output.returncode != 0:
                return None
            first = output.stdout.strip().splitlines()
            return first[0].split()[0] if first and first[0].split() else None
        except Exception as exc:
            print(f"[Updater] Error checking remote revision: {exc}")
            return None

    def _check_loop(self) -> None:
        while not self._stop_event.is_set():
            local_hash = self._get_local_hash()
            remote_hash = self._get_remote_hash()

            if (
                local_hash
                and remote_hash
                and local_hash != remote_hash
            ):
                print(
                    f"[Updater] Update detected! "
                    f"Local: {local_hash[:7]}, Remote: {remote_hash[:7]}"
                )
                self.update_available_sig.emit(remote_hash)
                return

            # Event.wait() is interruptible and avoids one-second polling loops.
            self._stop_event.wait(3600)


def apply_update_and_restart(base_dir: str | Path | None = None) -> bool:
    """Apply only a clean fast-forward update, then restart the application."""
    try:
        from updater import restart_application, update_from_github

        root = Path(base_dir or Path(__file__).resolve().parent.parent).resolve()
        changed = update_from_github(root)
        if changed:
            print("[Updater] Safe update applied. Restarting application...")
            restart_application(root)
            return True
        print("[Updater] Update was not applied (working tree may be dirty or branch diverged).")
    except Exception as exc:
        print(f"[Updater] Failed to apply update: {exc}")
    return False
