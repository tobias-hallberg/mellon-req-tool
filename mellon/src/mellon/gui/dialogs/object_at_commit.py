"""Read-only dialog showing one object as it existed at a historical commit.

Built from a controller-supplied `ObjectAtCommit` snapshot — the dialog does
no I/O itself, so it stays simple and easy to test. Mirrors the layout of
the live Inspector tab in read mode: id label, breadcrumb, attributes form,
description, outgoing links. No incoming links (those are a global cross-doc
computation that wouldn't be meaningful at a single point in history) and
no edit affordances.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..controller import ObjectAtCommit


class ObjectAtCommitDialog(QDialog):
    def __init__(
        self,
        snapshot: ObjectAtCommit,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._snapshot = snapshot
        self.setWindowTitle(
            f"Object {snapshot.obj_id} at {snapshot.short_hash}"
        )
        self.resize(560, 600)

        self._id_label = QLabel(f"{snapshot.doc_id} / {snapshot.obj_id}")
        self._id_label.setStyleSheet("font-weight: bold; font-size: 10pt;")
        self._id_label.setWordWrap(True)

        self._commit_label = QLabel(f"at commit {snapshot.short_hash}")
        self._commit_label.setStyleSheet("color: palette(mid);")

        self._breadcrumb = QLabel(
            " › ".join(snapshot.outline_path) if snapshot.outline_path else ""
        )
        self._breadcrumb.setStyleSheet("color: palette(mid);")
        self._breadcrumb.setWordWrap(True)

        self._attrs_form = QFormLayout()
        for name, value in snapshot.object.attrs.items():
            self._attrs_form.addRow(name, QLabel(value))
        self._attrs_wrap = QWidget()
        self._attrs_wrap.setLayout(self._attrs_form)

        self._description = QTextEdit()
        self._description.setReadOnly(True)
        self._description.setAcceptRichText(False)
        self._description.setMinimumHeight(160)
        self._description.setPlainText(snapshot.object.description or "")

        self._outgoing_label = QLabel("Outgoing links")
        self._outgoing_label.setStyleSheet("font-weight: 600; margin-top: 8px;")
        self._outgoing = QListWidget()
        if snapshot.outgoing:
            for link in snapshot.outgoing:
                item = QListWidgetItem(
                    f"{link.type}  →  {link.target_doc}/{link.target_id}"
                )
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._outgoing.addItem(item)
        else:
            self._outgoing.addItem(QListWidgetItem("(none)"))

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._buttons.rejected.connect(self.reject)
        self._buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self._id_label)
        layout.addWidget(self._commit_label)
        layout.addWidget(self._breadcrumb)
        layout.addWidget(self._attrs_wrap)
        layout.addWidget(QLabel("Description"))
        layout.addWidget(self._description)
        layout.addWidget(self._outgoing_label)
        layout.addWidget(self._outgoing)
        layout.addWidget(self._buttons)
