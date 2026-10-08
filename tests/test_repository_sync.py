from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RepositorySyncTests(unittest.TestCase):
    def test_repository_sync_is_opt_out(self):
        source = (ROOT / "core" / "repository_sync.py").read_text(encoding="utf-8")
        self.assertIn("BRAHMA_AUTO_PUBLISH_REPAIRS", source)
        self.assertIn('{"0", "false", "no", "off"}', source)

    def test_repository_sync_uses_canonical_repository(self):
        source = (ROOT / "core" / "repository_sync.py").read_text(encoding="utf-8")
        self.assertIn("GITHUB_REMOTE", source)
        self.assertIn('refs/remotes/origin/main', source)
        self.assertIn('push", "origin", "main"', source)

    def test_repository_sync_refuses_remote_divergence(self):
        from core import repository_sync
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            target = repo / "main.py"
            target.write_text("fixed", encoding="utf-8")
            with patch.object(repository_sync, "resolve_repository", return_value=repo),                  patch.object(repository_sync, "_run") as run:
                def fake_run(_repo, args, timeout=120):
                    class R:
                        returncode = 0
                        stdout = ""
                        stderr = ""
                    r = R()
                    args = tuple(args)
                    if args[:3] == ("remote", "get-url", "origin"):
                        r.stdout = repository_sync._canonical_remote(repo)
                    elif args[:2] == ("branch", "--show-current"):
                        r.stdout = "main"
                    elif args[:2] == ("rev-parse", "HEAD"):
                        r.stdout = "local"
                    elif args[:2] == ("rev-parse", "refs/remotes/origin/main"):
                        r.stdout = "remote"
                    return r
                run.side_effect = fake_run
                with patch.dict(os.environ, {"BRAHMA_AUTO_PUBLISH_REPAIRS": "1"}):
                    result = repository_sync.publish_verified_repair(target, "abc123")
            self.assertEqual(result["reason"], "remote_main_changed")

    def test_auto_heal_calls_repository_sync_after_verification(self):
        source = (ROOT / "actions" / "auto_heal_engine.py").read_text(encoding="utf-8")
        self.assertIn("publish_verified_repair(", source)
        self.assertIn("repository_relative_path", source)
        self.assertIn('"repository_publication": publication', source)

    def test_self_coding_still_requires_explicit_approval(self):
        source = (ROOT / "core" / "self_coding.py").read_text(encoding="utf-8")
        self.assertIn('state="pending"', source)
        self.assertIn('def approve(', source)
        self.assertIn('push", "origin", "main"', source)


if __name__ == "__main__":
    unittest.main()


def test_repository_sync_rejects_target_changes_after_verification():
    source = (ROOT / "core" / "repository_sync.py").read_text(encoding="utf-8")
    assert "expected_postimage_sha256" in source
    assert "target_changed_since_verification" in source


def test_repository_sync_rejects_changes_to_the_verified_preimage():
    source = (ROOT / "core" / "repository_sync.py").read_text(encoding="utf-8")
    assert "expected_preimage_sha256" in source
    assert "target_preimage_changed" in source
    assert '"show", f"HEAD:{' in source


def test_repository_sync_resolves_git_worktree_dotgit_file(tmp_path, monkeypatch):
    from core import repository_sync

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    monkeypatch.setenv("BRAHMA_REPOSITORY_PATH", str(worktree))
    monkeypatch.delenv("BRAHMA_SELF_CODING_REPO", raising=False)
    assert repository_sync.resolve_repository() == worktree.resolve()
