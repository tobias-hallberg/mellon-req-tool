"""Full-size figure viewer.

Shows an object's attached SVG figure scaled-to-fit in a resizable dialog.
Renders via `QSvgRenderer` (the project's only SVG dependency, also used by
the grid delegate and the icon loader) rather than `QtSvgWidgets`, so the
dependency surface matches the rest of the GUI. Like the grid thumbnail, the
SVG goes through `qt_svg` (draw.io colours and labels) and sits on white.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QWidget,
)

from ...drawio import qt_svg


class _SvgView(QWidget):
    """Paints an SVG centered and scaled-to-fit, preserving aspect ratio."""

    def __init__(self, svg_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        try:
            data = qt_svg(Path(svg_path).read_bytes())
        except OSError:
            data = b""
        self._renderer = QSvgRenderer(QByteArray(data))
        self.setMinimumSize(160, 120)

    def is_valid(self) -> bool:
        return self._renderer.isValid()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if not self._renderer.isValid():
            return
        default = self._renderer.defaultSize()
        if default.width() <= 0 or default.height() <= 0:
            return
        avail = self.rect()
        aspect = default.height() / default.width()
        w = avail.width()
        h = int(round(w * aspect))
        if h > avail.height():
            h = avail.height()
            w = int(round(h / aspect)) if aspect else avail.width()
        x = avail.left() + (avail.width() - w) / 2
        y = avail.top() + (avail.height() - h) / 2
        target = QRectF(x, y, w, h)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(target, Qt.GlobalColor.white)
        self._renderer.render(painter, target)
        painter.end()


class FigureViewDialog(QDialog):
    """Modal full-size viewer for a single object's SVG figure."""

    def __init__(
        self,
        svg_path: str,
        obj_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Figure — {obj_id}")
        self.resize(720, 600)
        self.setSizeGripEnabled(True)

        self._view = _SvgView(svg_path, self)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self._view, 1)
        layout.addWidget(buttons)
