"""Table UI: model role, delegate sizing, inline editor, grid_pane actions.

Runs on Qt's offscreen platform. Window-level tests use the committed
`req-test/` fixture; the inline-editor tests drive `InlineTableWidget`
directly (no document needed).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()

pytestmark = pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(),
    reason="req-test fixture not available",
)


def _sample_table():
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    return Table(
        columns=(TableColumn("left"), TableColumn("right")),
        rows=(
            TableRow((TableCell("Signal"), TableCell("Latency")), header=True),
            TableRow((TableCell("OVERTEMP"), TableCell("200 ms"))),
        ),
    )


@pytest.fixture
def window(qtbot, tmp_path, clean_worktree):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)

    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    w = DocumentEditorWindow(docs[0], svc)
    w._set_read_only(False)
    qtbot.addWidget(w)
    return w


def _first_object(window) -> tuple[str, str]:
    doc_id = window.controller.current_doc_id()
    obj_id = next(iter(window.controller.document(doc_id).iter_outline_ids()))
    return doc_id, obj_id


# --- grid_pane actions ------------------------------------------------------


def test_insert_table_via_grid(window) -> None:
    doc_id, obj_id = _first_object(window)
    window.grid_pane.insert_table(doc_id, obj_id)
    assert window.controller.document(doc_id).objects[obj_id].table is not None


def test_remove_table_via_grid(window, monkeypatch) -> None:
    from mellon.gui.panels import grid_pane
    from PySide6.QtWidgets import QMessageBox

    doc_id, obj_id = _first_object(window)
    window.controller.require_service().set_table(doc_id, obj_id, _sample_table())
    window.controller.reload_document(doc_id)
    monkeypatch.setattr(
        grid_pane.QMessageBox,
        "question",
        lambda *a, **k: QMessageBox.StandardButton.Ok,
    )
    window.grid_pane.remove_table(doc_id, obj_id)
    assert window.controller.document(doc_id).objects[obj_id].table is None


# --- model role + delegate --------------------------------------------------


def test_model_exposes_table_role(window) -> None:
    from mellon.gui.models.objects_grid import OBJ_TABLE_ROLE
    from mellon.schema import Table
    from PySide6.QtCore import Qt

    doc_id, obj_id = _first_object(window)
    window.controller.require_service().set_table(doc_id, obj_id, _sample_table())
    window.controller.reload_document(doc_id)

    model = window.grid_pane._model
    desc_col = model.column_index("heading_or_text")
    row = model.row_for_obj_id(obj_id)
    idx = model.index(row, desc_col)
    assert isinstance(idx.data(OBJ_TABLE_ROLE), Table)
    # EditRole hands the editor the Table, not the caption string.
    assert isinstance(idx.data(Qt.ItemDataRole.EditRole), Table)


def test_delegate_sizehint_grows_for_table(window) -> None:
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QStyleOptionViewItem

    doc_id, obj_id = _first_object(window)
    model = window.grid_pane._model
    desc_col = model.column_index("heading_or_text")
    delegate = window.grid_pane.table.itemDelegateForColumn(desc_col)

    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 400, 20)
    opt.font = window.grid_pane.table.font()

    row = model.row_for_obj_id(obj_id)
    plain_h = delegate.sizeHint(opt, model.index(row, desc_col)).height()

    window.controller.require_service().set_table(doc_id, obj_id, _sample_table())
    window.controller.reload_document(doc_id)
    row = model.row_for_obj_id(obj_id)
    table_h = delegate.sizeHint(opt, model.index(row, desc_col)).height()

    assert table_h > plain_h


def test_table_row_has_no_figure_rect(window) -> None:
    """A table row exposes no figure, so a click never opens the figure viewer."""
    from PySide6.QtCore import QRect

    doc_id, obj_id = _first_object(window)
    window.controller.require_service().set_table(doc_id, obj_id, _sample_table())
    window.controller.reload_document(doc_id)

    model = window.grid_pane._model
    desc_col = model.column_index("heading_or_text")
    delegate = window.grid_pane.table.itemDelegateForColumn(desc_col)
    row = model.row_for_obj_id(obj_id)
    idx = model.index(row, desc_col)
    font = window.grid_pane.table.font()
    assert delegate.figure_rect(QRect(0, 0, 400, 80), font, idx) is None


# --- inline editor widget ---------------------------------------------------


def test_inline_widget_roundtrip(qtbot) -> None:
    from mellon.gui.widgets.inline_table import InlineTableWidget
    from mellon.schema import table_geometry_errors

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.set_table(_sample_table())
    # Edit a cell, then read the model back.
    w.table.item(1, 0).setText("FAULT_X")
    out = w.to_table()
    assert table_geometry_errors(out) == []
    assert out.rows[1].cells[0].text == "FAULT_X"
    assert out.rows[0].header is True


def test_inline_widget_merge_stays_valid(qtbot) -> None:
    from mellon.gui.widgets.inline_table import InlineTableWidget
    from mellon.schema import table_geometry_errors

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.set_table(_sample_table())
    w.model.merge(0, 0, 0, 1)  # merge the two header cells
    out = w.to_table()
    assert table_geometry_errors(out) == []
    assert out.rows[0].cells[0].colspan == 2


def test_inline_widget_commit_emits(qtbot) -> None:
    from mellon.gui.widgets.inline_table import InlineTableWidget

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.set_table(_sample_table())
    with qtbot.waitSignal(w.commit_requested, timeout=1000):
        w._commit()


def test_inline_widget_blocks_invalid_commit(qtbot, monkeypatch) -> None:
    from mellon.gui.widgets import inline_table
    from mellon.gui.widgets.inline_table import InlineTableWidget
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.set_table(_sample_table())

    # Force to_table to yield an inconsistent table; _commit must refuse it.
    bad = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow((TableCell("only-one"),)),),
    )
    monkeypatch.setattr(w, "to_table", lambda: bad)
    warned = {"n": 0}
    monkeypatch.setattr(
        inline_table.QMessageBox, "warning", lambda *a, **k: warned.__setitem__("n", 1)
    )
    fired = {"n": 0}
    w.commit_requested.connect(lambda: fired.__setitem__("n", 1))
    w._commit()
    assert warned["n"] == 1
    assert fired["n"] == 0


def test_inline_widget_public_api(qtbot) -> None:
    from mellon.gui.widgets.inline_table import InlineTableWidget
    from mellon.schema import table_geometry_errors
    from PySide6.QtWidgets import QTableWidgetSelectionRange

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.show()
    w.set_table(_sample_table())

    w.table.setCurrentCell(1, 0)
    w.add_row(above=False)
    assert len(w.to_table().rows) == 3
    w.add_column(left=False)
    assert len(w.to_table().columns) == 3

    w.table.clearSelection()
    w.table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 1), True)
    w.merge_selection()
    out = w.to_table()
    assert table_geometry_errors(out) == []
    assert out.rows[0].cells[0].colspan == 2

    w.table.setCurrentCell(2, 0)
    w.set_column_alignment("center")
    assert w.to_table().columns[0].align == "center"


def test_inline_widget_has_no_buttons(qtbot) -> None:
    """Controls live in the toolbar now — the editor is just the grid."""
    from mellon.gui.widgets.inline_table import InlineTableWidget
    from PySide6.QtWidgets import QPushButton

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.set_table(_sample_table())
    assert w.findChildren(QPushButton) == []


# --- toolbar Table menu (window-level) --------------------------------------


def _select(window, obj_id: str) -> str:
    doc_id = window.controller.current_doc_id()
    window.controller.select_object(doc_id, obj_id)
    window._refresh_action_states()
    return doc_id


def _with_table(window) -> tuple[str, str]:
    doc_id, obj_id = _first_object(window)
    window.controller.require_service().set_table(doc_id, obj_id, _sample_table())
    window.controller.reload_document(doc_id)
    _select(window, obj_id)
    return doc_id, obj_id


def test_table_menu_insert_for_plain_object(window) -> None:
    _, obj_id = _first_object(window)
    _select(window, obj_id)
    window._populate_table_menu()
    labels = [a.text() for a in window._table_menu.actions() if a.text()]
    assert labels == ["Insert table"]


def test_table_menu_structural_for_table_object(window) -> None:
    _with_table(window)
    window._populate_table_menu()
    labels = [a.text() for a in window._table_menu.actions() if a.text()]
    assert "Remove table" in labels
    assert "Add row below" in labels
    assert "Merge cells" in labels
    assert "Insert table" not in labels


def test_begin_table_edit_tracks_editor(window) -> None:
    _, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    assert window._active_table_editor is not None


def test_window_add_row_handler(window) -> None:
    _, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    before = len(window._active_table_editor.to_table().rows)
    window._table_op("add_row", above=False)
    assert len(window._active_table_editor.to_table().rows) == before + 1


def test_window_toggle_header_handler(window) -> None:
    _, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    ed = window._active_table_editor
    ed.table.setCurrentCell(1, 0)  # sample row 1 is not a header
    window._table_op("toggle_header_row")
    assert ed.to_table().rows[1].header is True


def test_table_op_refused_while_stale(window) -> None:
    doc_id, obj_id = _with_table(window)
    window.show()
    window._set_stale(True)
    window._table_op("add_row", above=False)
    assert window._live_editor() is None
    assert "Reload first (F5)" in window._status.currentMessage()
    assert len(window.controller.document(doc_id).objects[obj_id].table.rows) == 2


def test_table_op_commits_inspector_description_first(window) -> None:
    """Pending Inspector text is committed before the inline editor opens,
    so its reload can't drop the editor the op lands in."""
    doc_id, obj_id = _with_table(window)
    window.show()
    window.inspector._description.setPlainText("Typed, then Table > Add row.")
    window._table_op("add_row", above=False)
    obj = window.controller.document(doc_id).objects[obj_id]
    assert obj.description == "Typed, then Table > Add row."
    ed = window._live_editor()
    assert ed is not None and len(ed.to_table().rows) == 3


