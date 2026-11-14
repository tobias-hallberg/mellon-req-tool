"""Show text from the repository or git as plain text.

Qt guesses whether the text of a label, tooltip, message box or input-dialog
label is HTML, from its first line (``Qt.mightBeRichText``). Text from another
clone, a commit or a branch name could then be read as markup: words hidden or
faked, and an ``<img>`` tag makes Qt open the file it names, which on Windows
sends the user's credentials to a host named in a network path.

- Labels the GUI builds get ``Qt.TextFormat.PlainText`` (:func:`label`,
  :func:`make_plain`).
- Sinks that can't be told the format (tooltips, the static ``QMessageBox`` and
  ``QInputDialog`` functions) get :func:`text`: the string itself, or an HTML
  rendering of it when Qt would take it for HTML, so it shows as written.
- Labels that are rich on purpose escape every value they interpolate
  (:func:`esc`).

The message-box functions keep the call shape of ``QMessageBox.warning`` and
co. and call them, so code and tests that handle those still do.
"""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import Qt as QtGuiNamespace  # convertFromPlainText, mightBeRichText
from PySide6.QtWidgets import QInputDialog, QLabel, QMessageBox, QWidget


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def text(value: object) -> str:
    """``value`` as a string that a sink guessing rich text shows as written."""
    s = "" if value is None else str(value)
    return QtGuiNamespace.convertFromPlainText(s) if QtGuiNamespace.mightBeRichText(s) else s


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def esc(value: object) -> str:
    """``value`` escaped for a label that is rich text on purpose."""
    return html.escape("" if value is None else str(value), quote=True)


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def make_plain(widget: QLabel | QMessageBox) -> QLabel | QMessageBox:
    """Set plain text format on ``widget`` (a message box's own labels
    included) and return it."""
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def label(value: object = "", parent: QWidget | None = None) -> QLabel:
    """A ``QLabel`` showing ``value`` as plain text."""
    lab = QLabel(parent)
    lab.setTextFormat(Qt.TextFormat.PlainText)
    lab.setText("" if value is None else str(value))
    return lab


# Requirements: MELLON-GUI-SRS-122
def form_label(value: object, buddy: QWidget | None = None) -> QLabel:
    """A plain-text row label for ``QFormLayout.addRow``, with ``buddy`` as
    its buddy as ``addRow(str, widget)`` would set."""
    lab = label(value)
    if buddy is not None:
        lab.setBuddy(buddy)
    return lab


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def warning(parent: QWidget | None, title: str, message: str, *args: Any, **kwargs: Any):
    return QMessageBox.warning(parent, title, text(message), *args, **kwargs)


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def critical(parent: QWidget | None, title: str, message: str, *args: Any, **kwargs: Any):
    return QMessageBox.critical(parent, title, text(message), *args, **kwargs)


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def information(parent: QWidget | None, title: str, message: str, *args: Any, **kwargs: Any):
    return QMessageBox.information(parent, title, text(message), *args, **kwargs)


# Requirements: MELLON-GUI-SRS-122, MELLON-VER-SRS-380
def question(parent: QWidget | None, title: str, message: str, *args: Any, **kwargs: Any):
    return QMessageBox.question(parent, title, text(message), *args, **kwargs)


# Requirements: MELLON-GUI-SRS-122
def get_text(parent: QWidget | None, title: str, prompt: str, *args: Any, **kwargs: Any):
    """``QInputDialog.getText`` with ``prompt`` shown as written."""
    return QInputDialog.getText(parent, title, text(prompt), *args, **kwargs)
