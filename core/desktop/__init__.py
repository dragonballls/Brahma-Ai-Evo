"""Brahma desktop environment primitives.

The desktop package is intentionally isolated from the main UI.  It provides:
- a persistent workspace model,
- safe Windows window inspection/control,
- an adaptive resource governor,
- a click-through desktop rendering layer,
- and a controller that composes those pieces without replacing Explorer.
"""

from .controller import DesktopModeController

__all__ = ["DesktopModeController"]
