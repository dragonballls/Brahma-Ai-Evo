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
    agent._validate_checkpoint = lambda _checkpoint, _checkpoint_id=None: None
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


def test_recover_undoing_push_failure_is_explicitly_ambiguous():
    promoted = "c" * 40
    undo_tip = "d" * 40
    checkpoint = _checkpoint(state="undoing", promoted_sha=promoted, undo_commits=(undo_tip,))
    agent = object.__new__(SelfCodingAgent)
    agent._load = lambda _checkpoint_id: checkpoint
    agent._validate_checkpoint = lambda _checkpoint, _checkpoint_id=None: None
    agent.validate_repo = lambda: None
    agent._save = lambda _value: None

    def fake_git(*args, **kwargs):
        if args == ("fetch", "origin", "main"):
            return _result(args)
        if args == ("rev-parse", "refs/remotes/origin/main"):
            return _result(args, stdout=promoted)
        if args == ("rev-parse", "refs/heads/main"):
            return _result(args, stdout=undo_tip)
        if args == ("push", "origin", "main"):
            return _result(args, returncode=1, stderr="connection lost after remote update")
        raise AssertionError(f"unexpected git call: {args}")

    agent._git = fake_git
    try:
        agent._recover_undoing(checkpoint)
    except SelfCodingError as exc:
        assert "ambiguous" in str(exc).casefold()
        assert "connection lost after remote update" in str(exc)
    else:
        raise AssertionError("recovery must not report an ambiguous push as success")


def test_undo_push_failure_preserves_undoing_state_for_recovery():
    promoted = "c" * 40
    undo_tip = "d" * 40
    checkpoint = _checkpoint(state="approved", promoted_sha=promoted)
    saved = []
    calls = []

    agent = object.__new__(SelfCodingAgent)
    agent._load = lambda _checkpoint_id: checkpoint
    agent._validate_checkpoint = lambda _checkpoint, _checkpoint_id=None: None
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


def test_partial_undo_failure_restores_approved_state(tmp_path):
    promoted = "c" * 40
    checkpoint = _checkpoint(state="approved", promoted_sha=promoted)
    saved = []
    calls = []
    agent = object.__new__(SelfCodingAgent)
    agent._load = lambda _checkpoint_id: checkpoint
    agent._validate_checkpoint = lambda _checkpoint, _checkpoint_id=None: None
    agent.validate_repo = lambda: None
    agent._branch = lambda: "main"
    agent._save = lambda value: saved.append(value)

    first_undo = "d" * 40
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
        if args == ("revert", "--no-edit", checkpoint.commits[-1]):
            return _result(args)
        if args == ("status", "--porcelain"):
            return _result(args, stdout="")
        if args == ("rev-parse", "HEAD"):
            return _result(args, stdout=first_undo)
        if args == ("reset", "--hard", promoted):
            return _result(args)
        if args == ("revert", "--no-edit", checkpoint.commits[0]):
            return _result(args, returncode=1, stderr="conflict during second revert")
        if args == ("revert", "--abort"):
            return _result(args)
        if args == ("switch", "main"):
            return _result(args)
        raise AssertionError(f"unexpected git call: {args}")

    agent._git = fake_git

    try:
        agent._undo_unlocked(checkpoint.checkpoint_id)
    except SelfCodingError as exc:
        assert "second revert" in str(exc) or "Unable to revert" in str(exc)
    else:
        raise AssertionError("partial undo failure must not be reported as success")

    assert ("reset", "--hard", promoted) in calls
    assert saved[-1].state == "approved"
    assert saved[-1].undo_commits == ()


def test_rollback_refuses_wrong_current_branch_before_reset():
    agent = object.__new__(SelfCodingAgent)
    calls = []
    agent._branch = lambda: "main"

    def fake_git(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"destructive rollback command must not run: {args}")

    agent._git = fake_git

    try:
        agent._rollback("a" * 40, "agent/checkpoint/test-checkpoint", "main")
    except SelfCodingError as exc:
        assert "not the checkpoint branch" in str(exc)
        assert "destructive reset" in str(exc)
    else:
        raise AssertionError("rollback must refuse when current branch is not the checkpoint branch")
    assert calls == []


def test_rollback_refuses_dirty_checkpoint_branch_before_reset():
    agent = object.__new__(SelfCodingAgent)
    calls = []
    agent._branch = lambda: "agent/checkpoint/test-checkpoint"

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args == ("status", "--porcelain"):
            return _result(args, stdout=" M unrelated.txt\n")
        raise AssertionError(f"unexpected destructive or post-check command: {args}")

    agent._git = fake_git

    try:
        agent._rollback("a" * 40, "agent/checkpoint/test-checkpoint", "main")
    except SelfCodingError as exc:
        assert "uncommitted changes" in str(exc)
        assert "destructive reset" in str(exc)
    else:
        raise AssertionError("rollback must refuse a dirty checkpoint branch")
    assert ("reset", "--hard", "a" * 40) not in calls


def test_rollback_clean_checkpoint_resets_then_switches_and_deletes_branch():
    agent = object.__new__(SelfCodingAgent)
    calls = []
    agent._branch = lambda: "agent/checkpoint/test-checkpoint"

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args == ("status", "--porcelain"):
            return _result(args, stdout="")
        if args == ("reset", "--hard", "a" * 40):
            return _result(args)
        if args == ("switch", "main"):
            return _result(args)
        if args == ("branch", "-D", "agent/checkpoint/test-checkpoint"):
            return _result(args)
        raise AssertionError(f"unexpected git call: {args}")

    agent._git = fake_git
    agent._rollback("a" * 40, "agent/checkpoint/test-checkpoint", "main")
    assert calls == [
        ("status", "--porcelain"),
        ("reset", "--hard", "a" * 40),
        ("switch", "main"),
        ("branch", "-D", "agent/checkpoint/test-checkpoint"),
    ]
