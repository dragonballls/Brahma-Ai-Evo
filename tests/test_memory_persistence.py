from pathlib import Path

import pytest


def test_save_memory_does_not_trim_caller_when_atomic_write_fails(monkeypatch):
    import memory.memory_manager as mm

    original_limit = mm.MEMORY_MAX_CHARS
    memory = {
        "identity": {},
        "preferences": {
            "favorite_food": {"value": "a very long value " * 30, "updated": "2026-01-01"},
            "favorite_music": {"value": "another very long value " * 30, "updated": "2026-01-02"},
        },
        "projects": {},
        "relationships": {},
        "wishes": {},
        "notes": {},
    }
    before = repr(memory)
    monkeypatch.setattr(mm, "MEMORY_MAX_CHARS", 100)
    monkeypatch.setattr(
        mm,
        "_atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    try:
        with pytest.raises(OSError, match="disk full"):
            mm.save_memory(memory)
    finally:
        mm.MEMORY_MAX_CHARS = original_limit

    assert repr(memory) == before
    assert "favorite_food" in memory["preferences"]
    assert "favorite_music" in memory["preferences"]
