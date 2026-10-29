"""History tab: git log for the currently-selected object.

Listens to the same `controller.object_selected` signal as the inspector and
re-fetches the commit list whenever the selection changes. Each row shows
short hash, date, author, and the first line of the commit message. The
"View at this commit…" button opens a read-only `ObjectAtCommitDialog` for
the selected commit.

History is per-file: there's no fall-through to document-level commits when
no object is selected — the panel just shows its empty-state label.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...service import ServiceError
from ..controller import RepoController
from ..dialogs.object_at_commit import ObjectAtCommitDialog


_HASH_ROLE = Qt.ItemDataRole.UserRole + 1


class HistoryTab(QWidget):
    EMPTY_MESSAGE = "Select a requirement to view its history."

    COLUMNS = ("Hash", "Issue", "Date", "Author", "Message")

    def __init__(self, controller: RepoController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._current_doc_id: str | None = None
        self._current_obj_id: str | None = None

        self._empty = QLabel(self.EMPTY_MESSAGE)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color: palette(mid); padding: 24px;")

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._refresh)
        self._view_button = QPushButton("View at this commit…")
        self._view_button.clicked.connect(self._view_at_commit)
        self._view_button.setEnabled(False)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSortingEnabled(False)
        self._table.itemDoubleClicked.connect(self._on_row_double_clicked)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(
            self.COLUMNS.index("Message"), QHeaderView.ResizeMode.Stretch
        )

        self._error_label = QLabel()
        self._error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_label.setStyleSheet("color: palette(mid); padding: 24px;")
        self._error_label.setWordWrap(True)

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self._view_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self._refresh_button)
        content_layout.addLayout(toolbar)
        content_layout.addWidget(self._table)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)        # 0
        self._stack.addWidget(self._content)      # 1
        self._stack.addWidget(self._error_label)  # 2

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._stack)

        self._stack.setCurrentWidget(self._empty)

        controller.repo_loaded.connect(self._on_repo_loaded)
        controller.object_selected.connect(self._on_selection)
        controller.edit_succeeded.connect(self._on_edit_succeeded)
        controller.committed.connect(self._on_committed)

    # --- controller signals -----------------------------------------------

    def _on_repo_loaded(self, *_args) -> None:
        self._current_doc_id = None
        self._current_obj_id = None
        self._table.setRowCount(0)
        self._stack.setCurrentWidget(self._empty)
        self._view_button.setEnabled(False)

    def _on_selection(self, doc_id: str, obj_id: str) -> None:
        self._current_doc_id = doc_id
        self._current_obj_id = obj_id
        self._refresh()

    def _on_edit_succeeded(self, *_args) -> None:
        # Edits don't auto-commit, but if the user committed externally
        # between selections, refreshing keeps the table honest. Cheap.
        self._refresh()

    def _on_committed(self, *_args) -> None:
        # New commit just landed — pull the updated log so the new row shows.
        self._refresh()

    # --- table ------------------------------------------------------------

    def _refresh(self) -> None:
        if self._current_doc_id is None or self._current_obj_id is None:
            self._table.setRowCount(0)
            self._stack.setCurrentWidget(self._empty)
            self._view_button.setEnabled(False)
            return

        commits = self._controller.object_history(
            self._current_doc_id, self._current_obj_id
        )

        if not commits:
            self._table.setRowCount(0)
            self._error_label.setText(
                "No history available — this object isn't tracked in a git repo "
                "or has no commits yet."
            )
            self._stack.setCurrentWidget(self._error_label)
            self._view_button.setEnabled(False)
            return

        self._populate_table(commits)
        self._stack.setCurrentWidget(self._content)

    def _populate_table(self, commits: list[dict]) -> None:
        self._table.setRowCount(len(commits))
        cols = self.COLUMNS
        for row, commit in enumerate(commits):
            short_hash = commit.get("short_hash", "")
            ts = commit.get("timestamp")
            date = (
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                if isinstance(ts, (int, float))
                else ""
            )
            cells = (
                short_hash,
                commit.get("issue", ""),
                date,
                commit.get("author", ""),
                commit.get("message", ""),
            )
            for col, text in zip(range(len(cols)), cells):
                item = QTableWidgetItem(text)
                item.setData(_HASH_ROLE, short_hash)
                self._table.setItem(row, col, item)
        self._table.resizeColumnToContents(cols.index("Hash"))
        self._table.resizeColumnToContents(cols.index("Issue"))
        self._table.resizeColumnToContents(cols.index("Date"))
        self._table.resizeColumnToContents(cols.index("Author"))

    # --- row interactions -------------------------------------------------

    def _on_selection_changed(self) -> None:
        self._view_button.setEnabled(self._selected_hash() is not None)

    def _on_row_double_clicked(self, item: QTableWidgetItem) -> None:
        short_hash = item.data(_HASH_ROLE)
        if not isinstance(short_hash, str) or not short_hash:
            return
        QGuiApplication.clipboard().setText(short_hash)

    def _selected_hash(self) -> str | None:
        items = self._table.selectedItems()
        if not items:
            return None
        short_hash = items[0].data(_HASH_ROLE)
        return short_hash if isinstance(short_hash, str) and short_hash else None

    def _view_at_commit(self) -> None:
        if self._current_doc_id is None or self._current_obj_id is None:
            return
        short_hash = self._selected_hash()
        if short_hash is None:
            return
        try:
            snapshot = self._controller.object_at_commit(
                self._current_doc_id, self._current_obj_id, short_hash
            )
        except ServiceError as e:
            QMessageBox.warning(
                self,
                "Cannot show object at commit",
                f"{short_hash}: {e}",
            )
            return
        dialog = ObjectAtCommitDialog(snapshot, self)
        dialog.exec()
