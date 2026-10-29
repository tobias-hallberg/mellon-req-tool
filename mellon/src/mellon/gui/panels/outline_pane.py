"""Left column of the Document Editor: outline tree for the current document.

Bound to a `RepoController`. Selection flows through the controller so the
grid + inspector stay in sync.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..controller import RepoController
from ..models.outline import OutlineModel


class OutlinePane(QWidget):
    def __init__(self, controller: RepoController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller

        self._outline_model = OutlineModel(controller)
        self.outline_view = QTreeView(self)
        self.outline_view.setModel(self._outline_model)
        self.outline_view.setHeaderHidden(True)
        self.outline_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.outline_view.setUniformRowHeights(True)
        self.outline_view.setIndentation(16)
        self.outline_view.clicked.connect(self._on_outline_clicked)

        controller.document_selected.connect(self._on_document_selected)
        controller.object_selected.connect(self._on_external_selection)

        self._pane_header = QLabel("OUTLINE")
        self._pane_header.setObjectName("PaneHeader")

        self.edit_toolbar = QToolBar(self)
        self.edit_toolbar.setMovable(False)
        self.edit_toolbar.setFloatable(False)
        self.edit_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.edit_toolbar.setIconSize(QSize(16, 16))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._pane_header)
        layout.addWidget(self.outline_view)
        layout.addWidget(self.edit_toolbar)

    # --- toolbar -------------------------------------------------------

    def set_edit_actions(self, actions: Iterable[QAction | None]) -> None:
        """Populate the toolbar above the tree. Pass `None` for separators."""
        self.edit_toolbar.clear()
        for a in actions:
            if a is None:
                self.edit_toolbar.addSeparator()
            else:
                self.edit_toolbar.addAction(a)
                btn = self.edit_toolbar.widgetForAction(a)
                if isinstance(btn, QToolButton):
                    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

    # --- click handlers ------------------------------------------------

    def _on_outline_clicked(self, index) -> None:
        obj_id = OutlineModel.obj_id_from_index(index)
        doc_id = self._controller.current_doc_id()
        if doc_id is not None and obj_id is not None:
            self._controller.select_object(doc_id, obj_id)

    def _on_document_selected(self, doc_id: str) -> None:
        self.outline_view.expandAll()

    def _on_external_selection(self, doc_id: str, obj_id: str) -> None:
        idx = self._find_outline_index(obj_id)
        if idx is not None:
            self.outline_view.setCurrentIndex(idx)
            self.outline_view.scrollTo(idx)

    def _find_outline_index(self, obj_id: str):
        model = self._outline_model

        def walk(parent):
            for row in range(model.rowCount(parent)):
                child = model.index(row, 0, parent)
                if OutlineModel.obj_id_from_index(child) == obj_id:
                    return child
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(self.outline_view.rootIndex())
