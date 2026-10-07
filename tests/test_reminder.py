import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from actions.reminder import reminder


def test_reminder_does_not_claim_success_without_scheduler_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    target = (datetime.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    calls = []

    class Result:
        def __init__(self, code, stderr="", stdout=""):
            self.returncode = code
            self.stderr = stderr
            self.stdout = stdout

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[1] == "/Create":
            return Result(0)
        if args[1] == "/Query":
            return Result(1, stderr="verification unavailable")
        if args[1] == "/Delete":
            return Result(0)
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = reminder({"date": target.strftime("%Y-%m-%d"), "time": target.strftime("%H:%M"), "message": "hello"})

    assert "couldn't verify" in result.lower()
    assert any(call[1] == "/Query" for call in calls)
    assert any(call[1] == "/Delete" for call in calls)


def test_reminder_reports_success_only_after_scheduler_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    target = (datetime.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    calls = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = "TaskName: verified"

    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(list(args)) or Result())
    result = reminder({"date": target.strftime("%Y-%m-%d"), "time": target.strftime("%H:%M"), "message": "hello"})

    assert result.startswith("Reminder set for")
    assert any(call[1] == "/Create" for call in calls)
    assert any(call[1] == "/Query" for call in calls)
