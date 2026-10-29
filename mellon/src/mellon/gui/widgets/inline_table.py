"""Inline editable table grid, used as the description-column editor for
objects that carry a `<table>`.

The widget hosts a real ``QTableWidget`` so cells edit in place, plus a small
toolbar and a context menu for structural edits (insert/delete rows & columns,
merge/split cells, header rows, column alignment).

Merged cells are the hard part: every structural op must keep each logical
cell rectangular. We get that for free by representing the table as a
*matrix of cell ids* (`_GridModel.M[r][c]`): a logical cell occupies every
matrix slot holding its id, and its colspan/rowspan are read back from the
bounding box. Insert/delete operate on whole row/column slices (rectangular by
construction) and merge always snaps the selection out to full span boundaries,
so the model can never represent a ragged cell.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...schema import (
    Table,
    TableCell,
    TableColumn,
    TableRow,
    table_geometry_errors,
)
from ..theme import ThemeManager

_QT_ALIGN: dict[str, Qt.AlignmentFlag] = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
}

# Per-cell padding (px) — matches the preview's HTML cellpadding so cell
# density reads the same in view and edit.
_CELL_PADDING = 4


class _GridModel:
    """Structural model: a matrix of cell ids plus per-column align / per-row
    header flags. Cell text lives in `texts`, keyed by cell id.
    """

    def __init__(
        self,
        aligns: list[str],
        headers: list[bool],
        matrix: list[list[int]],
        texts: dict[int, str],
    ) -> None:
        self.aligns = aligns
        self.headers = headers
        self.M = matrix
        self.texts = texts
        self._next = (max(texts) + 1) if texts else 0

    # --- dimensions -----------------------------------------------------

    @property
    def nrows(self) -> int:
        return len(self.M)

    @property
    def ncols(self) -> int:
        return len(self.aligns)

    def _new_id(self) -> int:
        cid = self._next
        self._next += 1
        return cid

    # --- conversion -----------------------------------------------------

    @classmethod
    def from_table(cls, table: Table) -> "_GridModel":
        ncols = max(len(table.columns), 1)
        aligns = [c.align for c in table.columns] or ["left"]
        headers = [r.header for r in table.rows]
        nrows = len(table.rows)
        matrix: list[list[int]] = [[-1] * ncols for _ in range(nrows)]
        texts: dict[int, str] = {}
        nid = 0
        for r, row in enumerate(table.rows):
            c = 0
            cell_iter = iter(row.cells)
            while c < ncols:
                if matrix[r][c] != -1:  # covered by a span placed from above
                    c += 1
                    continue
                try:
                    cell = next(cell_iter)
                except StopIteration:
                    break
                cs = min(max(cell.colspan, 1), ncols - c)
                rs = min(max(cell.rowspan, 1), nrows - r)
                cid = nid
                nid += 1
                texts[cid] = cell.text
                for rr in range(r, r + rs):
                    for cc in range(c, c + cs):
                        matrix[rr][cc] = cid
                c += cs
        # Defensive: fill any uncovered slot (only possible for an
        # inconsistent hand-edited table) with a fresh empty 1x1 cell.
        for r in range(nrows):
            for c in range(ncols):
                if matrix[r][c] == -1:
                    matrix[r][c] = nid
                    texts[nid] = ""
                    nid += 1
        if not headers and nrows == 0:
            # Empty table: seed a 1x1 so the editor isn't blank.
            matrix = [[nid]]
            texts[nid] = ""
            headers = [False]
        return cls(aligns, headers, matrix, texts)

    def to_table(self) -> Table:
        rows: list[TableRow] = []
        for r in range(self.nrows):
            cells: list[TableCell] = []
            for c in range(self.ncols):
                cid = self.M[r][c]
                top = r == 0 or self.M[r - 1][c] != cid
                left = c == 0 or self.M[r][c - 1] != cid
                if top and left:
                    cs = 1
                    while c + cs < self.ncols and self.M[r][c + cs] == cid:
                        cs += 1
                    rs = 1
                    while r + rs < self.nrows and self.M[r + rs][c] == cid:
                        rs += 1
                    cells.append(
                        TableCell(text=self.texts.get(cid, ""), colspan=cs, rowspan=rs)
                    )
            rows.append(TableRow(cells=tuple(cells), header=self.headers[r]))
        return Table(
            columns=tuple(TableColumn(align=a) for a in self.aligns),
            rows=tuple(rows),
        )

    # --- structural ops -------------------------------------------------

    def insert_row(self, i: int) -> None:
        ncols = self.ncols
        new_row: list[int] = []
        for c in range(ncols):
            # A vertical span crossing the boundary (same cell above & below)
            # is extended through the new row; otherwise a fresh 1x1 cell.
            if 0 < i < self.nrows and self.M[i - 1][c] == self.M[i][c]:
                new_row.append(self.M[i - 1][c])
            else:
                cid = self._new_id()
                self.texts[cid] = ""
                new_row.append(cid)
        self.M.insert(i, new_row)
        self.headers.insert(i, False)

    def delete_row(self, i: int) -> None:
        if self.nrows <= 1:
            return
        del self.M[i]
        del self.headers[i]

    def insert_col(self, j: int) -> None:
        for r in range(self.nrows):
            if 0 < j < self.ncols and self.M[r][j - 1] == self.M[r][j]:
                self.M[r].insert(j, self.M[r][j - 1])  # extend horizontal span
            else:
                cid = self._new_id()
                self.texts[cid] = ""
                self.M[r].insert(j, cid)
        self.aligns.insert(j, "left")

    def delete_col(self, j: int) -> None:
        if self.ncols <= 1:
            return
        for r in range(self.nrows):
            del self.M[r][j]
        del self.aligns[j]

    def _bbox(self, cid: int) -> tuple[int, int, int, int]:
        rs = [r for r in range(self.nrows) if cid in self.M[r]]
        cs = [c for r in range(self.nrows) for c in range(self.ncols) if self.M[r][c] == cid]
        return min(rs), min(cs), max(rs), max(cs)

    def merge(self, r0: int, c0: int, r1: int, c1: int) -> None:
        r0, r1 = sorted((r0, r1))
        c0, c1 = sorted((c0, c1))
        # Snap the rectangle out so it fully contains every overlapping cell —
        # guarantees the merged region stays rectangular.
        changed = True
        while changed:
            changed = False
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    br0, bc0, br1, bc1 = self._bbox(self.M[r][c])
                    if br0 < r0 or bc0 < c0 or br1 > r1 or bc1 > c1:
                        r0, c0 = min(r0, br0), min(c0, bc0)
                        r1, c1 = max(r1, br1), max(c1, bc1)
                        changed = True
        seen: list[int] = []
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if self.M[r][c] not in seen:
                    seen.append(self.M[r][c])
        joined = " ".join(t for t in (self.texts.get(i, "") for i in seen) if t)
        new_id = self._new_id()
        self.texts[new_id] = joined
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                self.M[r][c] = new_id

    def split(self, r: int, c: int) -> None:
        cid = self.M[r][c]
        positions = [
            (rr, cc)
            for rr in range(self.nrows)
            for cc in range(self.ncols)
            if self.M[rr][cc] == cid
        ]
        text = self.texts.get(cid, "")
        for idx, (rr, cc) in enumerate(positions):
            nid = self._new_id()
            self.texts[nid] = text if idx == 0 else ""
            self.M[rr][cc] = nid


class InlineTableWidget(QWidget):
    """Editable grid editor for a `Table`.

    Just the editable grid — structural controls live in the window's toolbar
    "Table" menu, which calls the public methods below on the active editor.
    Emits `commit_requested` (Ctrl+Enter, or focusing away to a non-popup) and
    `cancel_requested` (Esc); the hosting delegate wires these to the view's
    commit/close machinery. `to_table()` yields the edited `Table`.
    """

    commit_requested = Signal()
    cancel_requested = Signal()
    geometry_changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, read_only: bool = False) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self._committing = False
        self._read_only = read_only
        # Focus-out commits only after the widget has shown once, so the focus
        # churn during editor setup can't fire a premature commit/close.
        self._armed = False
        self._caption = ""
        self.model = _GridModel.from_table(Table())

        # Zero margins so the inner grid fills the editor exactly — the delegate
        # positions the editor to overlap the painted preview's grid region, and
        # any margin here would shift the grid relative to the preview.
        #
        # The caption is NOT shown here: the delegate keeps painting it (one
        # QTextDocument render path for both view and edit), and positions this
        # editor to cover only the grid *below* the painted caption. That avoids
        # the QLabel-vs-QTextDocument size divergence on HiDPI displays.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.table = QTableWidget(self)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        # Wrap long cell text within its (balanced) column instead of eliding,
        # so rows grow to fit — matching the preview, which is now a snapshot of
        # this same widget.
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        # Match the preview's cellpadding=4 + 1px rule so cell density and the
        # gridline read the same in view and edit.
        tokens = ThemeManager.instance().current_tokens()
        self.table.setStyleSheet(
            "QTableWidget { border: 0; gridline-color: %s; }"
            "QTableWidget::item { padding: %dpx; }"
            % (tokens["border_2"], _CELL_PADDING)
        )
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Sized to the full content height (like the preview), so the grid never
        # needs to scroll — a scrollbar would steal width and misalign columns.
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.table)

        if read_only:
            # The reusable preview instance (grabbed to a pixmap): never edits,
            # selects, or takes focus, so the snapshot is clean and it never
            # tries to commit.
            self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        else:
            self.table.setSelectionMode(
                QTableWidget.SelectionMode.ContiguousSelection
            )
            app = QApplication.instance()
            if app is not None:
                app.focusChanged.connect(self._on_focus_changed)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._armed = True

    # --- public API used by the delegate --------------------------------

    def set_table(self, table: Table, caption: str = "") -> None:
        self.model = _GridModel.from_table(table)
        self.set_caption(caption)
        self._rebuild()

    def set_caption(self, caption: str) -> None:
        # Stored only so the delegate can measure the caption's height to offset
        # the grid below it; the caption itself is painted by the delegate.
        self._caption = caption or ""

    def caption(self) -> str:
        return self._caption

    def to_table(self) -> Table:
        self._sync_texts()
        return self.model.to_table()

    def apply_layout(
        self,
        available_width: int | None = None,
        col_widths: list[int] | None = None,
    ) -> None:
        """Size the editor's columns to fill the width.

        When `col_widths` is supplied (the delegate extracts the preview's
        actual `QTextDocument` column widths), use them verbatim so the editor's
        columns line up pixel-for-pixel with the painted preview. Otherwise fall
        back to scaling content widths to fill `available_width` (used by
        standalone callers without a preview to mirror).
        """
        n = self.model.ncols
        if n == 0:
            return
        if available_width is None:
            available_width = self.table.viewport().width()
        if available_width <= 0:
            return
        if col_widths is not None and len(col_widths) == n:
            widths = [max(int(w), 1) for w in col_widths]
            widths[-1] = max(widths[-1] + available_width - sum(widths), 1)
        else:
            self.table.resizeColumnsToContents()
            widths = [max(self.table.columnWidth(c), 1) for c in range(n)]
            total = sum(widths)
            if total <= 0:
                return
            scale = available_width / total
            widths = [max(int(w * scale), 1) for w in widths]
            widths[-1] = max(widths[-1] + available_width - sum(widths), 1)
        for c, w in enumerate(widths):
            self.table.setColumnWidth(c, w)

    def content_height(self) -> int:
        # Grid-only height (the caption is painted by the delegate, not part of
        # this widget). Full height (uncapped) so the whole table shows in both
        # view and edit, exactly like the old HTML preview did; the row grows to
        # fit. The preview pixmap and the live editor both size to this.
        self.table.resizeRowsToContents()
        rows_h = sum(self.table.rowHeight(r) for r in range(self.model.nrows))
        if rows_h <= 0:
            rows_h = self.model.nrows * (self.fontMetrics().height() + 2 * _CELL_PADDING)
        return rows_h + 2 * self.table.frameWidth()

    # --- rendering ------------------------------------------------------

    def _rebuild(self) -> None:
        m = self.model
        tw = self.table
        tw.blockSignals(True)
        tw.clearSpans()
        tw.clear()
        tw.setRowCount(m.nrows)
        tw.setColumnCount(m.ncols)
        tokens = ThemeManager.instance().current_tokens()
        header_bg = QColor(tokens["bg_3"])
        header_fg = QColor(tokens["fg_0"])
        for r in range(m.nrows):
            for c in range(m.ncols):
                cid = m.M[r][c]
                top = r == 0 or m.M[r - 1][c] != cid
                left = c == 0 or m.M[r][c - 1] != cid
                if not (top and left):
                    continue
                cs = 1
                while c + cs < m.ncols and m.M[r][c + cs] == cid:
                    cs += 1
                rs = 1
                while r + rs < m.nrows and m.M[r + rs][c] == cid:
                    rs += 1
                item = QTableWidgetItem(m.texts.get(cid, ""))
                align = _QT_ALIGN.get(m.aligns[c], Qt.AlignmentFlag.AlignLeft)
                item.setTextAlignment(align | Qt.AlignmentFlag.AlignVCenter)
                if m.headers[r]:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setBackground(header_bg)
                    item.setForeground(header_fg)
                tw.setItem(r, c, item)
                if rs > 1 or cs > 1:
                    tw.setSpan(r, c, rs, cs)
        tw.blockSignals(False)
        # Column widths are set by the delegate's `_relayout_table_editor`
        # (it extracts the preview's exact widths) in response to the
        # `geometry_changed` signal below; don't pre-size here.
        tw.resizeRowsToContents()
        self.geometry_changed.emit()

    def _sync_texts(self) -> None:
        """Pull the live cell texts from the widget back into the model."""
        m = self.model
        for r in range(m.nrows):
            for c in range(m.ncols):
                cid = m.M[r][c]
                top = r == 0 or m.M[r - 1][c] != cid
                left = c == 0 or m.M[r][c - 1] != cid
                if top and left:
                    it = self.table.item(r, c)
                    if it is not None:
                        m.texts[cid] = it.text()

    # --- structural edits (called by the toolbar "Table" menu) ----------

    def _apply(self, fn, *args) -> None:
        """Sync live text, run a model mutation, rebuild, keep focus on the grid."""
        self._sync_texts()
        fn(*args)
        self._rebuild()
        self.table.setFocus()

    def add_row(self, above: bool) -> None:
        r = self.current_row()
        self._apply(self.model.insert_row, r if above else r + 1)

    def delete_row(self) -> None:
        self._apply(self.model.delete_row, self.current_row())

    def add_column(self, left: bool) -> None:
        c = self.current_column()
        self._apply(self.model.insert_col, c if left else c + 1)

    def delete_column(self) -> None:
        self._apply(self.model.delete_col, self.current_column())

    def merge_selection(self) -> None:
        rect = self._selection_rect()
        if rect is None:
            return
        r0, c0, r1, c1 = rect
        if r0 == r1 and c0 == c1:
            return  # nothing to merge
        self._apply(self.model.merge, r0, c0, r1, c1)

    def split_selection(self) -> None:
        self._apply(self.model.split, self.current_row(), self.current_column())

    def toggle_header_row(self) -> None:
        r = self.current_row()
        if not (0 <= r < self.model.nrows):
            return

        def _toggle() -> None:
            self.model.headers[r] = not self.model.headers[r]

        self._apply(_toggle)

    def set_column_alignment(self, name: str) -> None:
        c = self.current_column()
        if not (0 <= c < self.model.ncols):
            return

        def _set() -> None:
            self.model.aligns[c] = name

        self._apply(_set)

    # --- selection queries (drive menu enable/check state) --------------

    def _selection_rect(self) -> tuple[int, int, int, int] | None:
        ranges = self.table.selectedRanges()
        if not ranges:
            return None
        rg = ranges[0]
        return rg.topRow(), rg.leftColumn(), rg.bottomRow(), rg.rightColumn()

    def current_row(self) -> int:
        r = self.table.currentRow()
        return r if r >= 0 else max(self.model.nrows - 1, 0)

    def current_column(self) -> int:
        c = self.table.currentColumn()
        return c if c >= 0 else max(self.model.ncols - 1, 0)

    def has_selection(self) -> bool:
        return self.table.currentItem() is not None or bool(
            self.table.selectedRanges()
        )

    def selection_is_mergeable(self) -> bool:
        rect = self._selection_rect()
        if rect is None:
            return False
        r0, c0, r1, c1 = rect
        return not (r0 == r1 and c0 == c1)

    def current_is_merged(self) -> bool:
        r, c = self.current_row(), self.current_column()
        return self.table.rowSpan(r, c) > 1 or self.table.columnSpan(r, c) > 1

    def current_header(self) -> bool:
        r = self.current_row()
        return 0 <= r < self.model.nrows and self.model.headers[r]

    def current_alignment(self) -> str:
        c = self.current_column()
        return self.model.aligns[c] if 0 <= c < self.model.ncols else "left"

    # --- commit / cancel ------------------------------------------------

    def _commit(self) -> None:
        if self._committing:
            return
        table = self.to_table()
        problems = table_geometry_errors(table)
        if problems:
            QMessageBox.warning(
                self, "Invalid table", "Cannot save table:\n" + "\n".join(problems)
            )
            return
        self._committing = True
        self.commit_requested.emit()

    def _on_focus_changed(self, _old, now) -> None:
        if not self._armed or now is None or self._committing:
            return
        if now is self or self.isAncestorOf(now):
            return
        # A popup (the toolbar "Table" menu, a combobox list, etc.) is up —
        # focus left the editor only transiently; do NOT commit/close.
        if QApplication.activePopupWidget() is not None:
            return
        # Focus left the editor entirely — commit, but defer so any in-progress
        # cell editor flushes its text into the item first.
        QTimer.singleShot(0, self._commit_if_alive)

    def _commit_if_alive(self) -> None:
        try:
            if QApplication.activePopupWidget() is not None:
                return
            self._commit()
        except RuntimeError:
            # Underlying C++ object already deleted (editor closed) — ignore.
            pass

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.cancel_requested.emit()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._commit()
            event.accept()
            return
        super().keyPressEvent(event)
