import importlib
import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_autopilot_actually_dispatches_supported_actions():
    module = importlib.import_module("actions.mobile_autopilot")
    remote_results = [
        {
            "success": True,
            "data": {
                "screen_width": 1080,
                "screen_height": 2400,
                "nodes": [
                    {
                        "bounds": [0, 0, 100, 100],
                        "is_clickable": True,
                        "text": "Search",
                    }
                ]
            },
        },
        {"success": True, "data": {"clicked": True}},
        {
            "success": True,
            "data": {
                "screen_width": 1080,
                "screen_height": 2400,
                "nodes": [
                    {
                        "bounds": [0, 0, 100, 100],
                        "text": "Complete",
                    }
                ]
            },
        },
    ]
    decisions = [
        {"action": "tap", "x": 50, "y": 50, "reason": "Tap search"},
        {"action": "done", "verification": ["Complete"], "reason": "Completion is visible"},
    ]

    with patch.object(module, "connect_execute", side_effect=remote_results) as execute,          patch("core.gemini_runtime.generate_json", side_effect=decisions),          patch.object(module.time, "sleep", return_value=None):
        result = json.loads(
            module.mobile_autopilot(
                {"target": "phone-1", "instruction": "finish the task", "timeout_seconds": 5}
            )
        )

    assert result["success"] is True
    assert execute.call_count == 3
    assert execute.call_args_list[1].kwargs == {}
    dispatched = execute.call_args_list[1].args[0]
    assert dispatched["target"] == "phone-1"
    assert dispatched["action"] == "ui_tap"
    assert dispatched["parameters"] == {"x": 50, "y": 50}


def test_mobile_autopilot_checks_every_remote_action_result_and_fails_on_exhaustion():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
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


def test_mobile_autopilot_requires_actual_device_dimensions_and_visible_taps():
    source = (ROOT / "actions" / "mobile_autopilot.py").read_text(encoding="utf-8")
    assert "Mobile UI dump is missing valid device screen dimensions." in source
    assert "Mobile tap coordinates are outside the actual device screen bounds." in source
    assert "Mobile tap coordinates do not fall within a visible UI element." in source
    assert "Current UI elements on screen (" in source
    assert "len(instruction) > 4_000" in source
    assert "len(reason) > 1_000" in source


def test_mobile_autopilot_dimension_validation_is_exercised_at_runtime():
    import pytest
    from actions import mobile_autopilot

    with pytest.raises(ValueError, match="missing valid device screen dimensions"):
        mobile_autopilot._screen_dimensions({"nodes": []})

    assert mobile_autopilot._screen_dimensions({
        "screen_width": 1080,
        "screen_height": 2400,
        "nodes": [],
    }) == (1080, 2400)


def test_mobile_autopilot_never_reports_success_after_cancellation_is_observed_post_dispatch():
    import importlib
    import json
    from unittest.mock import patch

    module = importlib.import_module("actions.mobile_autopilot")
    parameters = {
        "target": "phone-1",
        "instruction": "tap search",
        "timeout_seconds": 5,
    }

    def execute(command):
        if command["action"] == "ui_dump":
            return {
                "success": True,
                "data": {
                    "screen_width": 1080,
                    "screen_height": 2400,
                    "nodes": [{
                        "bounds": [0, 0, 100, 100],
                        "is_clickable": True,
                        "text": "Search",
                    }],
                },
            }
        parameters["cancelled"] = True
        return {"success": True, "data": {"clicked": True}}

    with patch.object(module, "connect_execute", side_effect=execute),          patch("core.gemini_runtime.generate_json", return_value={
             "action": "tap", "x": 50, "y": 50, "reason": "Tap search"
         }),          patch.object(module.time, "sleep", return_value=None):
        result = json.loads(module.mobile_autopilot(parameters))

    assert result["success"] is False
    assert "after remote action dispatch" in result["error"]
