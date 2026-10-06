"""Guarded self-coding for Brahma Evo.

Ported from the proven JARVIS checkpoint model and adapted to Brahma's existing
BrahmaDevAgent. Coding runs only from a clean attached Git branch, verifies
before commit, creates a durable pending checkpoint, and requires explicit
approval before main is changed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid
from typing import Any

from core.efficiency_policy import EFFICIENCY_DIRECTIVE


_SELF_CODING_LOCK = threading.RLock()


class SelfCodingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    branch: str
    baseline: str
    base_branch: str
    commits: tuple[str, ...]
    created_at: str
    state: str
    promoted_sha: str | None = None
    undo_commits: tuple[str, ...] = ()


class SelfCodingAgent:
    def __init__(self, repo: Path | None = None) -> None:
        self.repo = self._resolve_repo(repo)
        if not self.repo.is_dir():
            raise SelfCodingError(f"Repository does not exist: {self.repo}")

    @staticmethod
    def _resolve_repo(repo: Path | None) -> Path:
        if repo:
            return Path(repo).expanduser().resolve()

        env_repo = os.environ.get("BRAHMA_SELF_CODING_REPO", "").strip()
        candidates: list[Path] = []
        if env_repo:
            candidates.append(Path(env_repo).expanduser())

        here = Path(__file__).resolve()
        candidates.extend([here.parent.parent, Path.cwd()])
        for candidate in candidates:
            for parent in (candidate, *candidate.parents):
                if (parent / ".git").is_dir():
                    return parent.resolve()

        raise SelfCodingError(
            "No real Git checkout was found. Set BRAHMA_SELF_CODING_REPO "
            "to the Brahma-Ai-Evo working tree before using self-coding."
        )

    @property
    def checkpoint_dir(self) -> Path:
        return self.repo / ".git" / "brahma-checkpoints"

    @staticmethod
    def _hidden_creationflags() -> int:
        """Keep Git/self-coding subprocesses invisible during normal GUI operation."""
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _run(self, args: list[str] | tuple[str, ...], timeout: int = 900) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=timeout,
                creationflags=self._hidden_creationflags(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SelfCodingError(f"Command failed to start: {args[0]}") from exc

    def _git(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return self._run(("git", *args), timeout=timeout)

    def _branch(self) -> str:
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if result.returncode != 0:
            raise SelfCodingError(result.stderr.strip() or "Unable to determine Git branch.")
        value = result.stdout.strip()
        if not value or value == "HEAD":
            raise SelfCodingError("Self-coding requires an attached Git branch.")
        return value

    def validate_repo(self) -> None:
        if not (self.repo / ".git").exists():
            raise SelfCodingError("Self-coding requires a real Git repository.")
        root = self._git("rev-parse", "--show-toplevel")
        if root.returncode != 0 or Path(root.stdout.strip()).resolve() != self.repo:
            raise SelfCodingError("Configured self-coding root is not the Git repository root.")
        status = self._git("status", "--porcelain")
        if status.returncode != 0:
            raise SelfCodingError(status.stderr.strip() or "Unable to inspect Git status.")
        if status.stdout.strip():
            raise SelfCodingError("Repository is not clean; self-coding refuses to overwrite existing work.")
        self._branch()

    @staticmethod
    def _slug(goal: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", goal.casefold()).strip("-")
        return (value[:40].rstrip("-") or "change")

    def _new_branch(self, goal: str) -> tuple[str, str, str]:
        head = self._git("rev-parse", "HEAD")
        if head.returncode != 0:
            raise SelfCodingError("Unable to read the baseline commit.")
        baseline = head.stdout.strip()
        base_branch = self._branch()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"agent/checkpoint/{stamp}-{self._slug(goal)}-{baseline[:8]}"
        result = self._git("switch", "-c", branch)
        if result.returncode != 0:
            raise SelfCodingError(result.stderr.strip() or "Unable to create checkpoint branch.")
        return branch, baseline, base_branch

    def _path(self, checkpoint_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", checkpoint_id):
            raise SelfCodingError("Invalid checkpoint id.")
        return self.checkpoint_dir / f"{checkpoint_id}.json"

    def _save(self, checkpoint: Checkpoint) -> None:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        target = self._path(checkpoint.checkpoint_id)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "branch": checkpoint.branch,
                "baseline": checkpoint.baseline,
                "base_branch": checkpoint.base_branch,
                "commits": list(checkpoint.commits),
                "created_at": checkpoint.created_at,
                "state": checkpoint.state,
                "promoted_sha": checkpoint.promoted_sha,
                "undo_commits": list(checkpoint.undo_commits),
            },
            indent=2,
        )
        try:
            temp.write_text(payload, encoding="utf-8")
            temp.replace(target)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _quarantine_checkpoint(self, checkpoint_id: str) -> None:
        path = self._path(checkpoint_id)
        if not path.exists():
            return
        quarantine = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:8]}")
        path.replace(quarantine)

    def _validate_checkpoint(self, checkpoint: Checkpoint, expected_checkpoint_id: str | None = None) -> None:
        if expected_checkpoint_id is not None and checkpoint.checkpoint_id != expected_checkpoint_id:
            raise SelfCodingError("Checkpoint metadata id does not match the requested checkpoint.")
        if checkpoint.base_branch != "main":
            raise SelfCodingError("Only checkpoints created from main can be promoted or undone.")
        if checkpoint.state not in {"pending", "promoting", "approved", "undoing", "undone"}:
            raise SelfCodingError("Checkpoint state metadata is invalid.")
        if not re.fullmatch(r"agent/checkpoint/[A-Za-z0-9._-]+", checkpoint.branch):
            raise SelfCodingError("Checkpoint branch metadata is invalid.")
        if checkpoint.branch.rsplit("/", 1)[-1] != checkpoint.checkpoint_id:
            raise SelfCodingError("Checkpoint branch does not match its checkpoint id.")
        if not re.fullmatch(r"[0-9a-f]{40}", checkpoint.baseline):
            raise SelfCodingError("Checkpoint baseline is invalid.")
        if not checkpoint.commits or any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in checkpoint.commits):
            raise SelfCodingError("Checkpoint commit metadata is invalid.")
        previous = checkpoint.baseline
        for commit in checkpoint.commits:
            parents = self._git("rev-list", "--parents", "-n", "1", commit)
            if parents.returncode != 0:
                raise SelfCodingError("Checkpoint references a missing Git commit.")
            fields = parents.stdout.strip().split()
            if len(fields) != 2 or fields[1] != previous:
                raise SelfCodingError("Checkpoint commits are not the expected linear chain.")
            previous = commit
        if checkpoint.promoted_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", str(checkpoint.promoted_sha)):
            raise SelfCodingError("Checkpoint promoted SHA is invalid.")
        if checkpoint.promoted_sha is not None and checkpoint.promoted_sha != checkpoint.commits[-1]:
            raise SelfCodingError("Checkpoint promoted SHA does not match the checkpoint tip.")
        if len(checkpoint.undo_commits) > len(checkpoint.commits):
            raise SelfCodingError("Checkpoint undo metadata contains too many commits.")
        if any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in checkpoint.undo_commits):
            raise SelfCodingError("Checkpoint undo metadata is invalid.")
        if checkpoint.undo_commits:
            previous = checkpoint.promoted_sha or checkpoint.commits[-1]
            for commit in checkpoint.undo_commits:
                parents = self._git("rev-list", "--parents", "-n", "1", commit)
                if parents.returncode != 0:
                    raise SelfCodingError("Checkpoint references a missing undo commit.")
                fields = parents.stdout.strip().split()
                if len(fields) != 2 or fields[1] != previous:
                    raise SelfCodingError("Checkpoint undo commits are not the expected linear chain.")
                previous = commit
    def _load(self, checkpoint_id: str) -> Checkpoint:
        path = self._path(checkpoint_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            checkpoint = Checkpoint(
                checkpoint_id=str(data["checkpoint_id"]),
                branch=str(data["branch"]),
                baseline=str(data["baseline"]),
                base_branch=str(data["base_branch"]),
                commits=tuple(str(x) for x in data.get("commits", [])),
                created_at=str(data["created_at"]),
                state=str(data["state"]),
                promoted_sha=data.get("promoted_sha"),
                undo_commits=tuple(str(x) for x in data.get("undo_commits", [])),
            )
            self._validate_checkpoint(checkpoint, checkpoint_id)
            return checkpoint
        except SelfCodingError:
            raise
        except Exception as exc:
            try:
                self._quarantine_checkpoint(checkpoint_id)
            except Exception:
                pass
            raise SelfCodingError("Checkpoint metadata is corrupt and was quarantined.") from exc
    def list_checkpoints(self) -> list[dict[str, Any]]:
        if not self.checkpoint_dir.is_dir():
            return []
        output: list[dict[str, Any]] = []
        for path in sorted(self.checkpoint_dir.glob("*.json"), reverse=True):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                output.append(
                    {
                        "checkpoint_id": item.get("checkpoint_id"),
                        "state": item.get("state"),
                        "branch": item.get("branch"),
                        "baseline": item.get("baseline"),
                        "promoted_sha": item.get("promoted_sha"),
                        "created_at": item.get("created_at"),
                    }
                )
            except Exception:
                continue
        return output

    @staticmethod
    def _coding_prompt(goal: str) -> str:
        return f"""You are Brahma Evo's guarded implementation agent.

