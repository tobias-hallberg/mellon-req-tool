"""Project-tree model: flat list of documents in the current repo.

A `QStandardItemModel` with one top-level item per doc. Each item stores its
doc_id in the UserRole so the view can retrieve it on click. Documents that
failed to load are listed too (greyed, with a warning icon and the load error
as tooltip) and carry their `DocLoadError` in `LOAD_ERROR_ROLE`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyle

from ...repo import DocLoadError
from ..controller import RepoController, describe_load_error


DOC_ID_ROLE = Qt.ItemDataRole.UserRole + 1
LOAD_ERROR_ROLE = Qt.ItemDataRole.UserRole + 2


class ProjectTreeModel(QStandardItemModel):
    def __init__(self, controller: RepoController) -> None:
        super().__init__()
        self._controller = controller
        self.setHorizontalHeaderLabels(["Documents"])
        controller.repo_loaded.connect(self._rebuild)
        self._rebuild()

    def _rebuild(self, *_args) -> None:
        self.removeRows(0, self.rowCount())
        rows: list[tuple[str, QStandardItem]] = []
        for doc_id, loaded in self._controller.docs().items():
            label = loaded.document.metadata.title or doc_id
            item = QStandardItem(f"{doc_id} — {label}" if label != doc_id else doc_id)
            item.setData(doc_id, DOC_ID_ROLE)
            item.setEditable(False)
            rows.append((doc_id, item))
        for err in self._controller.load_errors():
            item = QStandardItem(f"{err.doc_id} — failed to load")
            item.setData(err.doc_id, DOC_ID_ROLE)
            item.setData(err, LOAD_ERROR_ROLE)
            item.setToolTip(describe_load_error(err))
            item.setEditable(False)
            rows.append((err.doc_id, item))
        for _doc_id, item in sorted(rows, key=lambda r: r[0]):
            self.appendRow(item)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        # Colour and icon for failed documents are resolved at paint time so
        # they follow theme switches.
        if role in (Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.DecorationRole):
            if super().data(index, LOAD_ERROR_ROLE) is not None:
                if role == Qt.ItemDataRole.DecorationRole:
                    return QApplication.style().standardIcon(
                        QStyle.StandardPixmap.SP_MessageBoxWarning
                    )
                return QApplication.palette().color(
                    QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
                )
        return super().data(index, role)

    def refresh(self) -> None:
        """Re-read documents from the controller. Call after creating/deleting a doc."""
        self._rebuild()

    @staticmethod
    def doc_id_from_index(index) -> str | None:
        if not index.isValid():
            return None
        return index.data(DOC_ID_ROLE)

    @staticmethod
    def load_error_from_index(index) -> DocLoadError | None:
        """The load error of a document that failed to load, else None."""
        if not index.isValid():
            return None
        return index.data(LOAD_ERROR_ROLE)
