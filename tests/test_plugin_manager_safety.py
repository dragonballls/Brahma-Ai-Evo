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


def test_plugin_manager_namespaces_loaded_modules_and_removes_failed_entries():
    source = (ROOT / "plugin_manager.py").read_text(encoding="utf-8")
    assert 'module_name = f"_brahma_plugin_' in source
    assert 'sys.modules[module_name] = mod' in source
    assert 'sys.modules.pop(module_name, None)' in source
    assert 'sys.modules[p.stem]' not in source