Goal: {goal}

Rules:
- Work ONLY inside this Git repository.
- Read the existing code and tests before editing.
- Preserve all existing behavior unless this goal explicitly requires a change.
- Never access, print, copy, or modify API keys, tokens, private keys, browser profiles, or files outside the repository.
- Do not weaken authentication, permissions, safety guards, low-power behavior, or existing tests.
- Prefer small, reversible changes.
- Add or update tests for behavioral changes.
- Run the relevant repository tests before reporting success.
- Do NOT create Git commits, switch branches, push, or reset the repository; the outer checkpoint controller owns Git state.
- Never claim success when verification fails.
- Do not commit secrets or machine-specific configuration.
- Keep the existing architecture coherent; reuse existing modules instead of making duplicate systems.
- Apply the centralized efficiency policy: measure bottlenecks, reuse validated work, use exact cache keys, avoid duplicate verification, stream large data, and keep expensive optional work lazy without reducing quality or safety.

Implement the goal directly in the current repository and leave the working tree ready for verification.

EFFICIENCY-FIRST ENGINEERING POLICY:
- Measure or inspect the expensive step before optimizing it; do not optimize by guesswork.
- Reuse work when the relevant inputs are unchanged. Prefer exact content/configuration hashes,
  manifests, or other deterministic cache keys.
