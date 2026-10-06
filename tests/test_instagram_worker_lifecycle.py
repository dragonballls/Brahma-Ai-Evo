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