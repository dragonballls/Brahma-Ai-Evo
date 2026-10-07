import os
import sys
import subprocess
import threading
import time
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal

from core.runtime_paths import GITHUB_OWNER, GITHUB_REPOSITORY, GITHUB_BRANCH
from core.network_safety import fetch_public_bytes

class UpdateChecker(QObject):
    update_available_sig = pyqtSignal(str)

    def __init__(self, repo_owner=GITHUB_OWNER, repo_name=GITHUB_REPOSITORY, branch=GITHUB_BRANCH, base_dir=None):
        super().__init__()
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.branch = branch
        self.base_dir = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent.parent
        self._stop_event = threading.Event()
        self._check_thread = None
        self._lifecycle_lock = threading.Lock()

    def start(self):
        with self._lifecycle_lock:
            if self._check_thread is not None and self._check_thread.is_alive():
                return
            self._stop_event.clear()
            self._check_thread = threading.Thread(
                target=self._check_loop,
                daemon=True,
                name="updater-thread",
            )
            self._check_thread.start()

    def stop(self):
        self._stop_event.set()
        with self._lifecycle_lock:
            thread = self._check_thread
        if thread:
            thread.join(timeout=1.0)
        with self._lifecycle_lock:
            if self._check_thread is thread and (thread is None or not thread.is_alive()):
                self._check_thread = None

    def _remote_is_safe_update(self, remote_hash: str) -> bool:
        if not remote_hash:
            return False
        try:
            fetch = subprocess.run(
                ["git", "fetch", "origin", self.branch, "--quiet"],
                cwd=self.base_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                check=False,
            )
            if fetch.returncode != 0:
                return False
            local = self._get_local_hash()
            if not local:
                return False
            upstream = subprocess.run(
                ["git", "rev-parse", f"origin/{self.branch}"],
                cwd=self.base_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                text=True,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                check=False,
            )
            if upstream.returncode != 0 or upstream.stdout.strip() != remote_hash:
                return False
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", local, remote_hash],
                cwd=self.base_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                check=False,
            )
            return ancestry.returncode == 0 and local != remote_hash
        except (OSError, subprocess.SubprocessError):
            return False

    def check_now(self) -> str | None:
        """Check GitHub once and emit when a newer commit is available."""
        local_hash = self._get_local_hash()
        remote_hash = self._get_remote_hash()
        if local_hash and remote_hash and self._remote_is_safe_update(remote_hash):
            self.update_available_sig.emit(remote_hash)
            return remote_hash
        return None

    def _get_local_hash(self):
        try:
            output = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.base_dir,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            return output.decode("utf-8").strip()
        except Exception:
            return None

    def _get_remote_hash(self):
        url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/commits/{self.branch}"
        try:
            status, raw = fetch_public_bytes(
                url,
                timeout=10,
                max_response_bytes=64 * 1024,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "BrahmaEvo-Updater/1",
                },
            )
            if status == 200:
                import json
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict):
                    return data.get("sha")
        except Exception as e:
            print(f"[Updater] Error fetching remote hash: {e}")
        return None

    def _check_loop(self):
        try:
            while not self._stop_event.is_set():
                local_hash = self._get_local_hash()
                remote_hash = self._get_remote_hash()

                if local_hash and remote_hash and self._remote_is_safe_update(remote_hash):
                    print(f"[Updater] Update detected! Local: {local_hash[:7]}, Remote: {remote_hash[:7]}")
                    self.update_available_sig.emit(remote_hash)
                    break  # Stop checking once a safe fast-forward update is detected.

                # Poll every 6 hours and sleep in one interruptible wait.
                self._stop_event.wait(timeout=21600)
        finally:
            with self._lifecycle_lock:
                if self._check_thread is threading.current_thread():
                    self._check_thread = None

def apply_update_and_restart(base_dir=None):
    """Apply only fast-forward updates, then restart; never discard local work."""
    print("[Updater] Applying update...")
    try:
        repo_dir = Path(base_dir or Path(__file__).resolve().parent.parent)
        from updater import restart_application, update_from_github

        changed = update_from_github(repo_dir)
        if not changed:
            print("[Updater] No safe update was applied; local changes may exist or GitHub is already current.")
            return False

        print("[Updater] Update applied successfully. Restarting application...")
        restart_application(repo_dir)
        return True
    except Exception as e:
        print(f"[Updater] Failed to apply update: {e}")
        return False
