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