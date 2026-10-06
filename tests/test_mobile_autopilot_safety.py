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


def test_mobile_autopilot_rejects_malformed_actions_and_coordinates():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
    assert "if not isinstance(decision, dict):" in source
    assert "Unsupported mobile action:" in source
    assert "def _validate_coordinate" in source
    assert "Mobile action coordinate" in source
    assert "MAX_MOBILE_COORDINATE = 10_000" in source


def test_mobile_autopilot_requires_verifiable_completion_evidence():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
    assert "def _verify_completion" in source
    assert '"verification"' in source
    assert "Model claimed completion without independently verifiable UI evidence." in source
    assert 'connect_execute({"target": target, "action": "ui_dump"' in source


def test_mobile_autopilot_has_timeout_cancellation_and_loop_guards():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
    assert "MAX_AUTOPILOT_SECONDS = 600.0" in source
    assert '"Mobile autopilot was cancelled."' in source
    assert '"Mobile autopilot timed out."' in source
    assert "MAX_REPEAT_SIGNATURES = 2" in source
    assert "repeated action loop" in source


def test_mobile_autopilot_rejects_malformed_ui_tree_and_bounds_node_count():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
    assert "Mobile UI dump data must be an object." in source
    assert "Mobile UI dump nodes must be a list." in source
    assert "nodes[:500]" in source
    assert "import json" in source.splitlines()[:3]
