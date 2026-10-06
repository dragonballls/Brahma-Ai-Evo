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