def test_window_table_edit_commits_and_persists(window, qtbot) -> None:
    doc_id, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    ed = window._active_table_editor
    window._table_op("add_row", above=False)
    ed.table.item(2, 0).setText("EXTRA")
    ed._commit()
    qtbot.wait(20)
    t = window.controller.document(doc_id).objects[obj_id].table
    assert t is not None and len(t.rows) == 3


# --- view/edit sizing consistency -------------------------------------------


def _table_delegate(window):
    gp = window.grid_pane
    return gp, gp.table.itemDelegateForColumn(gp._model.column_index("heading_or_text"))


def test_table_editor_insets_option_rect(window) -> None:
    """The editor overlays the preview: inset by the cell padding, width=wrap_w.

    Driven with a controlled `option.rect` so it doesn't depend on the
    offscreen view laying out column x-positions (which it doesn't).
    """
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QStyleOptionViewItem
    from mellon.gui.delegates import (
        _GRID_CELL_PADDING_H as PH,
        _GRID_CELL_PADDING_V as PV,
    )

    _, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    ed = window._active_table_editor
    assert ed is not None

    gp, delegate = _table_delegate(window)
    row = gp._model.row_for_obj_id(obj_id)
    proxy_idx = gp._proxy.mapFromSource(
        gp._model.index(row, gp._model.column_index("heading_or_text"))
    )
    opt = QStyleOptionViewItem()
    opt.rect = QRect(168, 10, 420, 80)
    opt.font = gp.table.font()
    delegate.updateEditorGeometry(ed, opt, proxy_idx)

    wrap_w = 420 - 2 * PH
    table = ed.to_table()
    caption = ed.caption()
    caption_offset = delegate._caption_offset(table, caption, wrap_w, opt.font)
    _pm, grid_h = delegate._grid_render(table, wrap_w, opt.font)

    g = ed.geometry()
    assert g.left() == 168 + PH
    # The editor covers only the grid, below the painted caption.
    assert g.top() == 10 + PV + caption_offset
    # Editor grid height equals the preview pixmap's grid height (one engine).
    assert g.height() == grid_h == ed.content_height()
    assert g.width() == wrap_w
    # Columns fill that width exactly (no content-min shrink).
    colsum = sum(ed.table.columnWidth(c) for c in range(ed.model.ncols))
    assert colsum == wrap_w


