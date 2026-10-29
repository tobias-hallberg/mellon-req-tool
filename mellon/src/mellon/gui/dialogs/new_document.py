"""Modal dialog for creating a new document.

Collects a document id (validated against the same regex as
`mellon.editor._DOC_ID_RE`) and an optional title. The dialog does not touch
the service itself — the caller invokes `DocumentService.create_document()`
after the dialog accepts so error dialogs surface on the main window.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..controller import RepoController


_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class NewDocumentDialog(QDialog):
    def __init__(
        self,
        controller: RepoController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("New document")
        self.resize(380, 140)

        self._id_edit = QLineEdit()
        self._id_edit.setPlaceholderText("e.g. VCU3-SRS")

        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("Optional")

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: palette(mid); font-size: 10pt;")
        self._hint.setTextFormat(Qt.TextFormat.PlainText)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)

        form = QFormLayout()
        form.addRow("Document ID:", self._id_edit)
        form.addRow("Title:", self._title_edit)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addWidget(self._buttons)

        self._id_edit.textChanged.connect(self._revalidate)
        self._revalidate()
        self._id_edit.setFocus()

    def _revalidate(self) -> None:
        text = self._id_edit.text().strip()
        if not text:
            self._hint.setText("")
            self._ok_button.setEnabled(False)
            return
        if not _DOC_ID_RE.match(text):
            self._hint.setText("Use letters, digits, hyphens, underscores only.")
            self._ok_button.setEnabled(False)
            return
        if text in self._controller.docs():
            self._hint.setText(f"A document named {text!r} already exists.")
            self._ok_button.setEnabled(False)
            return
        self._hint.setText("")
        self._ok_button.setEnabled(True)

    def doc_id(self) -> str:
        return self._id_edit.text().strip()

    def title(self) -> str:
        return self._title_edit.text().strip()
