from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_messaging_does_not_claim_delivery_without_acknowledgement():
    source = (ROOT / "actions" / "send_message.py").read_text(encoding="utf-8")
    assert "desktop UI provided no delivery acknowledgement" in source
    assert "Message sent to {receiver} via WhatsApp." not in source
    assert "Message sent to {receiver} via Telegram." not in source
