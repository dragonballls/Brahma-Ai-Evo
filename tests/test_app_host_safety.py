from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_application_host_checks_system_browser_open_result():
    source = (ROOT / "core" / "desktop" / "app_host.py").read_text(encoding="utf-8")
    assert "opened = webbrowser.open(url, new=0)" in source
    assert "if not opened:" in source
    block = source[source.index("if not opened:"):source.index("if not opened:") + 300]
    assert '"ok": False' in block
