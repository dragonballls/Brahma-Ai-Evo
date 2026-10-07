import json
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def test_weather_action_never_describes_unavailable_fallback_as_current_weather():
    from actions import weather_report

    with patch.object(weather_report, "get_live_weather", return_value={
        "status": "unavailable",
        "city": "Testville",
        "temp_c": 26,
        "condition": "Clear",
        "summary": "Weather telemetry temporarily offline.",
    }):
        result = weather_report.weather_action({"city": "Testville"})

    assert "currently 26 degrees Celsius" not in result
    assert "currently unavailable" in result


def test_weather_action_reports_failed_browser_launch_instead_of_swallowing_it():
    from actions import weather_report

    with patch.object(weather_report, "get_live_weather", return_value={
        "status": "success",
        "city": "Testville",
        "temp_c": 20,
        "condition": "Clear",
    }), patch.object(weather_report.webbrowser, "open", return_value=False):
        result = weather_report.weather_action(
            {"city": "Testville", "open_browser": True}
        )

    assert "currently 20 degrees Celsius" in result
    assert "couldn't open the browser automatically" in result


def test_upload_video_rejects_missing_source_before_claiming_preparation_success():
    from actions import upload_video

    with patch.object(upload_video, "_find_candidate_video", return_value=None),          patch.object(upload_video, "webbrowser") as browser:
        result = upload_video.run({"platform": "youtube", "description": "demo"})

    assert result.startswith("Video upload preparation failed:")
    browser.open.assert_not_called()


def test_upload_video_rejects_browser_launch_failure():
    from actions import upload_video
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        video = Path(tmp) / "demo.mp4"
        video.write_bytes(b"video")

        with patch.object(upload_video, "_find_candidate_video", return_value=video),              patch.object(upload_video, "_generate_video_copy", return_value={
                 "hook_title": "Demo",
                 "caption": "Demo",
                 "hashtags": [],
                 "formatted_post": "Demo",
             }),              patch.object(upload_video.webbrowser, "open", return_value=False):
            result = upload_video.run({
                "platform": "youtube",
                "description": "demo",
                "video_path": str(video),
            })

    assert "No upload was performed" in result


def test_upload_video_success_language_does_not_claim_an_automatic_upload():
    from actions import upload_video
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        video = Path(tmp) / "demo.mp4"
        video.write_bytes(b"video")

        with patch.object(upload_video, "_find_candidate_video", return_value=video),              patch.object(upload_video, "_generate_video_copy", return_value={
                 "hook_title": "Demo",
                 "caption": "Demo",
                 "hashtags": [],
                 "formatted_post": "Demo",
             }),              patch.object(upload_video.webbrowser, "open", return_value=True):
            result = upload_video.run({
                "platform": "youtube",
                "description": "demo",
                "video_path": str(video),
            })

    assert "has not been uploaded automatically" in result


def test_upload_video_does_not_claim_browser_open_is_verified():
    from actions import upload_video
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        video = Path(tmp) / "demo.mp4"
        video.write_bytes(b"video")

        with patch.object(upload_video, "_find_candidate_video", return_value=video), \
             patch.object(upload_video, "_generate_video_copy", return_value={
                 "hook_title": "Demo",
                 "caption": "Demo",
                 "hashtags": [],
                 "formatted_post": "Demo",
             }), \
             patch.object(upload_video.webbrowser, "open", return_value=True):
            result = upload_video.run({
                "platform": "youtube",
                "description": "demo",
                "video_path": str(video),
            })

    assert "page load was not independently verified" in result
    assert "creator studio is open" not in result.lower()


def _load_email_flow_for_test():
    import ast

    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_email_flow"
    )
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["_handle_email_flow"]


def _make_email_flow_test_instance():
    method = _load_email_flow_for_test()

    class UI:
        def __init__(self):
            self.finished = []

        def write_log(self, _message):
            pass

        def update_task_workspace(self, **_kwargs):
            pass

        def finish_task_workspace(self, *args):
            self.finished.append(args)

    instance = type("EmailFlowHarness", (), {})()
    instance.ui = UI()
    instance.speak = lambda _message: None
    instance._email_step = 2
    instance._email_mode = True
    instance._email_profiles = {}
    instance._email_recipient = "test@example.com"
    instance._email_app = "Gmail"
    return method, instance


