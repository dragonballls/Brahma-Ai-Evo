from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_updater_does_not_treat_diverged_or_local_ahead_history_as_an_available_update():
    source = (ROOT / "core" / "updater.py").read_text(encoding="utf-8")
    assert "def _remote_is_safe_update" in source
    assert '["git", "merge-base", "--is-ancestor", local, remote_hash]' in source
    assert "return ancestry.returncode == 0 and local != remote_hash" in source
    assert "self._remote_is_safe_update(remote_hash)" in source


def test_updater_detection_fetches_and_verifies_the_actual_origin_tip():
    source = (ROOT / "core" / "updater.py").read_text(encoding="utf-8")
    start = source.index("def _remote_is_safe_update")
    end = source.index("def check_now", start)
    block = source[start:end]
    assert '["git", "fetch", "origin", self.branch, "--quiet"]' in block
    assert '["git", "rev-parse", f"origin/{self.branch}"]' in block


def test_updater_remote_hash_uses_pinned_transport_runtime(monkeypatch):
    from core.updater import UpdateChecker

    checker = UpdateChecker.__new__(UpdateChecker)
    checker.repo_owner = "dragonballls"
    checker.repo_name = "Brahma-Ai-Evo"
    checker.branch = "main"
    calls = {}

    def fake_fetch(url, *, timeout, max_response_bytes, headers):
        calls.update(url=url, timeout=timeout, max_response_bytes=max_response_bytes, headers=headers)
        return 200, b'{"sha":"abc123"}'

    monkeypatch.setattr("core.updater.fetch_public_bytes", fake_fetch)
    assert checker._get_remote_hash() == "abc123"
    assert calls["url"].startswith("https://api.github.com/repos/dragonballls/Brahma-Ai-Evo/commits/main")
    assert calls["max_response_bytes"] == 64 * 1024
    assert calls["headers"]["Accept"] == "application/vnd.github+json"


def test_updater_remote_hash_fails_closed_on_redirect_runtime(monkeypatch):
    from core.updater import UpdateChecker

    checker = UpdateChecker.__new__(UpdateChecker)
    checker.repo_owner = "dragonballls"
    checker.repo_name = "Brahma-Ai-Evo"
    checker.branch = "main"
    monkeypatch.setattr(
        "core.updater.fetch_public_bytes",
        lambda *args, **kwargs: (302, b"redirect"),
    )
    assert checker._get_remote_hash() is None
