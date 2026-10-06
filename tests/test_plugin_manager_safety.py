from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manager_rejects_symlinked_plugins():
    source = (ROOT / "plugin_manager.py").read_text(encoding="utf-8")
    assert "if p.is_symlink():" in source
    assert "Refusing symlinked plugin" in source
    assert "resolved.relative_to(plugin_root)" in source


def test_plugin_manager_does_not_retry_hooks_after_internal_type_error():
    source = (ROOT / "plugin_manager.py").read_text(encoding="utf-8")
    assert "inspect.signature(fn)" in source
    assert "accepts_brahma" in source
    assert "except TypeError:" not in source[source.index("def dispatch"):source.index("return False", source.index("def dispatch"))]
