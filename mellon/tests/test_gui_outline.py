"""Slice 3: outline mutations (new sibling/child, indent/outdent, move, delete).

Exercises the MainWindow action handlers directly so we don't have to simulate
toolbar clicks. Runs against a tmp copy of `req-test/` so edits don't dirty
the committed fixture.
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


# --- new sibling / child ---------------------------------------------------


def test_new_sibling_at_top_level(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")  # top-level
    before = set(w.controller.document("VCU3-SRS").objects.keys())
    w._new_sibling()
    after = set(w.controller.document("VCU3-SRS").objects.keys())
    new_ids = after - before
    assert len(new_ids) == 1
    new_id = new_ids.pop()
    # Current selection jumped to the new object.
    assert w.controller.current_obj_id() == new_id
    # It's a sibling — same parent as the selection (top-level).
    assert w.controller.outline_parent_id("VCU3-SRS", new_id) is None


def test_outline_shortcuts_work_with_outline_pane_hidden(window, qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    w, _ = window
    w.show()
    qtbot.waitExposed(w)
    w.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is w)
    w._set_outline_pane_visible(False)
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    before = set(w.controller.document("VCU3-SRS").objects.keys())
    w.grid_pane.setFocus()
    qtbot.keyClick(w.grid_pane, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    after = set(w.controller.document("VCU3-SRS").objects.keys())
    assert len(after - before) == 1


def test_new_child_nests_under_selection(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-14")  # Power Management heading
    before = set(w.controller.document("VCU3-SRS").objects.keys())
    w._new_child()
    after = set(w.controller.document("VCU3-SRS").objects.keys())
    new_id = (after - before).pop()
    assert w.controller.outline_parent_id("VCU3-SRS", new_id) == "VCU3-SRS-14"


# --- indent / outdent ------------------------------------------------------


def test_indent_makes_obj_child_of_previous_sibling(window):
    w, _ = window
    # VCU3-SRS-10 is top-level, preceded by VCU3-SRS-0. After indent it should
    # be a child of -0.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    w._indent()
    assert w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-10") == "VCU3-SRS-0"


def test_indent_noop_on_first_sibling(window):
    w, _ = window
    # VCU3-SRS-0 is the first top-level; no previous sibling.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-0")
    w._indent()
    assert w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-0") is None


def test_outdent_promotes_one_level(window):
    w, _ = window
    # VCU3-SRS-1 is under VCU3-SRS-0. Outdent should promote it to top level.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    w._outdent()
    assert w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-1") is None


def test_outdent_noop_at_top_level(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-0")
    w._outdent()  # no-op; stays top-level
    assert w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-0") is None


# --- move up / down --------------------------------------------------------


def test_move_up_swaps_with_previous(window):
    w, _ = window
    # Children of VCU3-SRS-0 are: 1, 2, 11, 12, 13. Move SRS-2 up -> before SRS-1.
    before = w.controller.outline_siblings("VCU3-SRS", "VCU3-SRS-2")
    assert before[:3] == ["VCU3-SRS-1", "VCU3-SRS-2", "VCU3-SRS-11"]
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    w._move_up()
    after = w.controller.outline_siblings("VCU3-SRS", "VCU3-SRS-2")
    assert after[:3] == ["VCU3-SRS-2", "VCU3-SRS-1", "VCU3-SRS-11"]


def test_move_down_swaps_with_next(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    w._move_down()
    after = w.controller.outline_siblings("VCU3-SRS", "VCU3-SRS-1")
    assert after[:3] == ["VCU3-SRS-2", "VCU3-SRS-1", "VCU3-SRS-11"]


# --- delete ----------------------------------------------------------------


def test_delete_with_no_incoming_prompts_then_succeeds(window, monkeypatch):
    w, root = window
    # VCU3-STS-11 has no incoming links; one outgoing link to SRS-24.
    w.controller.select_object("VCU3-STS", "VCU3-STS-11")

    confirm_calls = []
    monkeypatch.setattr(
        w, "_confirm_delete", lambda obj_id: confirm_calls.append(obj_id) or True
    )
    w._delete_selected()

    # The generic confirmation must always be shown.
    assert confirm_calls == ["VCU3-STS-11"]

    assert "VCU3-STS-11" not in w.controller.document("VCU3-STS").objects
    assert not (root / "VCU3-STS" / "objects" / "VCU3-STS-11.xml").exists()


def test_delete_with_incoming_prompts_and_cascades(window, monkeypatch):
    w, root = window
    # VCU3-SRS-1 has incoming links from STS-1.
    from mellon.gui import main_window as mw

    confirm_calls = []
    monkeypatch.setattr(
        w, "_confirm_delete", lambda obj_id: confirm_calls.append(obj_id) or True
    )
    question_calls = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "question",
        lambda *args, **kwargs: question_calls.append(args)
        or mw.QMessageBox.StandardButton.Ok,
    )

    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    w._delete_selected()

    # Up-front confirm + incoming-links second-stage dialog.
    assert confirm_calls == ["VCU3-SRS-1"]
    assert len(question_calls) == 1
    assert "VCU3-STS-1" in question_calls[0][2]

    # SRS-1 is gone and the STS link file has been cleaned up.
    assert "VCU3-SRS-1" not in w.controller.document("VCU3-SRS").objects
    assert not (root / "VCU3-STS" / "links" / "VCU3-STS-1.xml").exists()


def test_delete_moves_selection_off_the_deleted_object(window, monkeypatch):
    """After a delete no pane keeps showing (or editing) the deleted object:
    the selection moves to the document's first object."""
    w, _ = window
    w.controller.select_object("VCU3-STS", "VCU3-STS-11")
    assert w.inspector._current_obj_id == "VCU3-STS-11"
    monkeypatch.setattr(w, "_confirm_delete", lambda obj_id: True)

    w._delete_selected()

    doc = w.controller.document("VCU3-STS")
    assert "VCU3-STS-11" not in doc.objects
    assert w.controller.current_obj_id() == doc.iter_outline_ids()[0]
    assert w.inspector._current_obj_id == w.controller.current_obj_id()


