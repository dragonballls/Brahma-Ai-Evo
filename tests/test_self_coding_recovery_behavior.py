from __future__ import annotations

import subprocess

from core.self_coding import Checkpoint, SelfCodingAgent, SelfCodingError


def _result(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _checkpoint(state="pending", promoted_sha=None, undo_commits=()):
    return Checkpoint(
        checkpoint_id="test-checkpoint",
        branch="agent/checkpoint/test-checkpoint",
        baseline="a" * 40,
        base_branch="main",
        commits=("b" * 40,),
        created_at="2026-10-07T00:00:00+00:00",
        state=state,
        promoted_sha=promoted_sha,
        undo_commits=tuple(undo_commits),
    )


def test_approve_push_failure_preserves_promoting_state_for_recovery():
    agent = object.__new__(SelfCodingAgent)
    checkpoint = _checkpoint()
    saved = []
    calls = []

    agent._load = lambda _checkpoint_id: checkpoint
    agent._validate_checkpoint = lambda _checkpoint: None
    agent.validate_repo = lambda: None
    agent._branch = lambda: checkpoint.branch
    agent._save = lambda value: saved.append(value)

    promoted = "c" * 40

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args == ("rev-parse", checkpoint.branch):
            return _result(args, stdout="b" * 40)
        if args == ("fetch", "origin", "main"):
            return _result(args)
        if args == ("rev-parse", "refs/remotes/origin/main"):
            return _result(args, stdout="a" * 40)
        if args == ("rev-parse", "refs/heads/main"):
            return _result(args, stdout="a" * 40)
        if args == ("switch", "main"):
            return _result(args)
        if args == ("merge", "--ff-only", checkpoint.branch):
            return _result(args)
        if args == ("rev-parse", "HEAD"):
            return _result(args, stdout=promoted)
        if args == ("push", "origin", "main"):
            return _result(args, returncode=1, stderr="connection lost after remote update")
        if args == ("switch", checkpoint.branch):
            return _result(args)
        raise AssertionError(f"unexpected git call: {args}")

    agent._git = fake_git

    try:
        agent._approve_unlocked(checkpoint.checkpoint_id)
    except SelfCodingError as exc:
        assert "ambiguous" in str(exc).casefold()
    else:
        raise AssertionError("ambiguous push outcome must not be reported as approval success")

    assert saved[-1].state == "promoting"
    assert saved[-1].promoted_sha == promoted
    assert ("reset", "--hard", checkpoint.baseline) not in calls
    assert saved[-1].commits == checkpoint.commits


def test_undo_push_failure_preserves_undoing_state_for_recovery():
    promoted = "c" * 40
    undo_tip = "d" * 40
    checkpoint = _checkpoint(state="approved", promoted_sha=promoted)
    saved = []
    calls = []

    agent = object.__new__(SelfCodingAgent)
    agent._load = lambda _checkpoint_id: checkpoint
    agent._validate_checkpoint = lambda _checkpoint: None
    agent.validate_repo = lambda: None
    agent._branch = lambda: "agent/checkpoint/original"
    agent._save = lambda value: saved.append(value)

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args == ("fetch", "origin", "main"):
            return _result(args)
        if args == ("rev-parse", "refs/remotes/origin/main"):
            return _result(args, stdout=promoted)
        if args == ("rev-parse", "refs/heads/main"):
            return _result(args, stdout=promoted)
        if args == ("switch", "main"):
            return _result(args)
        if args == ("revert", "--no-edit", checkpoint.commits[0]):
            return _result(args)
        if args == ("rev-parse", "HEAD"):
            return _result(args, stdout=undo_tip)
        if args == ("status", "--porcelain"):
            return _result(args, stdout="")
        if args == ("push", "origin", "main"):
            return _result(args, returncode=1, stderr="connection lost after remote update")
        if args == ("switch", "agent/checkpoint/original"):
            return _result(args)
        raise AssertionError(f"unexpected git call: {args}")

    agent._git = fake_git

    try:
        agent._undo_unlocked(checkpoint.checkpoint_id)
    except SelfCodingError as exc:
        assert "ambiguous" in str(exc).casefold()
    else:
        raise AssertionError("ambiguous undo push outcome must not be reported as success")

    assert saved[0].state == "undoing"
    assert saved[-1].state == "undoing"
    assert saved[-1].undo_commits == (undo_tip,)
    assert ("reset", "--hard", promoted) not in calls


def test_recover_promoting_resets_main_not_checkpoint_branch():
    promoted = "c" * 40
    checkpoint = _checkpoint(state="promoting", promoted_sha=promoted)
    saved = []
    calls = []

    agent = object.__new__(SelfCodingAgent)
    agent._load = lambda _checkpoint_id: checkpoint
    agent.validate_repo = lambda: None
    agent._branch = lambda: "agent/checkpoint/original"
    agent._save = lambda value: saved.append(value)

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args == ("fetch", "origin", "main"):
            return _result(args)
        if args == ("rev-parse", "refs/remotes/origin/main"):
            return _result(args, stdout=checkpoint.baseline)
        if args == ("rev-parse", "refs/heads/main"):
            return _result(args, stdout=promoted)
        if args == ("switch", "main"):
            return _result(args)
        if args == ("reset", "--hard", checkpoint.baseline):
            return _result(args)
        if args == ("switch", "agent/checkpoint/original"):
            return _result(args)
        if args == ("rev-parse", checkpoint.branch):
            return _result(args, stdout=checkpoint.commits[-1])
        raise AssertionError(f"unexpected git call: {args}")

    agent._git = fake_git

    # Re-enter approval after restoring the unpublished local main. The nested
    # approval call must see the checkpoint branch intact.
    agent._approve_unlocked = lambda checkpoint_id: (
        "reapproved"
        if checkpoint_id == checkpoint.checkpoint_id
        else (_ for _ in ()).throw(AssertionError("wrong checkpoint id"))
    )

    assert agent._recover_promoting(checkpoint) == "reapproved"
    assert ("switch", "main") in calls
    assert ("reset", "--hard", checkpoint.baseline) in calls
    assert calls.index(("switch", "main")) < calls.index(("reset", "--hard", checkpoint.baseline))
    assert calls.index(("reset", "--hard", checkpoint.baseline)) < calls.index(("switch", "agent/checkpoint/original"))
    assert ("switch", "agent/checkpoint/original") in calls
    assert saved[-1].state == "pending"
    assert saved[-1].promoted_sha is None
