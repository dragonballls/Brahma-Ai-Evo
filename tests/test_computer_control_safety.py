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
    assert "requested = str(requested).strip() if requested else str(fallback)" in block


def test_screenshot_success_requires_a_real_nonempty_artifact(monkeypatch, tmp_path):
    import actions.computer_control as computer_control

    class Image:
        def save(self, _path):
            return None

    monkeypatch.setattr(computer_control, "_require_pyautogui", lambda: None)
    monkeypatch.setattr(computer_control.pyautogui, "screenshot", lambda: Image())
    target = tmp_path / "missing.png"
    result = computer_control._screenshot(str(target))
    assert result.startswith("Screenshot failed:")
    assert not target.exists()
