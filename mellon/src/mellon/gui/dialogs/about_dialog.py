"""About dialog for the Mellon application.

Shows the Portal mark, wordmark, tagline, and version. Modal QDialog with a
single OK button. Used by both ProjectExplorerWindow and DocumentEditorWindow.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ..branding import about_pixmap


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Mellon")
        self.resize(360, 300)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 16)
        layout.setSpacing(6)

        mark = QLabel()
        mark.setPixmap(about_pixmap(96))
        mark.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(mark)

        wordmark = QLabel("Mellon")
        wordmark.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        wm_font = QFont()
        wm_font.setFamilies(["Inter", "Segoe UI", "system-ui", "sans-serif"])
        wm_font.setPointSize(28)
        wm_font.setWeight(QFont.Weight.DemiBold)
        # Brand handoff specifies -0.02em letter-spacing on the wordmark.
        wm_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 98)
        wordmark.setFont(wm_font)
        layout.addWidget(wordmark)

        tagline = QLabel("requirements management")
        tagline.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        tag_font = QFont()
        tag_font.setItalic(True)
        tag_font.setPointSize(11)
        tagline.setFont(tag_font)
        tagline.setStyleSheet("color: palette(mid);")
        layout.addWidget(tagline)

        layout.addSpacing(6)

        version = QLabel(f"version {__version__}")
        version.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        version.setStyleSheet("color: palette(mid);")
        layout.addWidget(version)

        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons, alignment=Qt.AlignmentFlag.AlignRight)


def show_about(parent: QWidget | None = None) -> None:
    dlg = AboutDialog(parent)
    dlg.exec()
