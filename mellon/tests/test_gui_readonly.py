"""Read-only mode: defaults, banner toggle, edit gating, mid-edit cancel.

Mirrors the test_gui_edits / test_gui_outline fixture pattern: tmp copy of
`req-test/`, drive the window through its public attributes and the same
slot/signal entry points the GUI uses.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent


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
    qtbot.addWidget(w)
    return w, dst


# --- defaults --------------------------------------------------------------


def test_window_opens_read_only_by_default(window):
    w, _ = window
    assert w._read_only is True
    assert not w.grid_pane.read_only_banner.isHidden()
    assert w._read_only_act.isChecked()


def test_mutation_actions_disabled_when_read_only(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")  # nested under -0
    for action in (
        w._act_new_sibling, w._act_new_child,
        w._act_indent, w._act_outdent,
        w._act_move_up, w._act_move_down,
        w._act_delete, w._act_add_link,
        w._props_act,
    ):
        assert not action.isEnabled(), f"{action.text()} should be disabled"
    # Read-safe actions stay enabled.
    assert w._act_trace.isEnabled()


def test_grid_cells_not_editable_when_read_only(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    model = w.grid_pane._model
    desc_col = model.column_index("heading_or_text")
    idx = model.index(0, desc_col)
    assert not (model.flags(idx) & Qt.ItemFlag.ItemIsEditable)


def test_inspector_attrs_render_as_labels_when_read_only(window):
    from PySide6.QtWidgets import QLabel

    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    insp = w.inspector
    # In read-only mode the form holds plain QLabels, not editable widgets,
    # so there is no way to mutate from the inspector.
    form = insp._attrs_form
    for row in range(form.rowCount()):
        item = form.itemAt(row, form.ItemRole.FieldRole)
        assert item is not None
        assert isinstance(item.widget(), QLabel)
    # Description is read-only too.
    assert insp._description.isReadOnly()


# --- toggle off ------------------------------------------------------------


def test_disable_button_clears_read_only(window):
    from PySide6.QtWidgets import QLabel

    w, _ = window
    w.grid_pane.read_only_banner.disable_clicked.emit()
    assert w._read_only is False
    assert w.grid_pane.read_only_banner.isHidden()
    assert not w._read_only_act.isChecked()
    # Selection-dependent actions become enabled given a selection.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    assert w._act_new_sibling.isEnabled()
    assert w._act_new_child.isEnabled()
    # Inspector form now holds editable widgets instead of plain labels.
    insp = w.inspector
    fields = [
        insp._attrs_form.itemAt(r, insp._attrs_form.ItemRole.FieldRole).widget()
        for r in range(insp._attrs_form.rowCount())
    ]
    assert fields, "expected at least one attribute on the selected object"
    assert not all(isinstance(f, QLabel) for f in fields)
    assert not insp._description.isReadOnly()


def test_view_menu_action_toggles_read_only(window):
    w, _ = window
    # Off via menu.
    w._read_only_act.setChecked(False)
    assert w._read_only is False
    assert w.grid_pane.read_only_banner.isHidden()
    # Back on.
    w._read_only_act.setChecked(True)
    assert w._read_only is True
    assert not w.grid_pane.read_only_banner.isHidden()


def test_grid_cells_editable_after_toggling_off(window):
    w, _ = window
    w._set_read_only(False)
    w.controller.select_document("VCU3-SRS")
    model = w.grid_pane._model
    desc_col = model.column_index("heading_or_text")
    idx = model.index(0, desc_col)
    assert model.flags(idx) & Qt.ItemFlag.ItemIsEditable


# --- defense-in-depth slot guards -----------------------------------------


def test_tab_in_grid_does_not_indent_when_read_only(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    parent_before = w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-10")
    ev = QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier
    )
    w.grid_pane.table.keyPressEvent(ev)
    assert (
        w.controller.outline_parent_id("VCU3-SRS", "VCU3-SRS-10") == parent_before
    )


def test_new_sibling_slot_no_op_when_read_only(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    before = set(w.controller.document("VCU3-SRS").objects.keys())
    w._new_sibling()
    after = set(w.controller.document("VCU3-SRS").objects.keys())
    assert before == after


# --- inspector mid-edit cancellation --------------------------------------


def test_enabling_read_only_drops_pending_edits(window):
    from PySide6.QtWidgets import QLabel, QLineEdit

    w, root = window
    w._set_read_only(False)
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    insp = w.inspector

    # Type into a free-text attr without firing editingFinished, then flip
    # read-only on. The form rebuild must drop the typed-but-uncommitted text.
    widget = next(
        wid
        for wid in insp._attr_widgets.values()
        if isinstance(wid, QLineEdit)
    )
    original = widget.text()
    widget.setText(original + " UNSAVED")
    file_before = (
        root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    ).read_text(encoding="utf-8")

    w._set_read_only(True)

    # Form rebuilt as labels; pending text discarded; file untouched.
    field = insp._attrs_form.itemAt(
        0, insp._attrs_form.ItemRole.FieldRole
    ).widget()
    assert isinstance(field, QLabel)
    assert insp._description.isReadOnly()
    file_after = (
        root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    ).read_text(encoding="utf-8")
    assert file_before == file_after


def test_grid_setdata_refused_when_read_only(window):
    w, root = window
    obj_file = root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    before = obj_file.read_bytes()
    model = w.grid_pane._model
    idx = model.index(
        model.row_for_obj_id("VCU3-SRS-1"), model.column_index("heading_or_text")
    )

    assert model.setData(idx, "written while read-only", Qt.ItemDataRole.EditRole) is False
    assert obj_file.read_bytes() == before


def test_enabling_read_only_closes_open_grid_editor(window):
    from PySide6.QtWidgets import QTableView

    w, root = window
    w._set_read_only(False)
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    obj_file = root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    before = obj_file.read_bytes()
    model = w.grid_pane._model
    table = w.grid_pane.table
    idx = w.grid_pane._proxy.mapFromSource(
        model.index(model.row_for_obj_id("VCU3-SRS-1"), model.column_index("heading_or_text"))
    )
    table.setCurrentIndex(idx)
    table.edit(idx)
    table.indexWidget(idx).setPlainText("typed, then read-only")

    w._set_read_only(True)

    # Like the Inspector, the uncommitted text is dropped, not written.
    assert table.state() != QTableView.State.EditingState
    assert table.indexWidget(idx) is None
    table.setCurrentIndex(table.model().index(idx.row(), 1))
    assert obj_file.read_bytes() == before


# --- post-commit auto re-lock ---------------------------------------------


def test_successful_commit_returns_window_to_read_only(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from mellon.gui import main_window as mw

    w, _ = window
    w._set_read_only(False)
    w.controller.select_document("VCU3-SRS")
    # Dirty the document so the commit pipeline has something to stage.
    w.controller.require_service().set_attribute(
        "VCU3-SRS", "VCU3-SRS-2", "status", "Approved"
    )

    # Auto-accept the commit dialog without showing it.
    class _FakeDialog:
        def __init__(self, preview, parent=None):
            self._preview = preview

        def exec(self):
            return QDialog.DialogCode.Accepted

        def message(self):
            return self._preview.default_message

        def issue(self):
            return self._preview.current_issue

        def author(self):
            return self._preview.current_author

        def date(self):
            return self._preview.current_date

        def tag(self):
            return ""

        def tag_message(self):
            return ""

        def tag_only(self):
            return False

    monkeypatch.setattr(mw, "CommitDialog", _FakeDialog)

    w._commit_current_doc()

    assert w._read_only is True
    assert not w.grid_pane.read_only_banner.isHidden()
    assert w._read_only_act.isChecked()


def test_failed_commit_keeps_read_only_state(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from mellon.gui import main_window as mw

    w, _ = window
    w._set_read_only(False)
    w.controller.select_document("VCU3-SRS")
    w.controller.require_service().set_attribute(
        "VCU3-SRS", "VCU3-SRS-2", "status", "Approved"
    )

    class _FakeDialog:
        def __init__(self, preview, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

        def message(self):
            return ""

    monkeypatch.setattr(mw, "CommitDialog", _FakeDialog)

    w._commit_current_doc()

    # User cancelled — read-only stays as the user left it (off).
    assert w._read_only is False


# --- banner theming -------------------------------------------------------


def test_read_only_banner_uses_theme_blue(window):
    from mellon.gui.theme import TOKENS, ThemeManager

    w, _ = window
    ThemeManager.instance().apply("dark")
    assert TOKENS["dark"]["blue"] in w.grid_pane.read_only_banner.styleSheet()
    ThemeManager.instance().apply("light")
    assert TOKENS["light"]["blue"] in w.grid_pane.read_only_banner.styleSheet()
