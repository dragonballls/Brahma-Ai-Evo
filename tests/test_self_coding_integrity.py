from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_self_coding_validates_checkpoint_branch_and_commit_chain():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    assert "def _validate_checkpoint" in source
    assert 're.fullmatch(r"agent/checkpoint/[A-Za-z0-9._-]+", checkpoint.branch)' in source
    assert "checkpoint.branch.rsplit(\"/\", 1)[-1] != checkpoint.checkpoint_id" in source
    assert "Checkpoint commits are not the expected linear chain." in source
    assert "Checkpoint promoted SHA does not match the checkpoint tip." in source


def test_self_coding_quarantines_corrupt_checkpoint_metadata():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    assert "def _quarantine_checkpoint" in source
    assert "Checkpoint metadata is corrupt and was quarantined." in source


def test_self_coding_promotion_is_recoverable_after_metadata_write_failure():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    assert 'state="promoting"' in source
    assert "def _recover_promoting" in source
    assert "remote_sha == checkpoint.promoted_sha and local_sha == checkpoint.promoted_sha" in source
    assert "retry the same checkpoint approval" in source


def test_self_coding_approve_has_one_remote_push_and_persists_promoting_before_it():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    start = source.index("def _approve_unlocked")
    end = source.index("def undo", start)
    block = source[start:end]
    assert block.count('self._git("push", "origin", "main"') == 1
    assert "self._save(promoting)" in block
    assert block.index("self._save(promoting)") < block.index('self._git("push", "origin", "main"')

def test_self_coding_rejects_checkpoint_filename_id_mismatch():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    assert "expected_checkpoint_id" in source
    assert "checkpoint.checkpoint_id != expected_checkpoint_id" in source


def test_self_coding_checkpoint_validation_accepts_matching_expected_id():
    from core.self_coding import Checkpoint, SelfCodingAgent

    agent = object.__new__(SelfCodingAgent)
    baseline = "a" * 40
    commit = "b" * 40

    class Result:
        returncode = 0
        stdout = f"{commit} {baseline}\n"
        stderr = ""

    agent._git = lambda *args, **kwargs: Result()

    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-1",
        branch="agent/checkpoint/checkpoint-1",
        baseline=baseline,
        base_branch="main",
        commits=(commit,),
        created_at="now",
        state="pending",
    )
    agent._validate_checkpoint(checkpoint, "checkpoint-1")


def test_self_coding_undo_uses_recoverable_undoing_state():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    assert '"undoing"' in source
    assert "def _recover_undoing" in source
    assert "main state is ambiguous" in source
    assert "Undo checkpoint persistence failed" in source
    assert "Checkpoint undo commits are not the expected linear chain." in source