def test_email_flow_requires_browser_launch_proof(monkeypatch):
    import webbrowser

    method, instance = _make_email_flow_test_instance()
    monkeypatch.setattr(webbrowser, "open", lambda _url: False)

    assert method(instance, "hello") is True
    assert instance.ui.finished
    assert instance.ui.finished[-1][1:] == ("Failed", 0)
    assert "No email was sent or independently verified." in instance.ui.finished[-1][0]


def test_email_flow_success_is_launch_request_not_send_proof(monkeypatch):
    import webbrowser

    method, instance = _make_email_flow_test_instance()
    monkeypatch.setattr(webbrowser, "open", lambda _url: True)

    assert method(instance, "hello") is True
    assert instance.ui.finished
    assert instance.ui.finished[-1][1:] == ("Compose launch requested", 90)
    assert "not independently verified" in instance.ui.finished[-1][0]


def _load_ig_reply_flow_for_test():
    import ast

    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_ig_reply_flow"
    )
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["_handle_ig_reply_flow"]


def _make_ig_reply_flow_test_instance():
    method = _load_ig_reply_flow_for_test()

    class UI:
        def __init__(self):
            self.finished = []

        def write_log(self, _message):
            pass

        def finish_task_workspace(self, *args):
            self.finished.append(args)

    instance = type("IGReplyFlowHarness", (), {})()
    instance.ui = UI()
    instance.spoken = []
    instance.speak = instance.spoken.append
    instance._ig_pending_thread = {
        "thread_id": "12345678901234",
        "username": "test_user",
        "message": "incoming",
    }
    instance._ig_reply_mode = True
    instance._parse_ig_reply_intent = lambda _text: ("MANUAL_REPLY", "hello")
    return method, instance


def test_instagram_manual_reply_does_not_claim_send_before_result(monkeypatch):
    import actions.instagram_mcp as instagram_mcp
    import threading

    method, instance = _make_ig_reply_flow_test_instance()
    finished = threading.Event()

    def fake_send(_thread_id, _payload):
        try:
            return {
                "success": False,
                "status": "unknown",
                "submitted": False,
                "delivery_verified": False,
                "error": "send outcome was ambiguous",
            }
        finally:
            finished.set()

    monkeypatch.setattr(instagram_mcp, "send_direct_reply", fake_send)
    assert method(instance, "send it") is True
    assert finished.wait(2)
    assert "Message sent." not in instance.spoken
    assert instance.ui.finished[-1][1:] == ("Reply failed", 0)


def test_instagram_manual_reply_distinguishes_submission_from_delivery(monkeypatch):
    import actions.instagram_mcp as instagram_mcp
    import threading

    method, instance = _make_ig_reply_flow_test_instance()
    finished = threading.Event()

    def fake_send(_thread_id, _payload):
        finished.set()
        return {
            "success": False,
            "status": "submitted",
            "submitted": True,
            "delivery_verified": False,
            "error": "accepted but unverified",
        }

    monkeypatch.setattr(instagram_mcp, "send_direct_reply", fake_send)
    assert method(instance, "send it") is True
    assert finished.wait(2)
    assert instance.ui.finished[-1][1:] == ("Reply submitted", 90)
    assert any("not independently verified" in message for message in instance.spoken)


def test_instagram_manual_reply_accepts_verified_delivery_only_for_verified_result(monkeypatch):
    import actions.instagram_mcp as instagram_mcp
    import threading

    method, instance = _make_ig_reply_flow_test_instance()
    finished = threading.Event()

    def fake_send(_thread_id, _payload):
        finished.set()
        return {
            "success": True,
            "status": "delivered",
            "submitted": True,
            "delivery_verified": True,
        }

    monkeypatch.setattr(instagram_mcp, "send_direct_reply", fake_send)
    assert method(instance, "send it") is True
    assert finished.wait(2)
    assert instance.ui.finished[-1][1:] == ("Reply delivered", 100)
    assert "Message delivered." in instance.spoken
