from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_screenshot_path_validation_fails_closed_instead_of_using_fallback():
    source = (ROOT / "actions" / "computer_control.py").read_text(encoding="utf-8")
    start = source.index("def _safe_screenshot_path")
    end = source.index("def _require_pyautogui", start)
    block = source[start:end]
    assert "Screenshot path may not contain symlinked components." in block
    assert "Screenshot path must remain inside the user's home directory." in block
    assert "return fallback" not in block
