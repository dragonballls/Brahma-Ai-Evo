from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_send_message_rejects_unsuccessful_instagram_dm_results():
    source = (ROOT / "actions" / "send_message.py").read_text(encoding="utf-8")
    start = source.index("def _send_instagram")
    end = source.index("def _upload_instagram_media", start)
    block = source[start:end]
    assert "Instagram send failed:" in block
    assert "if not _is_success_result(res):" in block


def test_send_message_rejects_unsuccessful_instagram_media_results():
    source = (ROOT / "actions" / "send_message.py").read_text(encoding="utf-8")
    start = source.index("def _upload_instagram_media")
    end = source.index("def _send_telegram", start)
    block = source[start:end]
    assert "Instagram Reel publish failed:" in block
    assert "Instagram Photo publish failed:" in block
    assert "if not _is_success_result(res):" in block


def test_message_success_contract_rejects_explicit_false_even_with_success_status():
    from actions.send_message import _is_success_result

    assert _is_success_result({"status": "success", "success": True}) is True
    assert _is_success_result({"status": "success", "success": False}) is False
    assert _is_success_result({"status": "ok", "success": None}) is False
    assert _is_success_result({"status": "ok"}) is True


def test_email_compose_requires_browser_open_confirmation():
    source = (ROOT / "actions" / "send_message.py").read_text(encoding="utf-8")
    block = source[source.index("def _send_email_via_browser"):source.index("def _send_generic", source.index("def _send_email_via_browser"))]
    assert "if not webbrowser.open(url):" in block
    assert 'return f"Could not open {app_name} to compose email."' in block
