import os

import pytest

from core.command_safety import CommandSafetyError, resolve_git_executable


def test_git_resolution_rejects_repository_shadow(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_git = repo / "git.exe"
    fake_git.write_text("not git", encoding="utf-8")

    monkeypatch.setattr(
        "core.command_safety.shutil.which",
        lambda *_args, **_kwargs: str(fake_git),
    )

    with pytest.raises(CommandSafetyError, match="repository or current directory"):
        resolve_git_executable(repo)


def test_git_resolution_rejects_current_directory_shadow(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    fake_git = workdir / "git.exe"
    fake_git.write_text("not git", encoding="utf-8")

    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        "core.command_safety.shutil.which",
        lambda *_args, **_kwargs: str(fake_git),
    )

    with pytest.raises(CommandSafetyError, match="repository or current directory"):
        resolve_git_executable(repo)


def test_self_coding_git_path_uses_trusted_resolution(tmp_path, monkeypatch):
    from core.self_coding import SelfCodingAgent, SelfCodingError

    repo = tmp_path / "repo"
    repo.mkdir()
    fake_git = repo / "git.exe"
    fake_git.write_text("not git", encoding="utf-8")

    monkeypatch.setattr(
        "core.command_safety.shutil.which",
        lambda *_args, **_kwargs: str(fake_git),
    )

    agent = object.__new__(SelfCodingAgent)
    agent.repo = repo

    with pytest.raises(SelfCodingError, match="repository or current directory"):
        agent._git("status")


def test_repository_sync_git_path_uses_trusted_resolution(tmp_path, monkeypatch):
    from core import repository_sync

    fake_git = tmp_path / "git.exe"
    fake_git.write_text("not git", encoding="utf-8")

    monkeypatch.setattr(
        repository_sync,
        "resolve_git_executable",
        lambda _repo: (_ for _ in ()).throw(
            CommandSafetyError("Git executable may not come from the repository or current directory.")
        ),
    )

    with pytest.raises(CommandSafetyError, match="repository or current directory"):
        repository_sync._run(tmp_path, ("status",))


def test_windows_startfile_reports_submission_not_completion(tmp_path, monkeypatch):
    import actions.cmd_control as cmd

    if cmd.os.name != "nt":
        pytest.skip("Windows-specific semantic contract")
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(cmd.os, "startfile", lambda _path: None)

    result = cmd._open_target(f"open {target}")
    assert result == f"Open request submitted for {target}."


def test_update_checker_uses_trusted_git_executable(monkeypatch, tmp_path):
    import subprocess
    from core import updater

    checker = object.__new__(updater.UpdateChecker)
    checker.base_dir = tmp_path

    calls = []

    monkeypatch.setattr(
        updater,
        "resolve_git_executable",
        lambda repo: (calls.append(repo) or str(tmp_path / "trusted-git.exe")),
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **kwargs: b"abc123\n",
    )

    assert checker._get_local_hash() == "abc123"
    assert calls == [tmp_path]