def test_table_sizehint_equals_caption_plus_grid_plus_padding(window) -> None:
    """View row height = caption + grid + 2*pad — the grid height is the same
    widget render the editor uses, so there's no vertical jump on edit."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtWidgets import QStyleOptionViewItem
    from mellon.gui.delegates import (
        _GRID_CELL_PADDING_H as PH,
        _GRID_CELL_PADDING_V as PV,
    )
    from mellon.gui.models.objects_grid import OBJ_TABLE_ROLE

    _, obj_id = _with_table(window)
    gp, delegate = _table_delegate(window)
    row = gp._model.row_for_obj_id(obj_id)
    src = gp._model.index(row, gp._model.column_index("heading_or_text"))
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 420, 20)
    opt.font = gp.table.font()

    table = src.data(OBJ_TABLE_ROLE)
    caption = str(src.data(Qt.ItemDataRole.DisplayRole) or "")
    wrap_w = 420 - 2 * PH
    caption_offset = delegate._caption_offset(table, caption, wrap_w, opt.font)
    _pm, grid_h = delegate._grid_render(table, wrap_w, opt.font)
    assert delegate.sizeHint(opt, src).height() == caption_offset + grid_h + 2 * PV


def test_table_editor_tracks_caption_but_has_no_label(window) -> None:
    """The caption is painted by the delegate (one render path), not a QLabel in
    the editor — so it can't diverge in size on edit. The editor only tracks the
    caption string to offset the grid below it."""
    from PySide6.QtWidgets import QLabel

    doc_id, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    ed = window._active_table_editor
    desc = window.controller.document(doc_id).objects[obj_id].description
    assert ed.caption() == desc
    assert ed.findChildren(QLabel) == []


def test_table_editor_stable_across_add_row(window) -> None:
    """A structural edit keeps the editor's left/width put (only height grows)."""
    _, obj_id = _with_table(window)
    window.show()
    window.grid_pane.begin_table_edit(obj_id)
    ed = window._active_table_editor
    g0 = ed.geometry()
    window._table_op("add_row", above=False)
    g1 = ed.geometry()
    assert g1.left() == g0.left() and g1.width() == g0.width()
    assert g1.height() > g0.height()


