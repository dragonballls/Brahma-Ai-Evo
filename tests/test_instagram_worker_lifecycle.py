from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_instagram_worker_rejects_new_work_after_stop_and_resolves_queued_futures():
    source = (ROOT / "actions" / "instagram_mcp.py").read_text(encoding="utf-8")
    start = source.index("def stop(self) -> None:")
    end = source.index("def is_browser_logged_in", start)
    stop_block = source[start:end]
    assert "self._stop_event.set()" in stop_block
    assert "self.q.get_nowait()" in stop_block
    assert "fut.set_exception(rejection)" in stop_block
    exec_start = source.index("def execute(self, fn", start)
    exec_end = source.index("def is_browser_logged_in", exec_start)
    exec_block = source[exec_start:exec_end]
    assert "if self._stop_event.is_set() or not self.running" in exec_block
    assert "leaving callers waiting" in exec_block

def test_instagram_browser_dm_is_not_reported_as_verified_success():
    from actions.instagram_mcp import BrowserInstagramWorker

    worker = BrowserInstagramWorker.__new__(BrowserInstagramWorker)
    class FakeKeyboard:
        def type(self, *_args, **_kwargs): pass
        def press(self, *_args, **_kwargs): pass
    class FakePage:
        url = "https://www.instagram.com/direct/t/123456789012345/"
        keyboard = FakeKeyboard()
        def wait_for_selector(self, *_args, **_kwargs): return object()
        def wait_for_timeout(self, *_args, **_kwargs): pass
        def query_selector_all(self, *_args, **_kwargs): return []
        def goto(self, *_args, **_kwargs): pass
    class Ctx:
        pages=[FakePage()]
        def cookies(self, *_args): return [{"name":"sessionid","value":"x"},{"name":"ds_user_id","value":"1"}]

    result = worker.send_dm_action(Ctx(), "123456789012345", "hello", open_in_browser=False)
    assert result["success"] is False
    assert result["status"] == "submitted"
    assert result["delivery_verified"] is False


def test_instagram_dm_rejects_oversized_message():
    from actions.instagram_mcp import _validate_dm_inputs
    try:
        _validate_dm_inputs("user", "x" * (8192 + 1))
    except ValueError as exc:
        assert "8 KiB" in str(exc)
    else:
        raise AssertionError("oversized Instagram DMs must be rejected")


def test_instagram_session_path_rejects_symlink(tmp_path):
    from actions import instagram_mcp as ig
    real = tmp_path / "real-session.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "ig_session.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        return
    try:
        ig._validate_private_storage_path(link)
    except RuntimeError as exc:
        assert "symlink" in str(exc).lower()
    else:
        raise AssertionError("Instagram session symlink must be rejected")


def test_instagram_storage_rejects_symlinked_profile_parent(tmp_path):
    from actions import instagram_mcp as ig
    real = tmp_path / "real-profile-root"
    real.mkdir()
    link = tmp_path / "profile-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    try:
        ig._validate_private_storage_path(link / "ig_browser_profile", directory=True)
    except RuntimeError as exc:
        assert "parent" in str(exc).lower()
    else:
        raise AssertionError("Instagram browser profile under a symlinked parent must be rejected")
