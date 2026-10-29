"""Trace dialog: up + down recursive traceability for a single object.

Populated from `trace.trace_up` and `trace.trace_down`. Both walks are
cycle-safe already. Double-clicking a node selects it in the main window
and closes the dialog.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ...service import ServiceError
from ...trace import TraceEdge
from ..controller import RepoController


TARGET_ROLE = Qt.ItemDataRole.UserRole + 1


class TraceView(QDialog):
    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        obj_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle(f"Trace: {doc_id} / {obj_id}")
        self.resize(720, 560)

        header = QLabel(
            f"<b>{doc_id} / {obj_id}</b>  —  double-click a target to jump to it."
        )
        header.setStyleSheet("padding: 6px 0;")

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(["Target"])
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.doubleClicked.connect(self._on_double_clicked)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._buttons.rejected.connect(self.reject)
        self._buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self._tree)
        layout.addWidget(self._buttons)

        self._populate(doc_id, obj_id)
        self._tree.expandAll()

    # --- population -----------------------------------------------------

    def _populate(self, doc_id: str, obj_id: str) -> None:
        svc = self._controller.require_service()

        up_root = QStandardItem("↑ Up — what this object satisfies/refines")
        up_root.setEditable(False)
        try:
            up_edges = svc.trace_up(doc_id, obj_id)
        except ServiceError:
            up_edges = ()
        if up_edges:
            for edge in up_edges:
                up_root.appendRow(self._item_for(edge))
        else:
            placeholder = QStandardItem("(no outgoing links)")
            placeholder.setEditable(False)
            up_root.appendRow(placeholder)
        self._model.appendRow(up_root)

        down_root = QStandardItem("↓ Down — what refines/verifies this object")
        down_root.setEditable(False)
        try:
            down_edges = svc.trace_down(doc_id, obj_id)
        except ServiceError:
            down_edges = ()
        if down_edges:
            for edge in down_edges:
                down_root.appendRow(self._item_for(edge))
        else:
            placeholder = QStandardItem("(no incoming links)")
            placeholder.setEditable(False)
            down_root.appendRow(placeholder)
        self._model.appendRow(down_root)

    def _item_for(self, edge: TraceEdge) -> QStandardItem:
        label = f"{edge.link_type}  →  {edge.target_doc} / {edge.target_id}"
        item = QStandardItem(label)
        item.setEditable(False)
        item.setData((edge.target_doc, edge.target_id), TARGET_ROLE)
        for child in edge.children:
            item.appendRow(self._item_for(child))
        return item

    # --- navigation -----------------------------------------------------

    def _on_double_clicked(self, index: QModelIndex) -> None:
        payload = index.data(TARGET_ROLE)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        target_doc, target_id = payload
        self._controller.select_object(target_doc, target_id)
        self.accept()