def test_inline_widget_apply_layout_fills_width(qtbot) -> None:
    from mellon.gui.widgets.inline_table import InlineTableWidget

    w = InlineTableWidget()
    qtbot.addWidget(w)
    w.show()
    w.set_table(_sample_table())
    w.apply_layout(300)
    colsum = sum(w.table.columnWidth(c) for c in range(w.model.ncols))
    assert colsum == 300


def _long_text_table():
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    long_text = "ewrsd aed gasd " * 12
    return Table(
        columns=(TableColumn(), TableColumn(), TableColumn()),
        rows=(
            TableRow(
                (TableCell("Column 1"), TableCell("Column 2"), TableCell("Col 3")),
                header=True,
            ),
            TableRow(
                (TableCell("test sdaf"), TableCell("sad fasdg"), TableCell("sfdasdf"))
            ),
            TableRow((TableCell(""), TableCell(""), TableCell(long_text))),
        ),
    )


def test_editor_columns_match_preview_and_are_balanced(window) -> None:
    """A long cell must not squeeze the other columns — the editor uses the
    preview's exact QTextDocument column widths (the screenshot regression)."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QStyleOptionViewItem
    from mellon.gui.delegates import _GRID_CELL_PADDING_H as PH

    t = _long_text_table()
    doc_id, obj_id = _first_object(window)
    window.controller.require_service().set_table(doc_id, obj_id, t)
    window.controller.reload_document(doc_id)
    window.controller.select_object(doc_id, obj_id)
    window._refresh_action_states()
    window.show()

    gp, delegate = _table_delegate(window)
    desc_col = gp._model.column_index("heading_or_text")
    proxy = gp._proxy.mapFromSource(
        gp._model.index(gp._model.row_for_obj_id(obj_id), desc_col)
    )
    gp.begin_table_edit(obj_id)
    ed = window._active_table_editor

    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 420, 20)
    opt.font = gp.table.font()
    delegate.updateEditorGeometry(ed, opt, proxy)
    wrap_w = 420 - 2 * PH

    expected = delegate._table_column_widths(t, "", wrap_w, ed.font())
    actual = [ed.table.columnWidth(c) for c in range(ed.model.ncols)]
    assert expected is not None
    assert actual == expected
    assert sum(actual) == wrap_w
    # No column is squeezed near-zero by the long Col 3 (the bug gave Col 1/2 a
    # sliver and wrapped their text into many tall lines).
    assert min(actual) >= wrap_w // 6


def test_preview_grid_matches_editor_one_engine(window) -> None:
    """The preview grid is a snapshot of the same widget the editor uses, so its
    size matches the editor's grid exactly (one rendering engine)."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QStyleOptionViewItem
    from mellon.gui.delegates import _GRID_CELL_PADDING_H as PH

    t = _long_text_table()
    doc_id, obj_id = _first_object(window)
    window.controller.require_service().set_table(doc_id, obj_id, t)
    window.controller.reload_document(doc_id)
    window.controller.select_object(doc_id, obj_id)
    window._refresh_action_states()
    window.show()

    gp, delegate = _table_delegate(window)
    desc_col = gp._model.column_index("heading_or_text")
    proxy = gp._proxy.mapFromSource(
        gp._model.index(gp._model.row_for_obj_id(obj_id), desc_col)
    )
    font = gp.table.font()
    wrap_w = 420 - 2 * PH

    pm, grid_h = delegate._grid_render(t, wrap_w, font)
    assert pm.width() == wrap_w
    # Pixmap is in device pixels; its logical height matches grid_h.
    assert round(pm.height() / pm.devicePixelRatio()) == grid_h

    gp.begin_table_edit(obj_id)
    ed = window._active_table_editor
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 420, 20)
    opt.font = font
    delegate.updateEditorGeometry(ed, opt, proxy)
    # The live editor's grid is exactly the height of the preview snapshot.
    assert ed.content_height() == grid_h
    assert ed.geometry().height() == grid_h


