from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rename_destination_is_confined_to_safe_root():
    source = (ROOT / "actions" / "file_controller.py").read_text(encoding="utf-8")
    start = source.index("def rename_file")
    end = source.index("def read_file", start)
    block = source[start:end]
    assert "new_path = target.parent / new_name" in block
    assert 'if not _is_safe_path(new_path):' in block
    assert "Access denied (destination)" in block


def test_file_controller_rejects_symlink_components_before_file_mutation():
    source = (ROOT / "actions" / "file_controller.py").read_text(encoding="utf-8")
    assert "def _has_symlink_component" in source
    assert "if _has_symlink_component(target):" in source
    assert "return True" in source[source.index("def _has_symlink_component"):source.index("def _is_safe_path")]
