"""Modal dialog for renaming and deleting saved filters.

Mirrors `ManageViewsDialog`. Each Rename/Delete is committed to disk via
the controller; callers commit to git via the standard commit dialog.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..controller import RepoController


class ManageFiltersDialog(QDialog):
    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._doc_id = doc_id
        self.setWindowTitle("Manage filters")
        self.resize(360, 320)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_buttons)

        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.clicked.connect(self._rename_selected)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._delete_selected)

        side = QVBoxLayout()
        side.addWidget(self._rename_btn)
        side.addWidget(self._delete_btn)
        side.addStretch(1)

        body = QHBoxLayout()
        body.addWidget(self._list, 1)
        body.addLayout(side)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addLayout(body)
        layout.addWidget(buttons)

        self._reload()

    def _reload(self) -> None:
        self._list.clear()
        doc = self._current_doc()
        if doc is None:
            self._update_buttons()
            return
        for filt in doc.filters:
            item = QListWidgetItem(filt.name)
            item.setData(Qt.ItemDataRole.UserRole, filt.slug)
            self._list.addItem(item)
        self._update_buttons()

    def _current_doc(self):
        try:
            return self._controller.document(self._doc_id)
        except Exception:
            return None

    def _selected_slug(self) -> str | None:
        items = self._list.selectedItems()
        if not items:
            return None
        return items[0].data(Qt.ItemDataRole.UserRole)

    def _existing_names_excluding(self, slug: str | None) -> set[str]:
        doc = self._current_doc()
        if doc is None:
            return set()
        return {f.name for f in doc.filters if f.slug != slug}

    def _update_buttons(self) -> None:
        has_selection = self._selected_slug() is not None
        self._rename_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)

    def _rename_selected(self) -> None:
        slug = self._selected_slug()
        if slug is None:
            return
        doc = self._current_doc()
        if doc is None:
            return
        current = next((f.name for f in doc.filters if f.slug == slug), "")
        new_name, accepted = QInputDialog.getText(
            self, "Rename filter", "New name:", text=current
        )
        if not accepted:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        existing = {n.casefold() for n in self._existing_names_excluding(slug)}
        if new_name.casefold() in existing:
            QMessageBox.warning(
                self, "Name in use",
                f"A filter named {new_name!r} already exists.",
            )
            return
        try:
            self._controller.rename_filter(self._doc_id, slug, new_name)
        except Exception as exc:
            QMessageBox.warning(self, "Rename failed", str(exc))
            return
        self._reload()

    def _delete_selected(self) -> None:
        slug = self._selected_slug()
        if slug is None:
            return
        doc = self._current_doc()
        filt = next((f for f in (doc.filters if doc else ()) if f.slug == slug), None)
        if filt is None:
            return
        confirm = QMessageBox.question(
            self, "Delete filter",
            f"Delete filter {filt.name!r}? This removes its XML file.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._controller.delete_filter(self._doc_id, slug)
        except Exception as exc:
            QMessageBox.warning(self, "Delete failed", str(exc))
            return
        self._reload()