def test_grid_render_cache_reuses_pixmap(window) -> None:
    from mellon.gui.delegates import _GRID_CELL_PADDING_H as PH

    t = _sample_table()
    _with_table(window)  # ensures a delegate exists / app initialised
    gp, delegate = _table_delegate(window)
    font = gp.table.font()
    wrap_w = 420 - 2 * PH

    pm1, h1 = delegate._grid_render(t, wrap_w, font)
    pm2, h2 = delegate._grid_render(t, wrap_w, font)
    assert pm2 is pm1 and h2 == h1  # cached, no re-grab
    pm3, _ = delegate._grid_render(t, wrap_w + 50, font)
    assert pm3 is not pm1  # different width → fresh render


def test_preview_widget_is_render_only(window) -> None:
    """The reusable preview widget must render via grab() but never appear on
    screen (else it shows as a stray duplicate table at the viewport origin)."""
    from PySide6.QtCore import Qt
    from mellon.gui.delegates import _GRID_CELL_PADDING_H as PH

    _with_table(window)
    gp, delegate = _table_delegate(window)
    delegate._grid_render(_sample_table(), 420 - 2 * PH, gp.table.font())

    pv = delegate._preview_table
    assert pv is not None
    # Parentless → not in the view's child hierarchy → the view never paints it.
    assert pv.parent() is None
    assert pv.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen) is True
    assert pv.isVisible() is False


# MELLON-EDIT-SRS blind tests (176 and up)
from tests.edit_b_support import (  # noqa: E402
    _edit_cell,
    _gui_table_repo,
    _header_2x2,
    _open_table_editor,
    _plain,
    _save_table,
    menu_action,
)


def _saved_table(root: Path):
    from mellon.loader import load_document

    return load_document(root / "T").objects["T-0"].table


def _cells(table) -> list[list[tuple]]:
    return [[(c.text, c.colspan, c.rowspan) for c in row.cells]
            for row in table.rows]


def _select_cells(editor, top: int, left: int, bottom: int, right: int) -> None:
    from PySide6.QtWidgets import QTableWidgetSelectionRange

    grid = editor.table
    grid.clearSelection()
    grid.setCurrentCell(top, left)
    grid.setRangeSelected(QTableWidgetSelectionRange(top, left, bottom, right),
                          True)


