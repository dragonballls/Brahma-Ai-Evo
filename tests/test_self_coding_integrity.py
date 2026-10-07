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


def test_self_coding_list_checkpoints_does_not_skip_corruption(tmp_path):
    import json
    import pytest
    from core.self_coding import SelfCodingAgent

    agent = object.__new__(SelfCodingAgent)
    agent.repo = tmp_path
    checkpoint_dir = agent.checkpoint_dir
    checkpoint_dir.mkdir(parents=True)

    path = checkpoint_dir / "broken.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(Exception):
        agent.list_checkpoints()


def test_self_coding_rejects_symlinked_checkpoint_metadata(tmp_path):
    import os
    import pytest
    from core.self_coding import SelfCodingAgent

    agent = object.__new__(SelfCodingAgent)
    agent.repo = tmp_path
    checkpoint_dir = agent.checkpoint_dir
    checkpoint_dir.mkdir(parents=True)

    source = tmp_path / "outside.json"
    source.write_text("{}", encoding="utf-8")
    link = checkpoint_dir / "checkpoint.json"
    try:
        os.symlink(source, link)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable in this environment.")

    with pytest.raises(Exception, match="must not be a symlink"):
        agent._load("checkpoint")


def test_self_coding_checkpoint_save_uses_exclusive_file_creation(tmp_path):
    import os
    from unittest.mock import patch
    from core.self_coding import Checkpoint, SelfCodingAgent

    agent = object.__new__(SelfCodingAgent)
    agent.repo = tmp_path
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-1",
        branch="agent/checkpoint/checkpoint-1",
        baseline="a" * 40,
        base_branch="main",
        commits=("b" * 40,),
        created_at="now",
        state="pending",
    )
    seen = {}
    real_open = os.open

    def checked_open(path, flags, mode=0o666):
        seen["flags"] = flags
        return real_open(path, flags, mode)

    with patch("core.self_coding.os.open", side_effect=checked_open):
        agent._save(checkpoint)

    assert seen["flags"] & os.O_EXCL
    assert (agent.checkpoint_dir / "checkpoint-1.json").exists()


def test_self_coding_rejects_symlinked_checkpoint_directory(tmp_path):
    import os
    import pytest
    from core.self_coding import SelfCodingAgent

    agent = object.__new__(SelfCodingAgent)
    agent.repo = tmp_path
    target = tmp_path / "outside"
    target.mkdir()
    checkpoint_dir = agent.checkpoint_dir
    try:
        os.symlink(target, checkpoint_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are unavailable in this environment.")

    with pytest.raises(Exception, match="must not be a symlink"):
        agent.list_checkpoints()


def test_self_coding_link_like_guard_covers_junction_reparse_contract():
    source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
    assert "is_junction = getattr(path, "is_junction", None)" in source
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in source
    assert "Checkpoint directory must not be a symlink, junction, or reparse point." in source
