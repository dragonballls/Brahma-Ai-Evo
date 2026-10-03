"""Tiny lazy-import helpers used to keep Brahma Evo's idle footprint low.

Imports happen only on first attribute access/call.  This is deliberately small:
it avoids adding another dependency or background worker just to save startup RAM.
"""
from __future__ import annotations

import importlib
from typing import Any

__all__ = ["lazy_module", "lazy_attr"]


class _Lazy:
    __slots__ = ("_module_name", "_attr_name", "_loaded", "_value")

    def __init__(self, module_name: str, attr_name: str | None = None) -> None:
        self._module_name = module_name
        self._attr_name = attr_name
        self._loaded = False
        self._value: Any = None

    def _load(self) -> Any:
        if not self._loaded:
            module = importlib.import_module(self._module_name)
            self._value = getattr(module, self._attr_name) if self._attr_name else module
            self._loaded = True
        return self._value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Keep proxy internals on the proxy; forward runtime attributes to the
        # real object so lazy singletons remain transparent to callers.
        if name in self.__slots__:
            object.__setattr__(self, name, value)
            return
        setattr(self._load(), name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._load()(*args, **kwargs)

    def __repr__(self) -> str:
        target = self._module_name
        if self._attr_name:
            target += f":{self._attr_name}"
        return f"<lazy {target}>"

    def __bool__(self) -> bool:
        # Lazy proxies represent optional capabilities as present until used.
        return True


def lazy_module(module_name: str) -> Any:
    return _Lazy(module_name)


def lazy_attr(module_name: str, attr_name: str) -> Any:
    return _Lazy(module_name, attr_name)
