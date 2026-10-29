"""Modal dialog for creating a new attribute-visibility view.

Collects a view name and a checkbox-per-attribute selection. The caller is
responsible for invoking `editor.create_view()` with the dialog's outputs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class NewViewDialog(QDialog):
    """Asks the user for a view name plus which user-attributes to include.

    `attributes` is the full list of user-attribute names available in the
    document. `pre_checked` is the subset to start checked (typically the
    grid's currently-visible set, so dismissing without changes captures
    the current state).
    """

    def __init__(
        self,
        attributes: list[str],
        pre_checked: set[str],
        existing_names: set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New view")
        self.resize(360, 360)
        self._existing_names = {n.strip().casefold() for n in existing_names}

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. QA review")

        self._list = QListWidget()
        for attr in attributes:
            item = QListWidgetItem(attr)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if attr in pre_checked
                else Qt.CheckState.Unchecked
            )
            self._list.addItem(item)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: palette(mid); font-size: 10pt;")
        self._hint.setTextFormat(Qt.TextFormat.PlainText)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)

        form = QFormLayout()
        form.addRow("Name:", self._name_edit)
        form.addRow("Visible attributes:", self._list)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addWidget(self._buttons)

        self._name_edit.textChanged.connect(self._revalidate)
        self._revalidate()
        self._name_edit.setFocus()

    def _revalidate(self) -> None:
        text = self._name_edit.text().strip()
        if not text:
            self._hint.setText("")
            self._ok_button.setEnabled(False)
            return
        if text.casefold() in self._existing_names:
            self._hint.setText(f"A view named {text!r} already exists.")
            self._ok_button.setEnabled(False)
            return
        self._hint.setText("")
        self._ok_button.setEnabled(True)

    def view_name(self) -> str:
        return self._name_edit.text().strip()

    def visible_attrs(self) -> list[str]:
        out: list[str] = []
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.text())
        return out
