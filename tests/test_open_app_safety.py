from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_app_alias_matching_is_boundary_aware():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    assert "import re" in source
    assert "(?<!\\w)" in source
    assert "(?!\\w)" in source
    assert "alias_key in key or key in alias_key" not in source


def test_process_detection_requires_exact_application_name():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    start = source.index("def _is_running")
    end = source.index("def _launch_windows", start)
    block = source[start:end]
    assert "proc_name == app_lower" in block
    assert "app_lower in proc_name" not in block
    assert "proc_name in app_lower" not in block


def test_linux_launcher_checks_gtk_launch_exit_code():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    start = source.index("def _launch_linux")
    end = source.index("_OS_LAUNCHERS", start)
    block = source[start:end]
    assert "return result.returncode == 0" in block


def test_start_menu_fallback_is_not_reported_as_confirmed_without_process_check():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    start = source.index("# Fallback to Start Menu search")
    end = source.index("def _launch_macos", start)
    block = source[start:end]
    assert "return bool(_PSUTIL and _is_running(app_name))" in block

def test_open_app_fallbacks_do_not_assume_launch_success():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    mac_start = source.index("def _launch_macos")
    linux_start = source.index("def _launch_linux")
    mac = source[mac_start:linux_start]
    linux = source[linux_start:source.index("_OS_LAUNCHERS", linux_start)]
    assert "return bool(_PSUTIL and _is_running(app_name))" in mac
    assert "return result.returncode == 0" in linux


def test_spotify_browser_fallback_propagates_launcher_failure():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    assert 'if not _open_url_in_chrome("https://open.spotify.com"):' in source
    spotify = (ROOT / "actions" / "spotify_controller.py").read_text(encoding="utf-8")
    assert "return bool(webbrowser.open(url))" in spotify


def test_direct_windows_and_linux_binary_launches_check_child_liveness():
    source = (ROOT / "actions" / "open_app.py").read_text(encoding="utf-8")
    for marker in ("proc = subprocess.Popen([cp]", "proc = subprocess.Popen([sp]", "proc = subprocess.Popen([bin_path]", "proc = subprocess.Popen([binary]"):
        start = source.index(marker)
        block = source[start:source.find("\n\n", start)]
        assert "proc.poll()" in block
        assert "return False" in block
