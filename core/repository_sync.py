"""Optional publication of verified Brahma runtime repairs back to Git."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Iterable

from core.command_safety import hidden_creationflags, resolve_git_executable


class RepositorySyncError(RuntimeError):
    pass


def resolve_repository() -> Path | None:
    configured = os.environ.get("BRAHMA_REPOSITORY_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    configured_alt = os.environ.get("BRAHMA_SELF_CODING_REPO", "").strip()
    if configured_alt:
        candidates.append(Path(configured_alt).expanduser())

    here = Path(__file__).resolve()
    candidates.extend([here.parent.parent, Path.cwd()])

    for candidate in candidates:
        try:
            root = candidate.resolve()
        except Exception:
            continue
        for parent in (root, *root.parents):
            git_entry = parent / ".git"
            # Normal checkouts have a .git directory; linked Git worktrees
            # have a .git file pointing to the worktree-specific Git dir.
            if git_entry.is_dir() or git_entry.is_file():
                return parent
    return None


def _run(repo: Path, args: Iterable[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    git = resolve_git_executable(repo)
    return subprocess.run(
        [git, *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout,
        creationflags=hidden_creationflags(),
        check=False,
    )


def _canonical_remote(repo: Path) -> str:
    from core.runtime_paths import GITHUB_REMOTE
    return GITHUB_REMOTE


def ensure_remote(repo: Path) -> None:
    expected = _canonical_remote(repo)
    remote = _run(repo, ("remote", "get-url", "origin"))
    if remote.returncode == 0 and remote.stdout.strip():
        if remote.stdout.strip() != expected:
            fixed = _run(repo, ("remote", "set-url", "origin", expected))
            if fixed.returncode != 0:
                raise RepositorySyncError(fixed.stderr.strip() or "Unable to normalize origin remote.")
        return

    added = _run(repo, ("remote", "add", "origin", expected))
    if added.returncode != 0:
        raise RepositorySyncError(added.stderr.strip() or "Unable to create origin remote.")


def publish_verified_repair(target_file: str | Path, patch_id: str, explanation: str = "", repository_relative_path: str | None = None, expected_preimage_sha256: str | None = None, expected_postimage_sha256: str | None = None) -> dict:
    """Commit/push one already-verified runtime repair when an actual Git checkout exists.

    This deliberately does not touch self-coding checkpoints. It only publishes the
    file changed by AutoHealEngine after AST + py_compile verification have passed.
    """
    # Auto-heal may repair the local runtime, but promotion to main is explicit.
    # This keeps self-coding checkpoint/approval semantics intact unless the user
    # deliberately opts into automatic repository publication.
    if os.environ.get("BRAHMA_AUTO_PUBLISH_REPAIRS", "0").strip().lower() in {"0", "false", "no", "off"}:
        return {"published": False, "reason": "disabled"}

    repo = resolve_repository()
    if repo is None:
        return {"published": False, "reason": "no_git_checkout"}

    root = repo.resolve()
    target = Path(target_file).resolve()
    try:
        repo_target = target.relative_to(root)
    except ValueError:
        # Frozen/installed builds can repair an unpacked copy outside the Git checkout.
        # In that case the caller must explicitly provide the source-tree-relative path.
        relative = str(repository_relative_path or "").strip()
        if not relative:
            return {"published": False, "reason": "target_outside_repository"}
        repo_target = Path(relative)
        if repo_target.is_absolute() or ".." in repo_target.parts:
            return {"published": False, "reason": "invalid_repository_relative_path"}
        target = (root / repo_target).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return {"published": False, "reason": "target_outside_repository"}

    if not target.is_file():
        return {"published": False, "reason": "target_missing"}

    if expected_postimage_sha256:
        try:
            actual_postimage = hashlib.sha256(
                target.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
            ).hexdigest()
        except OSError:
            return {"published": False, "reason": "target_read_failed"}
        if actual_postimage != expected_postimage_sha256:
            return {"published": False, "reason": "target_changed_since_verification"}

    status = _run(root, ("status", "--porcelain"))
    if status.returncode != 0:
        return {"published": False, "reason": "git_status_failed"}

    # The repair engine normally operates on a clean file. If unrelated work appeared
    # between verification and publication, refusing to publish is safer than mixing it.
    lines = [line for line in status.stdout.splitlines() if line.strip()]
    allowed = {str(repo_target).replace("\\", "/")}
    if any(line[3:].strip().replace("\\", "/") not in allowed for line in lines if len(line) >= 4):
        return {"published": False, "reason": "unrelated_working_tree_changes"}

    ensure_remote(root)

    branch = _run(root, ("branch", "--show-current"))
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        return {
            "published": False,
            "reason": "repository_not_on_main",
            "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        }

    fetch = _run(root, ("fetch", "origin", "main", "--quiet"), timeout=180)
    if fetch.returncode != 0:
        return {"published": False, "reason": "fetch_failed", "detail": fetch.stderr.strip()[-1000:]}

    local = _run(root, ("rev-parse", "HEAD"))
    remote = _run(root, ("rev-parse", "refs/remotes/origin/main"))
    if local.returncode != 0 or remote.returncode != 0:
        return {"published": False, "reason": "revision_check_failed"}

    # Never overwrite remote work. The repair may only fast-forward an unchanged main.
    if local.stdout.strip() != remote.stdout.strip():
        return {"published": False, "reason": "remote_main_changed"}

    if expected_preimage_sha256:
        try:
            repo_target_text = str(repo_target).replace("\\", "/")
            head_file = _run(root, ("show", f"HEAD:{repo_target_text}"))
            if head_file.returncode != 0:
                return {"published": False, "reason": "target_preimage_unavailable"}
            actual_preimage = hashlib.sha256(head_file.stdout.encode("utf-8")).hexdigest()
        except (OSError, UnicodeError):
            return {"published": False, "reason": "target_preimage_read_failed"}
        if actual_preimage != expected_preimage_sha256:
            return {"published": False, "reason": "target_preimage_changed"}

    add = _run(root, ("add", "--", str(repo_target)))
    if add.returncode != 0:
        return {"published": False, "reason": "stage_failed", "detail": add.stderr.strip()[-1000:]}

    staged = _run(root, ("diff", "--cached", "--name-only"))
    if staged.returncode != 0 or not staged.stdout.strip():
        return {"published": False, "reason": "nothing_staged"}

    message = f"auto-heal: verified repair {patch_id}"
    if explanation:
        message += f" — {explanation[:120]}"
    commit = _run(root, ("commit", "-m", message), timeout=180)
    if commit.returncode != 0:
        _run(root, ("reset", "HEAD", "--", str(target.relative_to(root))))
        return {"published": False, "reason": "commit_failed", "detail": commit.stderr.strip()[-1200:]}

    pushed = _run(root, ("push", "origin", "main"), timeout=300)
    if pushed.returncode != 0:
        # Keep the local commit; do not rewrite history automatically. The caller can
        # report that remote publication failed while the verified repair remains safe locally.
        return {
            "published": False,
            "reason": "push_failed",
            "local_commit": _run(root, ("rev-parse", "HEAD")).stdout.strip(),
            "detail": pushed.stderr.strip()[-1200:],
        }

    commit_sha = _run(root, ("rev-parse", "HEAD")).stdout.strip()
    return {
        "published": True,
        "repository": str(root),
        "branch": "main",
        "commit_sha": commit_sha,
        "patch_id": patch_id,
        "target_file": str(target),
    }
