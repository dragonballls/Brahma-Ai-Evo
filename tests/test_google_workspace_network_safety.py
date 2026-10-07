from __future__ import annotations

from types import SimpleNamespace

import actions.google_workspace_mcp as gw


def test_gmail_imap_uses_bounded_network_timeout(monkeypatch):
    calls = []

    class FakeMail:
        def login(self, *_args):
            return "OK", [b""]

        def select(self, *_args, **_kwargs):
            return "OK", [b"0"]

        def logout(self):
            return "BYE", []

    def fake_imap(host, port, timeout=None, *args, **kwargs):
        calls.append((host, port, timeout))
        return FakeMail()

    monkeypatch.setattr(gw.imaplib, "IMAP4_SSL", fake_imap)
    monkeypatch.setattr(gw, "get_stored_gmail_credentials", lambda: ("user@example.com", "pw"))

    result = gw.GmailEngine.test_connection()

    assert result["success"] is True
    assert calls == [(gw.GmailEngine.IMAP_HOST, gw.GmailEngine.IMAP_PORT, gw.GmailEngine.IMAP_TIMEOUT_SECONDS)]


def test_gmail_read_rejects_oversized_message_before_full_fetch(monkeypatch):
    fetches = []

    class FakeMail:
        def login(self, *_args):
            return "OK", [b""]

        def select(self, *_args, **_kwargs):
            return "OK", [b"0"]

        def fetch(self, msg_id, query):
            fetches.append((msg_id, query))
            if query == "(RFC822.SIZE)":
                return "OK", [b"1 (RFC822.SIZE 5000000)"]
            raise AssertionError("full message fetch must not occur")

        def logout(self):
            return "BYE", []

    monkeypatch.setattr(gw.imaplib, "IMAP4_SSL", lambda *args, **kwargs: FakeMail())
    monkeypatch.setattr(gw, "get_stored_gmail_credentials", lambda: ("user@example.com", "pw"))

    result = gw.GmailEngine.read_message("42")

    assert "exceeds" in result
    assert fetches == [(b"42", "(RFC822.SIZE)")]


def test_gmail_send_reports_partial_smtp_refusal(monkeypatch):
    class FakeSMTP:
        def __init__(self, host, port, timeout=None, *args, **kwargs):
            assert host == gw.GmailEngine.SMTP_HOST
            assert port == gw.GmailEngine.SMTP_PORT
            assert timeout == gw.GmailEngine.SMTP_TIMEOUT_SECONDS

        def starttls(self):
            return None

        def login(self, *_args):
            return None

        def sendmail(self, *_args):
            return {"blocked@example.com": (550, b"Mailbox unavailable")}

        def quit(self):
            return None

    monkeypatch.setattr(gw.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(gw, "get_stored_gmail_credentials", lambda: ("sender@example.com", "pw"))

    result = gw.GmailEngine.send_message("blocked@example.com", "Subject", "Body")

    assert result.startswith("Failed to send email:")
    assert "refused" in result
