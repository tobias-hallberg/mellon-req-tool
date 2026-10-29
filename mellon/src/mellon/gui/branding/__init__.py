"""Mellon brand assets for the GUI.

Wraps the Portal-mark SVGs from `branding/*.svg` and renders them through
`QSvgRenderer` so the same source can be tinted to whatever theme the user
has chosen. Mirrors `gui/icons` but with two replacements (ink + accent) and
a wider size ladder so window/taskbar/About-dialog uses can all draw from
one cache.

Public API:

    window_icon()                -> QIcon          # for QMainWindow.setWindowIcon
    app_icon()                   -> QIcon          # for QApplication.setWindowIcon
    about_pixmap(size=96)        -> QPixmap        # for the About dialog
    refresh()                                       # invalidate cache

The accent dot in the source SVGs is hardcoded `#4fb3b8` (the dark-theme
accent). At render time we substitute it with `tokens["accent"]` so light
mode picks up `#1e8085` instead. Existing windows don't auto-refresh on
theme switch — title-bar icons stay in the previous accent until next
launch, which is invisible at title-bar size.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Final

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ..theme import ThemeManager


_ACCENT_LITERAL: Final[str] = "#4fb3b8"
_WINDOW_SIZES: Final[tuple[int, ...]] = (16, 20, 24, 32, 48, 64, 128, 256)
_APP_SIZES: Final[tuple[int, ...]] = (32, 48, 64, 128, 256, 512)

_cache: dict[tuple[str, int, str, str], QPixmap] = {}
_subscribed = False


def _svg_text(name: str) -> str:
    return files(__package__).joinpath(f"{name}.svg").read_text(encoding="utf-8")


def _render(name: str, size: int, *, ink: str | None, accent: str | None) -> QPixmap:
    body = _svg_text(name)
    if ink is not None:
        body = body.replace("currentColor", ink)
    if accent is not None:
        body = body.replace(_ACCENT_LITERAL, accent)
    renderer = QSvgRenderer(QByteArray(body.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pm


def _cached(name: str, size: int, ink: str | None, accent: str | None) -> QPixmap:
    key = (name, size, ink or "", accent or "")
    pm = _cache.get(key)
    if pm is not None:
        return pm
    pm = _render(name, size, ink=ink, accent=accent)
    _cache[key] = pm
    return pm


def _ensure_subscribed() -> None:
    global _subscribed
    if not _subscribed:
        ThemeManager.instance().theme_changed.connect(lambda *_: refresh())
        _subscribed = True


def _theme_colors() -> tuple[str, str]:
    tokens = ThemeManager.instance().current_tokens()
    return tokens["fg_1"], tokens["accent"]


def window_icon() -> QIcon:
    """Multi-resolution Portal mark for QMainWindow.setWindowIcon."""
    _ensure_subscribed()
    ink, accent = _theme_colors()
    qicon = QIcon()
    for size in _WINDOW_SIZES:
        name = "mellon-portal-minimal" if size < 24 else "mellon-portal-default"
        qicon.addPixmap(_cached(name, size, ink, accent), QIcon.Mode.Normal, QIcon.State.Off)
    return qicon


def app_icon() -> QIcon:
    """Rounded-tile app icon for QApplication.setWindowIcon.

    Theme-independent: the tile keeps its brand colors against any OS shell.
    """
    _ensure_subscribed()
    qicon = QIcon()
    for size in _APP_SIZES:
        qicon.addPixmap(
            _cached("mellon-app-icon-1024", size, None, None),
            QIcon.Mode.Normal,
            QIcon.State.Off,
        )
    return qicon


def about_pixmap(size: int = 96) -> QPixmap:
    """Default Portal mark for the About dialog. Theme-tinted."""
    _ensure_subscribed()
    ink, accent = _theme_colors()
    return _cached("mellon-portal-default", size, ink, accent)


def refresh() -> None:
    """Drop cached pixmaps. Consumers re-request on theme change."""
    _cache.clear()
