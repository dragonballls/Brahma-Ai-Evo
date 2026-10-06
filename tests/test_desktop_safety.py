from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_task_never_executes_generated_python():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert "def _execute_desktop_plan" in source
    assert "exec(compile(" not in source
    assert "_execute_generated_code" not in source
    assert '"actions"' in source


def test_desktop_task_has_a_bounded_declarative_action_allowlist():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert 'allowed = {"click"' in source
    assert "len(actions) > 20" in source
    assert "shell commands" in source
    assert "arbitrary code" in source

def test_platform_script_paths_are_escaped_before_interpolation():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert "def _escape_script_string" in source
    assert "escaped_path = _escape_script_string(str(path))" in source
    assert 'POSIX file "{escaped_path}"' in source
    assert "uri = \"file://\" + quote(" in source
    assert 'd.writeConfig("Image", "{uri}");' in source


def test_remote_wallpaper_download_is_scheme_bounded_and_size_bounded():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    start = source.index("def set_wallpaper_from_url")
    end = source.index("\ndef get_current_wallpaper", start)
    block = source[start:end]
    assert "urlparse" in block
    assert 'parsed.scheme.lower() not in {"http", "https"}' in block
    assert "max_bytes = 20 * 1024 * 1024" in block
    assert "tempfile.mkstemp" in block
    assert "urlretrieve" not in block


def test_desktop_task_rejects_explicitly_unsafe_plans_and_bounds_inputs():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert 'plan.get("unsafe")' in source
    assert "10,000-character safety limit" in source
    assert "screenshots must remain inside the user's home directory" in source


def test_windows_wallpaper_conversion_uses_race_safe_temp_creation():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    assert "tempfile.mkstemp(suffix=\".bmp\")" in source
    assert "os.close(fd)" in source
    assert "tempfile.mktemp(" not in source


def test_wallpaper_script_backends_check_exit_status_before_reporting_success():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    start = source.index("def set_wallpaper")
    block = source[start:source.index("def set_wallpaper_from_url", start)]
    assert "osascript exited with" in block
    assert "gsettings exited with" in block
    assert "qdbus exited with" in block
    assert "xfconf-query exited with" in block

def test_reminder_embeds_message_as_data_and_never_shell_executes():
    source = (ROOT / "actions" / "reminder.py").read_text(encoding="utf-8")
    assert "message_literal = json.dumps(safe_message" in source
    assert "<Description>MARK Reminder: {xml_message}</Description>" in source
    assert 'subprocess.run(\n            ["schtasks", "/Create"' in source
    assert "shell=True" not in source
    assert 'subprocess.run(["msg", "*", "/TIME:30", {message_literal}], shell=False)' in source

def test_remote_wallpaper_download_rejects_private_resolved_hosts():
    source = (ROOT / "actions" / "desktop.py").read_text(encoding="utf-8")
    start = source.index("def set_wallpaper_from_url")
    block = source[start:source.index("def get_current_wallpaper", start)]
    assert "def _validate_remote_http_target" in source
    assert "socket.getaddrinfo" in block
    assert "Remote wallpaper URLs may not target private or local network addresses." in block
    assert "safe_url = _validate_remote_http_target(url)" in block
