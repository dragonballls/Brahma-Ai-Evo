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


def test_direct_local_app_handler_gates_success_on_launch_result():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("if is_open_app_cmd:")
    end = source.index("memory_ctx = _memory_context_for_request", start)
    block = source[start:end]
    assert "_action_result_is_failure(result)" in block
    assert "self.speak(result)" in block
    assert 'self.speak(f"Opening {clean_app_candidate}, sir.")' not in block


def test_direct_diagnostic_handlers_gate_ui_completion_on_result():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("if is_ram_check:")
    end = source.index("# Autonomous Self-Healing", start)
    block = source[start:end]
    assert block.count("_action_result_is_failure(res)") >= 5
    assert "0 if failed else 100" in block


def test_smart_home_service_never_reports_provider_rejection_as_success():
    from smart_home.service import SmartHomeService

    class Storage:
        def get_device(self, _device_id):
            return {
                "id": "d1",
                "name": "Lamp",
                "provider_key": "fake",
                "provider_account_id": "a1",
                "is_on": False,
                "traits": {},
            }

        def get_provider_account(self, _account_id):
            return {"credentials": {}}

        def update_device(self, *args, **kwargs):
            raise AssertionError("state must not be updated after provider rejection")

        def log_activity(self, *args, **kwargs):
            pass

        def list_devices(self, *args, **kwargs):
            return [{
                "id": "d1",
                "name": "Lamp",
                "provider_key": "fake",
                "provider_account_id": "a1",
                "is_on": False,
                "traits": {},
            }]

    class Provider:
        def execute(self, _device, _action, _payload):
            return {"success": False, "error": "provider rejected"}

    service = SmartHomeService(storage=Storage())
    service._registry = type("Registry", (), {"get": lambda self, _key: Provider()})()
    result = service.execute_command("turn on lamp")
    assert result["success"] is False
    assert result["failures"]
    assert "provider rejected" in result["detail"]


def test_smart_home_restart_does_not_claim_success_on_provider_rejection():
    from smart_home.service import SmartHomeService

    class Storage:
        def get_device(self, _device_id):
            return {
                "id": "d1",
                "name": "Lamp",
                "provider_key": "fake",
                "provider_account_id": "a1",
            }

        def get_provider_account(self, _account_id):
            return {"credentials": {}}

        def log_activity(self, *args, **kwargs):
            pass

    class Provider:
        def execute(self, _device, _action, _payload):
            return {"success": False, "error": "restart rejected"}

    service = SmartHomeService(storage=Storage())
    service._registry = type("Registry", (), {"get": lambda self, _key: Provider()})()
    result = service.restart_device("d1")
    assert result["success"] is False
    assert "restart rejected" in result["error"]


def test_pending_reply_failure_and_delivery_states_are_honest():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("def _draft_and_send_reply(")
    end = source.index("def _parse_ig_reply_intent", start)
    block = source[start:end]
    assert "if _action_result_is_failure(result):" in block
    assert '"Reply failed", 0' in block
    assert '"Reply delivered." if verified else "Reply submitted"' in block
    assert "100 if verified else 90" in block
    assert "delivery was not independently verified" in block


def test_atomberg_provider_fails_closed_on_command_rejection():
    from smart_home.providers.builtin import AtombergProvider
    import smart_home.providers.builtin as builtin

    class Client:
        def send_command(self, _device_id, _command):
            return False

    device = {
        "name": "Fan",
        "external_id": "fan-1",
        "traits": {},
        "provider_credentials": {"api_key": "key", "refresh_token": "refresh"},
    }
    original = builtin.AtombergCloudClient
    builtin.AtombergCloudClient = lambda *_args, **_kwargs: Client()
    try:
        try:
            AtombergProvider().execute(device, "power", {"is_on": True})
        except RuntimeError as exc:
            assert "rejected" in str(exc).casefold()
        else:
            raise AssertionError("Atomberg provider claimed success after command rejection")
    finally:
        builtin.AtombergCloudClient = original


