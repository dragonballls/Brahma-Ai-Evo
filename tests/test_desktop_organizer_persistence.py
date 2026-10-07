import os
from pathlib import Path

import pytest


def test_corrupt_history_is_preserved_and_load_fails_closed(tmp_path):
    from actions.desktop_organizer_mcp import SmartOrganizerEngine

    history = tmp_path / "organizer_history.json"
    raw = "{broken"
    history.write_text(raw, encoding="utf-8")

    engine = SmartOrganizerEngine()
    engine.history_file = history

    with pytest.raises(RuntimeError, match="refusing to substitute an empty history"):
        engine._load_history()

    assert history.exists()
    assert history.read_text(encoding="utf-8") == raw
    copies = list(tmp_path.glob("organizer_history.json.corrupt-*"))
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == raw


def test_corrupt_history_does_not_rename_a_replaced_path(tmp_path, monkeypatch):
    from actions.desktop_organizer_mcp import SmartOrganizerEngine

    history = tmp_path / "organizer_history.json"
    replacement = tmp_path / "replacement.json"
    history.write_text("{broken", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")

    engine = SmartOrganizerEngine()
    engine.history_file = history
    original_read = Path.read_text
    swapped = {"done": False}

    def race_read(self, *args, **kwargs):
        if self == history and not swapped["done"]:
            swapped["done"] = True
            history.replace(tmp_path / "original-corrupt.json")
            history.write_text("new-live", encoding="utf-8")
            return original_read(tmp_path / "original-corrupt.json", *args, **kwargs)
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", race_read)
    with pytest.raises(RuntimeError):
        engine._load_history()

    assert history.read_text(encoding="utf-8") == "new-live"
    assert (tmp_path / "original-corrupt.json").read_text(encoding="utf-8") == "{broken"
