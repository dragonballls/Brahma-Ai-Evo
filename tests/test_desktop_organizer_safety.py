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

def test_organizer_rejects_out_of_home_custom_targets():
    source = (ROOT / "actions" / "desktop_organizer_mcp.py").read_text(encoding="utf-8")
    start = source.index("def _resolve_target_dir")
    end = source.index("def _format_bytes", start)
    block = source[start:end]
    assert "Organizer target must remain inside the user's home directory." in block
    assert "Fallback to Downloads" not in block


def test_organizer_history_failure_rolls_back_changes():
    source = (ROOT / "actions" / "desktop_organizer_mcp.py").read_text(encoding="utf-8")
    assert "def _rollback_moves" in source
    assert "Organization was rolled back because its undo history could not be persisted." in source
    assert "Unable to read persistent organizer history." in source
