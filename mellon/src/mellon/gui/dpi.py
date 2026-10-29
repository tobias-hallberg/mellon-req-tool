"""DPI helpers for code that needs physical-pixel sizing.

In Qt 6, almost everything (widget geometry, QSS px/pt, QPainter coords) is
expressed in *logical* pixels and Qt scales by the device pixel ratio at
render time. The helpers here exist for the few cases where physical pixels
are the right unit — e.g. choosing an SVG render-target size that exactly
matches the screen, or pixel-snapping in custom-painted code that wants to
align with the device grid.
"""

from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


def device_pixel_ratio(widget: QWidget | None = None) -> float:
    """DPR of the screen `widget` is on, or the primary screen.

    Returns 1.0 when no QGuiApplication / screen is available, so callers
    can use this in module-level constants without crashing during import
    in headless contexts.
    """
    if widget is not None:
        screen = widget.screen()
    else:
        screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen is not None else 1.0


def dpi_scale(logical_px: float, widget: QWidget | None = None) -> int:
    """Convert `logical_px` to the equivalent physical-pixel count."""
    return round(logical_px * device_pixel_ratio(widget))