def test_edit_srs_210_table_editor_saves_edited_cell_text(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-210: the table editor lets the user edit a cell's text.
    Change cell (1,1) of a 2x2 table to 'New text' and save: the saved table
    holds New text there and the other three cells are unchanged."""
    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    _edit_cell(qtbot, editor, 1, 1, ["New text"])
    _save_table(qtbot, editor)
    table = _saved_table(root)
    assert [[c.text for c in row.cells] for row in table.rows] == [
        ["h1", "h2"], ["a", "New text"]]
    assert [row.header for row in table.rows] == [True, False]


def test_edit_srs_211_table_editor_keeps_pasted_line_break(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-211: a cell text of two lines, first and second, given
    to the cell editor (pasted) and saved, is stored with the line break."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    QApplication.clipboard().setText("first\nsecond")
    _edit_cell(qtbot, editor, 1, 0, [(Qt.Key_V, Qt.ControlModifier)])
    _save_table(qtbot, editor)
    assert _saved_table(root).rows[1].cells[0].text == "first\nsecond"


def test_edit_srs_211_table_editor_keeps_typed_line_break(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-211: typing first, a line break (Enter, the key USAGE.md
    names for a newline in a table-cell editor) and second, then saving,
    stores first, line break, second."""
    from PySide6.QtCore import Qt

    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    _edit_cell(qtbot, editor, 1, 0, ["first", (Qt.Key_Return,), "second"])
    _save_table(qtbot, editor)
    assert _saved_table(root).rows[1].cells[0].text == "first\nsecond"


def test_edit_srs_212_cancelled_cell_edit_keeps_old_text(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-212: start editing a cell holding 'a', type 'new',
    cancel with Escape, then save: the cell still holds 'a' and the object
    file's bytes are unchanged."""
    root = _gui_table_repo(tmp_path, _header_2x2())
    obj_file = root / "T" / "objects" / "T-0.xml"
    old = obj_file.read_bytes()
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    _edit_cell(qtbot, editor, 1, 0, ["new"], finish="escape")
    assert editor.to_table().rows[1].cells[0].text == "a"
    _save_table(qtbot, editor)
    assert obj_file.read_bytes() == old


def _run_table_action(window, text: str, sub: str | None = None) -> None:
    action = menu_action(window, "Table", text)
    if sub is not None:
        assert action.menu() is not None, f"{text} has no submenu"
        action = next(a for a in action.menu().actions() if _plain(a.text()) == sub)
    assert action.isEnabled(), f"Table menu: {text} {sub or ''} is disabled"
    action.trigger()


def test_edit_srs_213_add_row_from_table_menu_is_saved_empty(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-213: the Table menu adds a row to the table being
    edited. After Add row below and save, the saved table has three rows and
    the new row's cells are empty; each position is covered once."""
    from mellon.schema import table_geometry_errors

    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(1, 0)
    _run_table_action(window, "Add row below")
    _save_table(qtbot, editor)
    table = _saved_table(root)
    assert len(table.rows) == 3
    texts = [[c.text for c in row.cells] for row in table.rows]
    assert ["h1", "h2"] in texts and ["a", "b"] in texts
    new_rows = [t for t in texts if t not in (["h1", "h2"], ["a", "b"])]
    assert new_rows == [["", ""]]
    assert table_geometry_errors(table) == []


def _one_column(texts: list[str]):
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    return Table(columns=(TableColumn("left"),),
                 rows=tuple(TableRow(cells=(TableCell(t),)) for t in texts))


def _one_row(texts: list[str]):
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    return Table(columns=tuple(TableColumn("left") for _ in texts),
                 rows=(TableRow(cells=tuple(TableCell(t) for t in texts)),))


def test_edit_srs_215_delete_row_from_table_menu(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-215: rows a, b, c; with a cell of row b selected,
    Delete row and save: the saved table has rows a, c."""
    root = _gui_table_repo(tmp_path, _one_column(["a", "b", "c"]))
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(1, 0)
    _run_table_action(window, "Delete row")
    _save_table(qtbot, editor)
    assert _cells(_saved_table(root)) == [[("a", 1, 1)], [("c", 1, 1)]]


def test_edit_srs_216_add_column_from_table_menu(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-216: on a 2-column table choose Add column right and
    save: the saved table has three columns and each position is covered
    once."""
    from mellon.schema import table_geometry_errors

    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(1, 1)
    _run_table_action(window, "Add column right")
    _save_table(qtbot, editor)
    table = _saved_table(root)
    assert len(table.columns) == 3
    assert table_geometry_errors(table) == []
    assert [sum(c.colspan for c in row.cells) for row in table.rows] == [3, 3]


def test_edit_srs_218_delete_column_from_table_menu(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-218: columns a, b, c in one row; with cell b selected,
    Delete column and save: the saved table has columns a, c."""
    root = _gui_table_repo(tmp_path, _one_row(["a", "b", "c"]))
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(0, 1)
    _run_table_action(window, "Delete column")
    _save_table(qtbot, editor)
    table = _saved_table(root)
    assert len(table.columns) == 2
    assert _cells(table) == [[("a", 1, 1), ("c", 1, 1)]]


def test_edit_srs_221_merge_cells_from_table_menu(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-221: select the 1x2 range of row 1 and choose Merge
    cells, then save: the saved row's first cell has column span 2 and the
    table still covers each position once."""
    from mellon.schema import table_geometry_errors

    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    _select_cells(editor, 1, 0, 1, 1)
    _run_table_action(window, "Merge cells")
    _save_table(qtbot, editor)
    table = _saved_table(root)
    assert len(table.rows[1].cells) == 1
    assert table.rows[1].cells[0].colspan == 2
    assert table_geometry_errors(table) == []


def test_edit_srs_224_split_cell_from_table_menu(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-224: a 2x2 table whose top-left cell spans both
    columns and both rows; select it, choose Split cell, save: four single
    cells, each position covered once."""
    from mellon.schema import Table, TableCell, TableColumn, TableRow, \
        table_geometry_errors

    merged = Table(columns=(TableColumn("left"), TableColumn("left")), rows=(
        TableRow(cells=(TableCell("all", colspan=2, rowspan=2),)),
        TableRow(cells=())))
    root = _gui_table_repo(tmp_path, merged)
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    _select_cells(editor, 0, 0, 0, 0)
    _run_table_action(window, "Split cell")
    _save_table(qtbot, editor)
    table = _saved_table(root)
    assert [len(row.cells) for row in table.rows] == [2, 2]
    assert all((c.colspan, c.rowspan) == (1, 1) for row in table.rows
               for c in row.cells)
    assert table_geometry_errors(table) == []


def test_edit_srs_226_header_row_toggles_back_to_body_row(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-226: with a cell of the header row selected, Header row
    makes it a body row again; saved, row 0 is a body row."""
    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(0, 0)
    _run_table_action(window, "Header row")
    _save_table(qtbot, editor)
    assert [row.header for row in _saved_table(root).rows] == [False, False]


def test_edit_srs_226_header_row_makes_body_row_a_header(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-226, other direction: with a cell of body row 1
    selected, Header row makes row 1 a header row."""
    from mellon.schema import Table, TableCell, TableColumn, TableRow

    plain = Table(columns=(TableColumn("left"),), rows=(
        TableRow(cells=(TableCell("a"),)), TableRow(cells=(TableCell("b"),))))
    root = _gui_table_repo(tmp_path, plain)
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(0, 0)
    _run_table_action(window, "Header row")
    _save_table(qtbot, editor)
    assert [row.header for row in _saved_table(root).rows] == [True, False]


@pytest.mark.parametrize("first,second", [("Right", "Left"), ("Center", "Right")])
def test_edit_srs_227_column_alignment_from_table_menu(tmp_path, qtbot, monkeypatch,
                                                       first, second):
    """MELLON-EDIT-SRS-227: set the first column's alignment from the Table
    menu and save; reopen and set it again: the saved alignment is the first
    choice, then the second; the other column stays left."""
    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(1, 0)
    _run_table_action(window, "Column alignment", first)
    _save_table(qtbot, editor)
    assert [c.align for c in _saved_table(root).columns] == [first.lower(), "left"]
    window.close()
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    editor.table.setCurrentCell(0, 0)
    _run_table_action(window, "Column alignment", second)
    _save_table(qtbot, editor)
    assert [c.align for c in _saved_table(root).columns] == [second.lower(), "left"]
