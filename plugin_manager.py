import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any


class PluginManager:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.plugins_dir = self.base_dir / "plugins"
        self.plugins: list[Any] = []
        self.brahma = None

    def load_plugins(self) -> None:
        if not self.plugins_dir.exists():
            return
        plugin_root = self.plugins_dir.resolve()
        for p in sorted(self.plugins_dir.glob("*.py")):
            if p.name.startswith("__"):
                continue
            try:
                if p.is_symlink():
                    print(f"[Plugins] Refusing symlinked plugin: {p.name}")
                    continue
                resolved = p.resolve()
                resolved.relative_to(plugin_root)
                module_key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
                module_name = f"_brahma_plugin_{p.stem}_{module_key}"
                spec = importlib.util.spec_from_file_location(module_name, str(resolved))
                if not spec or not spec.loader:
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
                plugin = getattr(mod, "plugin", None)
                if plugin is None:
                    # fallback: accept module with functions
                    plugin = mod
                self.plugins.append(plugin)
                print(f"[Plugins] Loaded {p.name}")
            except Exception as exc:
                print(f"[Plugins] Failed to load {p.name}: {exc}")

    def register_brahma(self, brahma_obj) -> None:
        self.brahma = brahma_obj
        for p in list(self.plugins):
            try:
                fn = getattr(p, "on_brahma_created", None)
                if callable(fn):
                    fn(brahma_obj)
            except Exception as exc:
                print(f"[Plugins] on_brahma_created error: {exc}")

    def dispatch(self, hook: str, *args, **kwargs):
        """Call hook on plugins. If any plugin returns True, stop and return True."""
        for p in list(self.plugins):
            try:
                fn = getattr(p, hook, None)
                if callable(fn):
                    try:
                        signature = inspect.signature(fn)
                    except (TypeError, ValueError):
                        signature = None
                    accepts_brahma = bool(
                        signature
                        and (
                            "brahma" in signature.parameters
                            or any(
                                param.kind is inspect.Parameter.VAR_KEYWORD
                                for param in signature.parameters.values()
                            )
                        )
                    )
                    res = (
                        fn(*args, **kwargs, brahma=self.brahma)
                        if accepts_brahma
                        else fn(*args, **kwargs)
                    )
                    if res is True:
                        return True
            except Exception as exc:
                print(f"[Plugins] Hook {hook} error in {getattr(p,'__name__',str(p))}: {exc}")
        return False
