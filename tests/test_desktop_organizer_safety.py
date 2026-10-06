from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_organizer_imports_api_config_path_explicitly():
    source = (ROOT / "actions" / "desktop_organizer_mcp.py").read_text(encoding="utf-8")
    assert "from core.runtime_paths import API_CONFIG_PATH" in source


def test_desktop_organizer_rollback_confines_paths_and_never_overwrites():
    source = (ROOT / "actions" / "desktop_organizer_mcp.py").read_text(encoding="utf-8")
    start = source.index("def undo")
    end = source.index("def find_duplicates", start)
    block = source[start:end]
    assert "orig.relative_to(target_root)" in block
    assert "curr.relative_to(target_root)" in block
    assert "Refusing rollback overwrite" in block


def test_desktop_organizer_history_is_published_atomically():
    source = (ROOT / "actions" / "desktop_organizer_mcp.py").read_text(encoding="utf-8")
    assert "self._history_lock" in source
    assert "uuid.uuid4().hex" in source
    assert "temp.replace(self.history_file)" in source


def test_desktop_organizer_archive_collision_uses_incrementing_names():
    source = (ROOT / "actions" / "desktop_organizer_mcp.py").read_text(encoding="utf-8")
    start = source.index("def archive_old")
    block = source[start:]
    assert "while dest_file.exists():" in block
    assert "counter += 1" in block
    assert "int(datetime.now().timestamp())" not in block