def test_delete_cancelled_leaves_doc_intact(window, monkeypatch):
    w, root = window
    monkeypatch.setattr(w, "_confirm_delete", lambda obj_id: False)
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    w._delete_selected()

    # Nothing removed.
    assert "VCU3-SRS-1" in w.controller.document("VCU3-SRS").objects
    assert (root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml").exists()


def test_delete_confirmation_has_no_dont_ask_again(window, monkeypatch):
    """There's no undo, so every delete asks (Cancel is the default) and the
    prompt offers no "Don't ask again" that would silence the next one."""
    from PySide6.QtWidgets import QMessageBox

    w, _ = window
    shown = []

    def fake_exec(box):
        cancel = box.button(QMessageBox.StandardButton.Cancel)
        shown.append((box.checkBox() is None, box.defaultButton() is cancel))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)

    for obj_id in ("VCU3-STS-11", "VCU3-STS-10"):
        w.controller.select_object("VCU3-STS", obj_id)
        w._delete_selected()

    # (no checkbox, Cancel is the default) for each of the two deletes.
    assert shown == [(True, True), (True, True)]
    objects = w.controller.document("VCU3-STS").objects
    assert "VCU3-STS-11" not in objects and "VCU3-STS-10" not in objects


def test_delete_refused_when_object_has_children(window, monkeypatch):
    w, _ = window
    from mellon.gui import main_window as mw

    warn_calls = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "warning",
        lambda *args, **kwargs: warn_calls.append(args)
        or mw.QMessageBox.StandardButton.Ok,
    )
    question_calls = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "question",
        lambda *args, **kwargs: question_calls.append(args)
        or mw.QMessageBox.StandardButton.Cancel,
    )

    # VCU3-SRS-0 has children; the GUI should pre-empt with a modal warning
    # and never reach the confirmation dialog or the service call.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-0")
    w._delete_selected()

    assert len(warn_calls) == 1
    body = warn_calls[0][2]
    assert "VCU3-SRS-0" in body
    assert "sub-objects" in body
    assert question_calls == []
    # Still present.
    assert "VCU3-SRS-0" in w.controller.document("VCU3-SRS").objects


# --- empty-state overlay ---------------------------------------------------


def test_empty_state_visible_when_document_has_no_objects(qtbot, tmp_path: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    # Minimal empty doc.
    doc_root = tmp_path / "EMPTY"
    (doc_root / "objects").mkdir(parents=True)
    (doc_root / "EMPTY.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<document id="EMPTY">
  <metadata><title>Empty</title><author>t</author><date>2026-04-22</date></metadata>
  <attributes>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
    </attribute>
  </attributes>
  <outline/>
</document>
""",
        encoding="utf-8",
    )

    svc = DocumentService(tmp_path, cache=True)
    svc.get_all_documents()
    w = DocumentEditorWindow("EMPTY", svc)
    w._set_read_only(False)
    qtbot.addWidget(w)

    # The grid's stacked widget should show the empty-state overlay, not the table.
    assert w.grid_pane._stack.currentWidget() is w.grid_pane._empty


def test_empty_state_button_triggers_new_top_level(qtbot, tmp_path: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    doc_root = tmp_path / "EMPTY"
    (doc_root / "objects").mkdir(parents=True)
    (doc_root / "EMPTY.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<document id="EMPTY">
  <metadata><title>Empty</title><author>t</author><date>2026-04-22</date></metadata>
  <attributes>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
    </attribute>
  </attributes>
  <outline/>
</document>
""",
        encoding="utf-8",
    )

    svc = DocumentService(tmp_path, cache=True)
    svc.get_all_documents()
    w = DocumentEditorWindow("EMPTY", svc)
    w._set_read_only(False)
    qtbot.addWidget(w)

    assert len(w.controller.document("EMPTY").objects) == 0
    w.grid_pane.create_first_object_requested.emit()
    assert len(w.controller.document("EMPTY").objects) == 1
    # Empty state is replaced by the table now.
    assert w.grid_pane._stack.currentWidget() is w.grid_pane.table
