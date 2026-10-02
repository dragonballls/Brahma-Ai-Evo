"""Safe, fast-forward-only updater for Brahma Evo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


BRANCH = os.environ.get("BRAHMA_UPDATE_BRANCH", "main")
REMOTE_URL = os.environ.get(
    "BRAHMA_UPDATE_REMOTE_URL",
    "https://github.com/dragonballls/Brahma-Ai-Evo.git",
)


def _run_git(base_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=base_dir,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def _remote_url(base_dir: Path) -> str | None:
    remote = _run_git(base_dir, "remote", "get-url", "origin")
    if remote.returncode == 0 and remote.stdout.strip():
        return remote.stdout.strip()
    return None


def update_from_github(base_dir: Path) -> bool:
    """Fast-forward a clean Git checkout and return True when a restart is needed.

    No force reset, stash, checkout, or overwrite operation is performed.
    """
    if os.environ.get("BRAHMA_SKIP_UPDATE") == "1":
        return False
    base_dir = Path(base_dir).resolve()
    if not (base_dir / ".git").exists():
        return False

    status = _run_git(base_dir, "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        return False

    remote = _remote_url(base_dir)
    if not remote:
        add = _run_git(base_dir, "remote", "add", "origin", REMOTE_URL)
        if add.returncode != 0:
            return False

    fetch = _run_git(base_dir, "fetch", "--quiet", "origin", BRANCH)
    if fetch.returncode != 0:
        return False

    local = _run_git(base_dir, "rev-parse", "HEAD")
    remote_head = _run_git(base_dir, "rev-parse", f"origin/{BRANCH}")
    if local.returncode != 0 or remote_head.returncode != 0:
        return False
    if local.stdout.strip() == remote_head.stdout.strip():
        return False

    ancestor = _run_git(
        base_dir, "merge-base", "--is-ancestor", "HEAD", f"origin/{BRANCH}"
    )
    if ancestor.returncode != 0:
        # Never auto-merge or overwrite a branch that diverged.
        return False

    pull = _run_git(base_dir, "merge", "--ff-only", f"origin/{BRANCH}")
    return pull.returncode == 0


def restart_application(base_dir: Path) -> None:
    """Restart the current Python application after a successful update."""
    main_path = Path(base_dir) / "main.py"
    if not main_path.exists():
        raise FileNotFoundError(f"Cannot restart: {main_path} does not exist.")
    os.execv(sys.executable, [sys.executable, str(main_path), *sys.argv[1:]])
