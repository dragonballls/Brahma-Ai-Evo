"""Brahma desktop environment package."""

__all__ = ["DesktopModeController"]


def __getattr__(name):
    if name == "DesktopModeController":
        from .controller import DesktopModeController
        return DesktopModeController
    raise AttributeError(name)