def test_unimplemented_smart_home_providers_are_not_advertised_as_available():
    from smart_home.providers.builtin import (
        HueProvider, LgProvider, DaikinProvider, TuyaProvider, NestProvider, SmartThingsProvider,
    )
    for provider_cls in (HueProvider, LgProvider, DaikinProvider, TuyaProvider, NestProvider, SmartThingsProvider):
        assert provider_cls.available is False
        assert provider_cls.coming_soon is True


def test_smart_home_service_rejects_unimplemented_provider_execution():
    from smart_home.service import SmartHomeService

    service = SmartHomeService.__new__(SmartHomeService)
    class Registry:
        def get(self, _key):
            from smart_home.providers.builtin import HueProvider
            return HueProvider()
    service._registry = Registry()
    try:
        service._require_available_provider("hue")
    except RuntimeError as exc:
        assert "not available" in str(exc).casefold()
    else:
        raise AssertionError("Unavailable smart-home provider crossed the execution boundary")


def test_smart_home_page_uses_provider_metadata_and_does_not_claim_fake_local_state():
    source = (ROOT / "smart_home_page_new.py").read_text(encoding="utf-8")
    assert "for info in self._service.list_platforms()" in source
    assert "card.setEnabled(platform.available)" in source
    assert "Integration not available yet." in source
    assert "Provider:</span>" in source
    assert "traits.get('firmware', 'N/A')" in source
    assert "traits.get('mac', 'N/A')" in source
    assert "traits.get('ip', 'N/A')" in source
    assert "Device action failed" in source
    assert "Device connection failed" in source


def test_self_coding_max_iteration_path_never_claims_completion():
    source = (ROOT / "actions" / "brahma_dev_agent.py").read_text(encoding="utf-8")
    assert "Developer task incomplete: the iteration limit was reached before the agent" in source
    assert 'Completed developer task after max iterations.' not in source


def test_smart_home_connect_rejects_unavailable_provider_before_discovery():
    from smart_home.service import SmartHomeService

    service = SmartHomeService.__new__(SmartHomeService)
    class Registry:
        def get(self, _key):
            from smart_home.providers.builtin import HueProvider
            return HueProvider()
    service._registry = Registry()
    try:
        service.connect_devices("hue", "Hue", {"bridge_ip": "127.0.0.1"}, ["fake"])
    except RuntimeError as exc:
        assert "not available" in str(exc).casefold()
    else:
        raise AssertionError("Unavailable provider reached connect/discovery path")


def test_smart_home_voice_control_changes_persisted_ptt_mode_instead_of_fake_states():
    source = (ROOT / "smart_home_page_new.py").read_text(encoding="utf-8")
    assert "get_push_to_talk_enabled()" in source
    assert "set_push_to_talk_enabled(enabled)" in source
    assert 'states = ["Idle", "Listening", "Thinking", "Executing", "Completed"]' not in source
    assert 'self._voice_state = "Push-to-Talk" if enabled else "Hands-Free"' in source


def test_smart_home_device_management_surfaces_persistence_failures():
    source = (ROOT / "smart_home_page_new.py").read_text(encoding="utf-8")
    assert "Rename failed" in source
    assert "Forget failed" in source
    assert "Restart failed" in source


def test_action_result_classifier_rejects_error_dicts_and_accepts_normal_dicts():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("def _action_result_is_failure")
    end = source.index("\ndef ", start + 5)
    block = source[start:end]
    assert 'error = result.get("error")' in block
    assert 'errors = result.get("errors")' in block
    assert 'if error not in (None, "")' in block


def test_instagram_reply_handler_does_not_claim_success_for_unverified_send():
    from pathlib import Path

    source = Path(ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index('            elif name == "instagram_reply"')
    end = source.index('            elif name == "system_manager"', start)
    block = source[start:end]

    assert "delivery was not independently verified" in block
    assert "Successfully sent manual reply" not in block
    assert 'if isinstance(send_result, dict) and send_result.get("delivery_verified") is True' in block