- Validate cache hits before trusting them. Stale, partial, incompatible, or corrupted caches
  must be rejected and rebuilt rather than silently used.
- Prefer incremental verification: run the smallest authoritative checks for a local change,
  then retain broader release gates when they protect system integrity.
- Do not repeat the same scan, archive traversal, compilation, or verification twice in one path
  unless the second pass has a distinct correctness purpose.
- Stream large files and archives instead of loading whole payloads into memory.
- Bound retries, polling, waits, and recovery loops while preserving diagnostics.
- Keep expensive optional work lazy and off the hot path until it is actually needed.
- Never cache API keys, tokens, credentials, private data, browser profiles, or user secrets.
- Keep cache invalidation conservative and tied to every input that can affect correctness.
- Optimize total user-visible latency and resource usage, not merely one local step.
- Never trade away correctness, security, safety, voice quality, feature behavior, or verification
  coverage merely to make a task faster.
- When performance changes materially, add a regression test or measurable guard proving equivalence.
- For autonomous repairs, make the smallest safe change first and reuse valid evidence; rerun
  affected verification when that evidence is invalidated."""
    
    def _verify(self) -> None:
        commands = (
            (sys.executable, "-m", "compileall", "-q", "actions", "core", "features", "memory", "plugins", "smart_home", "brahma_connect", "main.py", "ui.py", "updater.py", "or_client.py", "llm_client.py"),
            (sys.executable, "tests/test_repository_integrity.py"),
            (sys.executable, "tests/test_low_power_guards.py"),
            (sys.executable, "tests/test_runtime_consistency.py"),
            (sys.executable, "tests/test_conversation_delivery.py"),
            (sys.executable, "tests/test_live_voice_contract.py"),
        )
        optional_tests = (
            self.repo / "tests" / "test_voice_guards.py",
            self.repo / "tests" / "test_language_lock.py",
        )
        for test_path in optional_tests:
            if test_path.is_file():
                commands += ((sys.executable, str(test_path)),)
        for command in commands:
            result = self._run(command, timeout=900)
            if result.returncode != 0:
                output = (result.stdout + "\n" + result.stderr).strip()
                raise SelfCodingError(
                    f"Verification failed for {' '.join(str(x) for x in command)}.\n{output[-12000:]}"
                )

    def _rollback(self, baseline: str, branch: str, base_branch: str) -> None:
        reset = self._git("reset", "--hard", baseline)
        if reset.returncode != 0:
            raise SelfCodingError(reset.stderr.strip() or "Unable to restore the checkpoint baseline.")
        current = self._branch()
        if current == branch:
            switched = self._git("switch", base_branch)
            if switched.returncode != 0:
                raise SelfCodingError(
                    switched.stderr.strip() or "Unable to return to the original branch during rollback."
                )
        deleted = self._git("branch", "-D", branch)
        if deleted.returncode != 0:
            raise SelfCodingError(
                deleted.stderr.strip() or "Unable to remove the failed checkpoint branch."
            )
        status = self._git("status", "--porcelain")
        if status.stdout.strip():
            # Never run git clean here: untracked files may have been created by
            # another process while self-coding was in progress. Preserve them
            # rather than risking unrelated user data loss.
            raise SelfCodingError(
                "Rollback preserved untracked or working-tree changes; "
                "manual cleanup is required before another self-coding run."
            )

    def preview(self, goal: str, *, max_passes: int = 1, return_to_base: bool = False) -> dict[str, Any]:
        with _SELF_CODING_LOCK:
            return self._preview_unlocked(goal, max_passes=max_passes, return_to_base=return_to_base)

    def _preview_unlocked(
        self,
        goal: str,
        *,
        max_passes: int = 1,
        return_to_base: bool = False,
    ) -> dict[str, Any]:
        goal = str(goal or "").strip()
        if not goal:
            raise SelfCodingError("A non-empty self-coding goal is required.")
        if max_passes < 1 or max_passes > 3:
            raise SelfCodingError("max_passes must be between 1 and 3.")

        self.validate_repo()
        branch, baseline, base_branch = self._new_branch(goal)
        checkpoint_id = branch.split("/", 2)[-1]
        created_at = datetime.now(timezone.utc).isoformat()
        commits: list[str] = []
        try:
            for _ in range(max_passes):
                from actions.brahma_dev_agent import run_dev_agent
                result = run_dev_agent(
                    {
                        "description": self._coding_prompt(goal),
                        "workspace_path": str(self.repo),
                    }
                )
                if isinstance(result, str) and result.lower().startswith(("error:", "failed:")):
                    raise SelfCodingError(result)
                self._verify()
                status = self._git("status", "--porcelain")
                if status.returncode != 0 or not status.stdout.strip():
                    raise SelfCodingError("Coding pass produced no verified repository change.")
                add = self._git("add", "--all")
                if add.returncode != 0:
                    raise SelfCodingError(add.stderr.strip() or "Unable to stage coding changes.")
                commit = self._git(
                    "commit",
                    "-m",
                    f"self-coding: {self._slug(goal)}",
                    timeout=300,
                )
                if commit.returncode != 0:
                    raise SelfCodingError(commit.stderr.strip() or "Unable to create checkpoint commit.")
                head = self._git("rev-parse", "HEAD")
                if head.returncode != 0:
                    raise SelfCodingError("Unable to record checkpoint commit.")
                commits.append(head.stdout.strip())

            checkpoint = Checkpoint(
                checkpoint_id=checkpoint_id,
                branch=branch,
                baseline=baseline,
                base_branch=base_branch,
                commits=tuple(commits),
                created_at=created_at,
                state="pending",
            )
            self._save(checkpoint)
            result = {
                "success": True,
                "state": "pending",
                "checkpoint_id": checkpoint_id,
                "branch": branch,
                "baseline": baseline,
                "commits": commits,
                "message": "Verified checkpoint created; explicit approval is required before main changes.",
            }
            if return_to_base:
                switched = self._git("switch", base_branch)
                if switched.returncode != 0:
                    raise SelfCodingError(
                        switched.stderr.strip()
                        or "Verified checkpoint was created but the base branch could not be restored."
                    )
                result["returned_to_base"] = base_branch
            return result
        except Exception as exc:
            try:
                self._rollback(baseline, branch, base_branch)
            except Exception as rollback_exc:
                raise SelfCodingError(f"Self-coding failed and rollback also failed: {rollback_exc}") from exc
            raise SelfCodingError(f"Self-coding failed safely: {exc}") from exc

    def _recover_promoting(self, checkpoint: Checkpoint) -> str:
        if not checkpoint.promoted_sha:
            raise SelfCodingError("Promoting checkpoint has no promoted SHA.")
        self.validate_repo()
        remote = self._git("fetch", "origin", "main", timeout=300)
        if remote.returncode != 0:
            raise SelfCodingError(remote.stderr.strip() or "Unable to refresh remote main during promotion recovery.")
        remote_head = self._git("rev-parse", "refs/remotes/origin/main")
        local_head = self._git("rev-parse", "refs/heads/main")
        if remote_head.returncode != 0 or local_head.returncode != 0:
            raise SelfCodingError("Unable to inspect main during promotion recovery.")
        remote_sha = remote_head.stdout.strip()
        local_sha = local_head.stdout.strip()
        if remote_sha == checkpoint.promoted_sha and local_sha == checkpoint.promoted_sha:
            self._save(replace(checkpoint, state="approved", promoted_sha=checkpoint.promoted_sha))
            return checkpoint.promoted_sha
        if remote_sha == checkpoint.baseline and local_sha == checkpoint.promoted_sha:
            reset = self._git("reset", "--hard", checkpoint.baseline)
            if reset.returncode != 0:
                raise SelfCodingError(reset.stderr.strip() or "Unable to restore an unpublished promotion.")
            self._save(replace(checkpoint, state="pending", promoted_sha=None))
            return self._approve_unlocked(checkpoint_id=checkpoint.checkpoint_id)
        raise SelfCodingError("Promotion state is ambiguous; refusing to mutate main further.")
    def _recover_undoing(self, checkpoint: Checkpoint) -> str:
        if not checkpoint.promoted_sha:
            raise SelfCodingError("Undoing checkpoint is missing its promoted SHA.")
        self.validate_repo()
        fetched = self._git("fetch", "origin", "main", timeout=300)
        if fetched.returncode != 0:
            raise SelfCodingError(fetched.stderr.strip() or "Unable to refresh remote main during undo recovery.")
        remote = self._git("rev-parse", "refs/remotes/origin/main")
        local = self._git("rev-parse", "refs/heads/main")
        if remote.returncode != 0 or local.returncode != 0:
            raise SelfCodingError("Unable to inspect main during undo recovery.")
        remote_sha = remote.stdout.strip()
        local_sha = local.stdout.strip()
        if not checkpoint.undo_commits:
            if remote_sha == checkpoint.promoted_sha and local_sha == checkpoint.promoted_sha:
                self._save(replace(checkpoint, state="approved", undo_commits=()))
                return self._undo_unlocked(checkpoint.checkpoint_id)
            raise SelfCodingError("Undo started but no durable undo commit was recorded; main state is ambiguous.")
        undo_tip = checkpoint.undo_commits[-1]
        if local_sha != undo_tip:
            raise SelfCodingError("Local main does not match the durable undo checkpoint tip.")
        if remote_sha == undo_tip:
            self._save(replace(checkpoint, state="undone"))
            return "undone"
        if remote_sha != checkpoint.promoted_sha:
            raise SelfCodingError("Undo recovery found unrelated remote main changes; refusing further mutation.")
        pushed = self._git("push", "origin", "main", timeout=300)
        if pushed.returncode != 0:
            raise SelfCodingError(pushed.stderr.strip() or "Unable to publish the pending checkpoint undo.")
        self._save(replace(checkpoint, state="undone"))
        return "undone"

    def approve(self, checkpoint_id: str) -> str:
        with _SELF_CODING_LOCK:
            return self._approve_unlocked(checkpoint_id)

    def _approve_unlocked(self, checkpoint_id: str) -> str:
        checkpoint = self._load(checkpoint_id)
        self._validate_checkpoint(checkpoint)
        if checkpoint.state == "promoting":
            return self._recover_promoting(checkpoint)
        if checkpoint.state != "pending":
            raise SelfCodingError(f"Checkpoint is not pending: {checkpoint.state}")
        if checkpoint.base_branch != "main":
            raise SelfCodingError("Only checkpoints created from main can be promoted or undone.")
        self.validate_repo()
        head = self._git("rev-parse", checkpoint.branch)
        if head.returncode != 0 or head.stdout.strip() != checkpoint.commits[-1]:
            raise SelfCodingError("Checkpoint branch metadata does not match its current head.")
        fetched = self._git("fetch", "origin", "main", timeout=300)
        if fetched.returncode != 0:
            raise SelfCodingError(fetched.stderr.strip() or "Unable to fetch remote main.")
        remote = self._git("rev-parse", "refs/remotes/origin/main")
        local = self._git("rev-parse", "refs/heads/main")
        if remote.returncode != 0 or local.returncode != 0:
            raise SelfCodingError("Unable to inspect main before approval.")
        if remote.stdout.strip() != checkpoint.baseline or local.stdout.strip() != checkpoint.baseline:
            raise SelfCodingError("main changed since preview; refusing promotion.")
        previous = self._branch()
        self._save(replace(checkpoint, state="promoting"))
        switched = self._git("switch", "main")
        if switched.returncode != 0:
            self._save(checkpoint)
            raise SelfCodingError(switched.stderr.strip() or "Unable to switch to main.")
        merged = self._git("merge", "--ff-only", checkpoint.branch, timeout=300)
        if merged.returncode != 0:
            self._git("switch", previous)
            self._save(checkpoint)
            raise SelfCodingError(merged.stderr.strip() or "Unable to promote checkpoint.")
        promoted = self._git("rev-parse", "HEAD")
        if promoted.returncode != 0:
            self._git("reset", "--hard", checkpoint.baseline)
            self._git("switch", previous)
            self._save(checkpoint)
            raise SelfCodingError("Unable to record the promoted checkpoint SHA.")
        promoted_sha = promoted.stdout.strip()
        promoting = replace(checkpoint, state="promoting", promoted_sha=promoted_sha)
        try:
            self._save(promoting)
        except Exception as save_exc:
            reset = self._git("reset", "--hard", checkpoint.baseline)
            switched_back = self._git("switch", previous)
            try:
                self._save(checkpoint)
            except Exception:
                pass
            if reset.returncode != 0 or switched_back.returncode != 0:
                raise SelfCodingError(
                    "Promotion checkpoint persistence failed and local main could not be restored safely."
                ) from save_exc
            raise SelfCodingError(
                "Promotion checkpoint persistence failed; local main was restored to the baseline."
            ) from save_exc
        pushed = self._git("push", "origin", "main", timeout=300)
        if pushed.returncode != 0:
            current_head = self._git("rev-parse", "HEAD")
            status = self._git("status", "--porcelain")
            if current_head.returncode == 0 and current_head.stdout.strip() == promoted_sha and not status.stdout.strip():
                self._git("reset", "--hard", checkpoint.baseline)
            self._git("switch", previous)
            self._save(checkpoint)
            raise SelfCodingError(pushed.stderr.strip() or "Approval publish failed safely.")
        approved = replace(promoting, state="approved")
        try:
            self._save(approved)
        except Exception as save_exc:
            raise SelfCodingError(
                "Approval was published but checkpoint metadata could not be finalized; "
                "retry the same checkpoint approval to recover the persisted state."
            ) from save_exc
        return promoted_sha

    def undo(self, checkpoint_id: str) -> str:
        with _SELF_CODING_LOCK:
            return self._undo_unlocked(checkpoint_id)

    def _undo_unlocked(self, checkpoint_id: str) -> str:
        checkpoint = self._load(checkpoint_id)
        self._validate_checkpoint(checkpoint, checkpoint_id)
        if checkpoint.state == "undoing":
            return self._recover_undoing(checkpoint)
        self.validate_repo()
        if checkpoint.base_branch != "main":
            raise SelfCodingError("Only checkpoints created from main can be promoted or undone.")
        if checkpoint.state == "pending":
            current = self._branch()
            if current == checkpoint.branch:
                switched = self._git("switch", checkpoint.base_branch)
                if switched.returncode != 0:
                    raise SelfCodingError(
                        switched.stderr.strip() or "Unable to leave the checkpoint branch during undo."
                    )
            deleted = self._git("branch", "-D", checkpoint.branch)
            if deleted.returncode != 0:
                raise SelfCodingError(
                    deleted.stderr.strip() or "Unable to remove the pending checkpoint branch."
                )
            undone = replace(checkpoint, state="undone")
            self._save(undone)
            return "undone"
        if checkpoint.state != "approved" or not checkpoint.promoted_sha:
            raise SelfCodingError(f"Checkpoint cannot be undone from state {checkpoint.state}")
        fetched = self._git("fetch", "origin", "main", timeout=300)
        if fetched.returncode != 0:
            raise SelfCodingError(fetched.stderr.strip() or "Unable to fetch remote main.")
        remote = self._git("rev-parse", "refs/remotes/origin/main")
        local = self._git("rev-parse", "refs/heads/main")
        if remote.returncode != 0 or local.returncode != 0:
            raise SelfCodingError("Unable to inspect main before undo.")
        if remote.stdout.strip() != checkpoint.promoted_sha or local.stdout.strip() != checkpoint.promoted_sha:
            raise SelfCodingError("main changed after approval; refusing to undo unrelated work.")
        current = self._branch()
        switched = self._git("switch", "main")
        if switched.returncode != 0:
            raise SelfCodingError(switched.stderr.strip() or "Unable to switch to main for undo.")
        undo_commits: list[str] = []
        try:
            try:
                self._save(replace(checkpoint, state="undoing", undo_commits=()))
            except Exception as save_exc:
                self._git("switch", current)
                raise SelfCodingError("Unable to persist the undoing checkpoint before changing main.") from save_exc
            for commit in reversed(checkpoint.commits):
                reverted = self._git("revert", "--no-edit", commit, timeout=300)
                if reverted.returncode != 0:
                    self._git("revert", "--abort")
                    self._git("reset", "--hard", checkpoint.promoted_sha)
                    raise SelfCodingError(reverted.stderr.strip() or f"Unable to revert {commit}.")
                head = self._git("rev-parse", "HEAD")
                if head.returncode != 0:
                    self._git("reset", "--hard", checkpoint.promoted_sha)
                    raise SelfCodingError("Unable to record an undo commit.")
                undo_commits.append(head.stdout.strip())
                try:
                    self._save(replace(checkpoint, state="undoing", undo_commits=tuple(undo_commits)))
                except Exception as save_exc:
                    self._git("reset", "--hard", checkpoint.promoted_sha)
                    self._git("switch", current)
                    try:
                        self._save(checkpoint)
                    except Exception:
                        pass
                    raise SelfCodingError(
                        "Undo checkpoint persistence failed; local main was restored to the promoted state."
                    ) from save_exc
            status = self._git("status", "--porcelain")
            if status.returncode != 0 or status.stdout.strip():
                self._git("reset", "--hard", checkpoint.promoted_sha)
                raise SelfCodingError("Main became dirty during undo; refusing to publish ambiguous work.")
            pushed = self._git("push", "origin", "main", timeout=300)
            if pushed.returncode != 0:
                self._git("reset", "--hard", checkpoint.promoted_sha)
                self._git("switch", current)
                self._save(checkpoint)
                raise SelfCodingError(pushed.stderr.strip() or "Unable to publish checkpoint undo.")
            try:
                self._save(replace(checkpoint, state="undone", undo_commits=tuple(undo_commits)))
            except Exception as save_exc:
                raise SelfCodingError(
                    "Checkpoint undo was published but metadata could not be finalized; "
                    "retry the same undo to recover the durable state."
                ) from save_exc
            self._git("switch", current)
            return "undone"
        except Exception:
            try:
                if self._branch() == "main" and not undo_commits:
                    self._git("reset", "--hard", checkpoint.promoted_sha)
                self._git("switch", current)
            except Exception:
                pass
            raise
        except Exception:
            try:
                if self._branch() == "main":
                    self._git("reset", "--hard", checkpoint.promoted_sha)
                self._git("switch", current)
            except Exception:
                pass
            raise
