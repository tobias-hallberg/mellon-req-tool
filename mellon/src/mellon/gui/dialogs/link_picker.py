"""Modal dialog for picking a link target + type.

Populated from `repo.all_objects(docs)`, restricted to the source document and
its declared `<parents>` (matches the policy enforced by `editor.add_link` and
the validator's `LINK_NOT_ALLOWED` check). A QLineEdit filter narrows the list
by substring; the link type is a free-text combo seeded with the common values
seen in the fixture.
"""

from __future__ import annotations

from PySide6.QtCore import QRegularExpression, QSortFilterProxyModel, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QListView,
    QVBoxLayout,
    QWidget,
)

from ..controller import RepoController


TARGET_ROLE = Qt.ItemDataRole.UserRole + 1

COMMON_LINK_TYPES = ("satisfies", "verifies", "refines", "implements")


class LinkPicker(QDialog):
    def __init__(
        self,
        controller: RepoController,
        source_doc: str,
        source_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._source_doc = source_doc
        self._source_id = source_id
        self.setWindowTitle(f"Add link from {source_doc}/{source_id}")
        self.resize(640, 520)

        self._type_combo = QComboBox()
        self._type_combo.setEditable(True)
        self._type_combo.addItems(list(COMMON_LINK_TYPES))
        self._type_combo.setCurrentText("satisfies")

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by id or title…")
        self._search.setClearButtonEnabled(True)

        self._model = QStandardItemModel()
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)

        self._list = QListView()
        self._list.setModel(self._proxy)
        self._list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self._list.doubleClicked.connect(lambda _ix: self.accept())
        self._search.textChanged.connect(self._on_search_changed)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Link type:", self._type_combo)
        form.addRow("Target:", self._search)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._list)
        layout.addWidget(self._buttons)

        self._populate()
        self._search.setFocus()

    # --- population + filter -------------------------------------------

    def _populate(self) -> None:
        svc = self._controller.require_service()
        source = svc.get_document(self._source_doc)
        allowed = {self._source_doc, *source.parents}
        for doc_id, obj_id, title in svc.all_objects():
            if doc_id not in allowed:
                continue  # source doc may only link to itself or its parents
            if doc_id == self._source_doc and obj_id == self._source_id:
                continue  # can't link to self
            label = f"{doc_id} / {obj_id}"
            if title:
                label += f"  —  {title}"
            item = QStandardItem(label)
            item.setEditable(False)
            item.setData((doc_id, obj_id), TARGET_ROLE)
            self._model.appendRow(item)
        # Pre-select the first row so Enter without scrolling works.
        if self._proxy.rowCount() > 0:
            self._list.setCurrentIndex(self._proxy.index(0, 0))

    def _on_search_changed(self, text: str) -> None:
        pattern = QRegularExpression(
            QRegularExpression.escape(text),
            QRegularExpression.PatternOption.CaseInsensitiveOption,
        )
        self._proxy.setFilterRegularExpression(pattern)
        # Keep a live selection so Enter always accepts *something*.
        if self._proxy.rowCount() > 0 and not self._list.currentIndex().isValid():
            self._list.setCurrentIndex(self._proxy.index(0, 0))

    # --- result ---------------------------------------------------------

    def selected(self) -> tuple[str, str, str] | None:
        """Return (target_doc, target_id, link_type) on accept, or None."""
        idx = self._list.currentIndex()
        if not idx.isValid():
            return None
        source_idx = self._proxy.mapToSource(idx)
        item = self._model.itemFromIndex(source_idx)
        if item is None:
            return None
        payload = item.data(TARGET_ROLE)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return None
        link_type = self._type_combo.currentText().strip()
        if not link_type:
            return None
        target_doc, target_id = payload
        return target_doc, target_id, link_type
