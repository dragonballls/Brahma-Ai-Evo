import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_autopilot_checks_every_remote_action_result_and_fails_on_exhaustion():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
    assert "elif action in {\"tap\", \"swipe\", \"type\"}:" in source
    assert "parsed_command.get(\"success\") is not True" in source
    assert '"success": False' in source
    assert "Mobile autopilot stopped before the goal was confirmed complete." in source
    assert 'return json.dumps({"success": True, "message": "Max steps reached or stopped."})' not in source
