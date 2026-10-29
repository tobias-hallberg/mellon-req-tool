"""Theme-tinted SVG icons.

The SVGs in this package all use `stroke="currentColor"`. At load time we
substitute that placeholder with the active theme's `fg_1` token and render to
a QPixmap via `QSvgRenderer`, so icons follow the dark/light palette without
shipping two sets.

Public API:

    icon(name)      -> QIcon    # for the current theme, cached
    refresh()                   # clear the cache after a theme switch

The module subscribes itself to `ThemeManager.theme_changed` on first use so
consumers only need to `setIcon(icon("..."))` again from their own
theme-change slots; they don't have to also prod us first.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Final

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ..theme import ThemeManager


_PIXMAP_SIZES: Final[tuple[int, ...]] = (16, 24, 32, 48, 64, 72, 96)

_cache: dict[tuple, QIcon] = {}
_subscribed = False


def _svg_text(name: str) -> str:
    path = files(__package__).joinpath(f"{name}.svg")
    return path.read_text(encoding="utf-8")


def _render(svg_bytes: bytes, size: int) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_bytes))
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pm


def icon(name: str) -> QIcon:
    """Return a `QIcon` for `name`, tinted for the current theme."""
    global _subscribed
    if not _subscribed:
        ThemeManager.instance().theme_changed.connect(lambda *_: refresh())
        _subscribed = True

    tokens = ThemeManager.instance().current_tokens()
    tint = tokens["fg_1"]
    key = (name, tint)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    tinted = _svg_text(name).replace("currentColor", tint)
    svg_bytes = tinted.encode("utf-8")

    qicon = QIcon()
    for size in _PIXMAP_SIZES:
        qicon.addPixmap(_render(svg_bytes, size), QIcon.Mode.Normal, QIcon.State.Off)
    _cache[key] = qicon
    return qicon


def toggle_icon(off_name: str, on_name: str) -> QIcon:
    """Return a `QIcon` whose Off state shows `off_name` and On state shows `on_name`.

    `QAbstractButton.isChecked()` flips between the two automatically, so a
    checkable `QToolButton` can swap glyphs without explicit wiring.
    """
    global _subscribed
    if not _subscribed:
        ThemeManager.instance().theme_changed.connect(lambda *_: refresh())
        _subscribed = True

    tokens = ThemeManager.instance().current_tokens()
    tint = tokens["fg_1"]
    key = (off_name, on_name, tint)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    off_bytes = _svg_text(off_name).replace("currentColor", tint).encode("utf-8")
    on_bytes = _svg_text(on_name).replace("currentColor", tint).encode("utf-8")

    qicon = QIcon()
    for size in _PIXMAP_SIZES:
        qicon.addPixmap(_render(off_bytes, size), QIcon.Mode.Normal, QIcon.State.Off)
        qicon.addPixmap(_render(on_bytes, size), QIcon.Mode.Normal, QIcon.State.On)
    _cache[key] = qicon
    return qicon


def refresh() -> None:
    """Drop cached icons. Consumers re-request with `icon(name)` on theme change."""
    _cache.clear()
