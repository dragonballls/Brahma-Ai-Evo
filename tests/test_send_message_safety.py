from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_send_message_rejects_unsuccessful_instagram_dm_results():
    source = (ROOT / "actions" / "send_message.py").read_text(encoding="utf-8")
    start = source.index("def _send_instagram")
    end = source.index("def _upload_instagram_media", start)
    block = source[start:end]
    assert "Instagram send failed:" in block
    assert 'res.get("status") not in {"success", "ok"}' in block
    assert 'res.get("success") is not True' in block


def test_send_message_rejects_unsuccessful_instagram_media_results():
    source = (ROOT / "actions" / "send_message.py").read_text(encoding="utf-8")
    start = source.index("def _upload_instagram_media")
    end = source.index("def _send_telegram", start)
    block = source[start:end]
    assert "Instagram Reel publish failed:" in block
    assert "Instagram Photo publish failed:" in block
    assert 'res.get("status") not in {"success", "ok"}' in block
