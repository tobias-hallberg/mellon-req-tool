"""Slice 6: polish — git status cell, Tab indent, Ctrl+P focus, filter-empty
state.
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


@pytest.fixture
def window(qtbot, tmp_path: Path, clean_worktree):
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
    return w, dst


# --- git status cell ------------------------------------------------------


def test_git_cell_shows_branch_and_hash(window):
    w, _ = window
    text = w._git_cell.text()
    # branch and short hash separated by " @ ".
    assert " @ " in text
    branch, rest = text.split(" @ ", 1)
    assert branch in ("master", "main")
    # 7-char short hash; may be followed by dirty marker.
    assert len(rest.split()[0]) == 7


def test_git_cell_dirty_marker_appears_after_edit(window):
    w, _ = window
    # Clean worktree: no " · dirty" suffix.
    before = w._git_cell.text()
    assert " dirty" not in before

    # Mutate via the GUI, which refreshes the cell on edit_succeeded.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    from PySide6.QtCore import Qt

    grid = w.grid_pane._model
    status_col = grid.column_index("status")
    row = grid.row_for_obj_id("VCU3-SRS-2")
    grid.setData(grid.index(row, status_col), "Approved", Qt.ItemDataRole.EditRole)

    after = w._git_cell.text()
    assert " dirty" in after


def test_git_cell_empty_when_not_in_git_repo(qtbot, tmp_path: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    # A plain directory with one doc but no .git/.
    (tmp_path / "DOC" / "objects").mkdir(parents=True)
    (tmp_path / "DOC" / "DOC.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<document id="DOC">
  <metadata><title>t</title><author>t</author><date>2026-04-22</date></metadata>
  <attributes>
    <attribute name="status" kind="enum" required="true"><option>Draft</option></attribute>
  </attributes>
  <outline/>
</document>
""",
        encoding="utf-8",
    )
    svc = DocumentService(tmp_path, cache=True)
    svc.get_all_documents()
    w = DocumentEditorWindow("DOC", svc)
    w._set_read_only(False)
    qtbot.addWidget(w)
    assert w._git_cell.text() == ""


# --- Tab / Shift+Tab indent-outdent ---------------------------------------


def test_tab_indents_via_grid_keypress(window):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    w, _ = window
    # Pick a top-level object preceded by a sibling so indent is valid.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    ev = QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier
    )
    w.grid_pane.table.keyPressEvent(ev)
    assert w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-10") == "VCU3-SRS-0"


def test_shift_tab_outdents_via_grid_keypress(window):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    w, _ = window
    # SRS-1 is nested under SRS-0.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    ev = QKeyEvent(
        QKeyEvent.Type.KeyPress,
        Qt.Key.Key_Backtab,
        Qt.KeyboardModifier.ShiftModifier,
    )
    w.grid_pane.table.keyPressEvent(ev)
    assert w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-1") is None


# --- filter-empty overlay ------------------------------------------------


def test_filter_empty_state_when_text_hides_everything(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    # A filter value guaranteed to match nothing.
    w.grid_pane.filter_box.setText("qqqqnonexistentqqqq")
    assert w.grid_pane._proxy.rowCount() == 0
    assert w.grid_pane._stack.currentWidget() is w.grid_pane._filter_empty


def test_clear_filters_restores_table(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    w.grid_pane.filter_box.setText("qqqqnonexistentqqqq")
    w.grid_pane.set_suspect_only(True)
    assert w.grid_pane.banner._suspect_only_btn.isChecked()
    # Click the clear button by emitting the signal.
    w.grid_pane.clear_filters_requested.emit()
    assert w.grid_pane.filter_box.text() == ""
    assert not w.grid_pane.is_suspect_only()
    assert not w.grid_pane.banner._suspect_only_btn.isChecked()
    assert w.grid_pane._stack.currentWidget() is w.grid_pane.table


def test_filter_empty_distinct_from_document_empty(qtbot, tmp_path: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    # Empty doc → document-empty state, not filter-empty.
    (tmp_path / "EMPTY" / "objects").mkdir(parents=True)
    (tmp_path / "EMPTY" / "EMPTY.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<document id="EMPTY">
  <metadata><title>e</title><author>t</author><date>2026-04-22</date></metadata>
  <attributes>
    <attribute name="status" kind="enum" required="true"><option>Draft</option></attribute>
  </attributes>
  <outline/>
</document>
""",
        encoding="utf-8",
    )
    svc = DocumentService(tmp_path, cache=True)
    svc.get_all_documents()
    w = DocumentEditorWindow("EMPTY", svc)
    qtbot.addWidget(w)
    assert w.grid_pane._stack.currentWidget() is w.grid_pane._empty
