"""Slice 2: write-through inline edits from the GUI.

Runs against a tmp copy of `req-test/` so mutations never dirty the committed
fixture. Tests drive the models + widgets directly rather than simulating
clicks — faster and scoped to the layer we actually changed.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from mellon import editor
from tests.store_support import (  # MELLON-STORE-SRS blind tests
    _svg_file,
    changed,
    git_commit_all,
    git_env,  # noqa: F401, F811 -- fixture
    git_init,
    gui,  # noqa: F401, F811 -- fixture
    links_xml,
    make_doc,
    snapshot,
    write,
)


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

    # Fresh copy so tests can freely mutate object files.
    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)

    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    w = DocumentEditorWindow(docs[0], svc)
    w._set_read_only(False)
    qtbot.addWidget(w)
    return w, dst


# --- grid cell edit --------------------------------------------------------


def test_grid_setdata_commits_enum_attribute(window):
    from PySide6.QtCore import Qt

    w, root = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    row = grid.row_for_obj_id("VCU3-SRS-2")
    status_col = grid.column_index("status")
    assert grid.setData(grid.index(row, status_col), "Approved", Qt.ItemDataRole.EditRole)

    # File on disk actually changed.
    obj_xml = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml").read_text(
        encoding="utf-8"
    )
    assert "<attr name=\"status\">Approved</attr>" in obj_xml

    # And the grid model now reflects the new value.
    idx = grid.index(row, status_col)
    assert grid.data(idx) == "Approved"


def test_grid_setdata_invalid_enum_is_rejected(window, qtbot):
    from PySide6.QtCore import Qt

    w, root = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model

    caught = []
    w.controller.edit_failed.connect(
        lambda d, o, f, msg: caught.append((d, o, f, msg))
    )
    row = grid.row_for_obj_id("VCU3-SRS-2")
    status_col = grid.column_index("status")
    ok = grid.setData(grid.index(row, status_col), "Nonsense", Qt.ItemDataRole.EditRole)
    assert ok is False
    assert caught and caught[0][2] == "status"
    # File is untouched — original was Draft.
    obj_xml = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml").read_text(
        encoding="utf-8"
    )
    assert "<attr name=\"status\">Draft</attr>" in obj_xml


def test_grid_setdata_edits_description(window):
    from PySide6.QtCore import Qt

    w, root = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    row = grid.row_for_obj_id("VCU3-SRS-2")
    desc_col = grid.column_index("heading_or_text")
    new_text = "Updated description from the grid."
    assert grid.setData(grid.index(row, desc_col), new_text, Qt.ItemDataRole.EditRole)

    assert new_text in (
        root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml"
    ).read_text(encoding="utf-8")


def test_grid_id_column_is_not_editable(window):
    from PySide6.QtCore import Qt

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    id_col = grid.column_index("id")
    flags = grid.flags(grid.index(0, id_col))
    assert not bool(flags & Qt.ItemFlag.ItemIsEditable)


# --- enum delegate ---------------------------------------------------------


def test_enum_delegate_populates_options(window):
    from mellon.gui.delegates import EnumDelegate

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    delegate = EnumDelegate(w.controller, "status")
    grid = w.grid_pane._model
    idx = grid.index(0, grid.column_index("status"))
    editor = delegate.createEditor(w.grid_pane.table, None, idx)
    items = [editor.itemText(i) for i in range(editor.count())]
    # Leading empty entry so an empty cell's editor doesn't preselect a value.
    assert items == ["", "Draft", "Reviewed", "Approved", "Deleted"]


def _record_writes(monkeypatch, svc) -> list[tuple]:
    """Record set_attribute/set_description calls, still passing them through."""
    calls: list[tuple] = []
    for name in ("set_attribute", "set_description"):
        real = getattr(svc, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append((_name, *args))
            return _real(*args, **kwargs)

        monkeypatch.setattr(svc, name, spy)
    return calls


def _open_cell_editor(w, obj_id: str, col_name: str):
    """Open the grid's cell editor the way a click/F2 does; return (editor, idx)."""
    w.controller.select_object("VCU3-SRS", obj_id)
    grid = w.grid_pane._model
    proxy = w.grid_pane._proxy
    idx = proxy.mapFromSource(
        grid.index(grid.row_for_obj_id(obj_id), grid.column_index(col_name))
    )
    table = w.grid_pane.table
    table.setCurrentIndex(idx)
    table.edit(idx)
    editor = table.indexWidget(idx)
    assert editor is not None
    return editor, idx


def _leave_cell(w, idx) -> None:
    """Move the current index away, which commits + closes the open editor."""
    table = w.grid_pane.table
    table.setCurrentIndex(table.model().index(idx.row(), 1))


def test_attr_editor_opened_and_left_writes_nothing(window, monkeypatch):
    w, root = window
    w.controller.select_document("VCU3-SRS")
    calls = _record_writes(monkeypatch, w.controller.require_service())
    obj_file = root / "VCU3-SRS" / "objects" / "VCU3-SRS-0.xml"
    before = obj_file.read_bytes()

    # Empty enum cells (a Heading has no priority etc.) and an unchanged one.
    for col in ("priority", "req-type", "verification-method", "type"):
        _editor, idx = _open_cell_editor(w, "VCU3-SRS-0", col)
        _leave_cell(w, idx)

    assert calls == []
    assert obj_file.read_bytes() == before


def test_enum_editor_keeps_value_outside_options(window, monkeypatch):
    w, root = window
    svc = w.controller.require_service()
    obj_file = root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml"
    # A hand edit / merge left a value the schema doesn't list.
    obj_file.write_text(
        obj_file.read_text(encoding="utf-8").replace(
            '<attr name="priority">Must</attr>', '<attr name="priority">Legacy</attr>'
        ),
        encoding="utf-8",
    )
    svc.cache.invalidate_all()
    w.controller.reload_document("VCU3-SRS")
    before = obj_file.read_bytes()
    calls = _record_writes(monkeypatch, svc)
    failures: list[tuple] = []
    w.controller.edit_failed.connect(lambda *args: failures.append(args))

    editor, idx = _open_cell_editor(w, "VCU3-SRS-2", "priority")
    assert editor.currentText() == "Legacy"
    _leave_cell(w, idx)

    assert calls == [] and failures == []
    assert obj_file.read_bytes() == before


def test_typed_attribute_editors_write_only_when_changed(window, monkeypatch, qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w, root = window
    svc = w.controller.require_service()
    svc.add_attribute("VCU3-SRS", "effort", "integer", False, ())
    svc.add_attribute("VCU3-SRS", "reviewed-on", "date", False, ())
    w.controller.select_document("VCU3-SRS")
    w.controller.reload_document("VCU3-SRS")
    calls = _record_writes(monkeypatch, svc)

    # Untouched: an empty cell opened and left stays empty.
    for col in ("effort", "reviewed-on"):
        _editor, idx = _open_cell_editor(w, "VCU3-SRS-2", col)
        _leave_cell(w, idx)
    assert calls == []

    # A typed value is written as typed.
    editor, idx = _open_cell_editor(w, "VCU3-SRS-2", "effort")
    editor.setText("5")
    _leave_cell(w, idx)
    assert calls == [("set_attribute", "VCU3-SRS", "VCU3-SRS-2", "effort", "5")]

    # Enter on an empty date cell writes nothing (no date of its own choosing).
    calls.clear()
    editor, idx = _open_cell_editor(w, "VCU3-SRS-2", "reviewed-on")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    # Qt closes the editor on Enter via a queued call.
    qtbot.waitUntil(lambda: w.grid_pane.table.indexWidget(idx) is None)
    assert calls == []


def test_grid_setdata_unchanged_value_is_a_noop(window, monkeypatch):
    from PySide6.QtCore import Qt

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    calls = _record_writes(monkeypatch, w.controller.require_service())
    grid = w.grid_pane._model
    row = grid.row_for_obj_id("VCU3-SRS-2")
    for col in ("status", "heading_or_text"):
        idx = grid.index(row, grid.column_index(col))
        assert grid.setData(idx, idx.data(Qt.ItemDataRole.EditRole))
    assert calls == []


# --- typed-but-uncommitted text survives actions ----------------------------


def test_action_commits_open_grid_editor_first(window):
    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-0")
    # Double-click on another row's description: the press moves the current
    # row (which re-homes the current index to column 0), then the editor
    # opens on the clicked cell — not on the current index.
    grid = w.grid_pane._model
    table = w.grid_pane.table
    idx = w.grid_pane._proxy.mapFromSource(
        grid.index(grid.row_for_obj_id("VCU3-SRS-2"), grid.column_index("heading_or_text"))
    )
    table.setCurrentIndex(idx)
    table.edit(idx)
    assert table.currentIndex() != idx
    table.indexWidget(idx).setPlainText("Typed in the grid, then Ctrl+N.")

    # QAction shortcuts don't move focus, so no focus-out commit fires.
    w._act_new_sibling.trigger()

    content = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml").read_text(
        encoding="utf-8"
    )
    assert "Typed in the grid, then Ctrl+N." in content


def test_reload_commits_pending_inspector_text(window):
    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    insp = w.inspector
    insp._description.setPlainText("Typed in the Inspector, then F5.")
    insp._attr_widgets["rationale"].setText("Because F5.")

    w._reload_act.trigger()

    content = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-10.xml").read_text(
        encoding="utf-8"
    )
    assert "Typed in the Inspector, then F5." in content
    assert "Because F5." in content


# --- inspector auto-commit -------------------------------------------------


def test_inspector_description_autocommits_on_focus_out(window):
    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    insp = w.inspector

    insp._description.setPlainText("MISRA rewrite for slice 2 test.")
    # Simulate focus-out of the description widget — the install eventFilter
    # converts that into a _commit_description call.
    insp._commit_description()

    content = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-10.xml").read_text(
        encoding="utf-8"
    )
    assert "MISRA rewrite for slice 2 test." in content


def test_inspector_combobox_autocommits_on_change(window):
    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    insp = w.inspector

    # Form is always editable when not read-only — no toggle required.
    from PySide6.QtWidgets import QComboBox
    status_widget = insp._attr_widgets["status"]
    assert isinstance(status_widget, QComboBox)
    # currentTextChanged fires synchronously, triggering _commit_attr.
    status_widget.setCurrentText("Reviewed")

    content = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml").read_text(
        encoding="utf-8"
    )
    assert "<attr name=\"status\">Reviewed</attr>" in content


def test_inspector_combo_popup_click_commits_without_destroying_combo(window, qtbot):
    """Picking an enum value from the Inspector popup with the mouse must not
    rebuild the form inside the combo's own signal — that deleted the combo
    mid-emit and crashed the process (access violation)."""
    import shiboken6
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w, root = window
    w.show()
    qtbot.waitExposed(w)
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    cb = w.inspector._attr_widgets["status"]
    cb.showPopup()
    view = cb.view()
    pos = view.visualRect(view.model().index(cb.findText("Approved"), 0)).center()
    QTest.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos
    )

    # Written synchronously, but the combo outlived its own signal.
    assert shiboken6.isValid(cb)
    content = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml").read_text(
        encoding="utf-8"
    )
    assert "<attr name=\"status\">Approved</attr>" in content
    # The form is rebuilt on the next event-loop pass.
    qtbot.waitUntil(lambda: w.inspector._attr_widgets["status"] is not cb)
    assert w.inspector._attr_widgets["status"].currentText() == "Approved"


def test_inspector_enum_combo_shows_empty_value_as_the_empty_entry(window):
    """An empty enum value must not show as the first option: that misstates
    the object, and picking that option would then change nothing. It shows
    as the empty entry, which comes first (MELLON-EDIT-SRS-130)."""
    w, root = window
    obj_file = root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml"
    text = obj_file.read_text(encoding="utf-8")
    assert '<attr name="status">Draft</attr>' in text
    obj_file.write_text(
        text.replace('<attr name="status">Draft</attr>', '<attr name="status"></attr>'),
        encoding="utf-8",
    )
    w.controller.reload_document("VCU3-SRS")
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    cb = w.inspector._attr_widgets["status"]
    assert cb.currentText() == ""
    assert cb.itemText(0) == ""
    first = cb.itemText(1)

    cb.setCurrentIndex(1)  # picking the first option is a real change

    assert f'<attr name="status">{first}</attr>' in obj_file.read_text(encoding="utf-8")


def test_inspector_stale_combo_shows_value_on_disk(window):
    """A combo pick refused by the "Changed on disk" banner must not stay
    shown as if it had been written."""
    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    obj_file = root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml"
    before = obj_file.read_bytes()
    w._set_stale(True)
    cb = w.inspector._attr_widgets["status"]

    cb.setCurrentText("Approved")

    assert obj_file.read_bytes() == before
    assert cb.currentText() == "Draft"
    assert "Reload first (F5)" in w._status.currentMessage()


def test_inspector_fields_are_locked_while_stale(window):
    """While "Changed on disk" is up, typed Inspector text could only be
    refused and then dropped by the reload, so nothing can be typed."""
    from PySide6.QtWidgets import QComboBox, QLineEdit

    w, _root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    insp = w.inspector
    widgets = insp._attr_widgets.values()
    assert not insp._description.isReadOnly()

    w._set_stale(True)

    assert insp._description.isReadOnly()
    assert all(x.isReadOnly() for x in widgets if isinstance(x, QLineEdit))
    assert not any(x.isEnabled() for x in widgets if isinstance(x, QComboBox))
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")  # a rebuilt form too
    widgets = insp._attr_widgets.values()
    assert insp._description.isReadOnly()
    assert not any(x.isEnabled() for x in widgets if isinstance(x, QComboBox))

    w._set_stale(False)

    assert not insp._description.isReadOnly()
    assert all(x.isEnabled() for x in widgets if isinstance(x, QComboBox))
    assert not any(x.isReadOnly() for x in widgets if isinstance(x, QLineEdit))


def test_inspector_failed_commit_emits_error_and_leaves_file_intact(window):
    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-2")
    insp = w.inspector

    caught = []
    w.controller.edit_failed.connect(
        lambda d, o, f, msg: caught.append((d, o, f, msg))
    )

    # Inject a QLineEdit holding a value the enum schema will reject, then
    # commit it directly. Mirrors what would happen if the user typed an
    # invalid enum value into a free-text widget.
    from PySide6.QtWidgets import QLineEdit
    bad = QLineEdit("Nonsense")
    insp._attr_widgets["status"] = bad
    insp._commit_attr("status", bad)

    assert caught and caught[0][2] == "status"
    # File untouched — original was Draft.
    obj_xml = (root / "VCU3-SRS" / "objects" / "VCU3-SRS-2.xml").read_text(
        encoding="utf-8"
    )
    assert "<attr name=\"status\">Draft</attr>" in obj_xml


def test_grid_description_multiline_roundtrip(window):
    from PySide6.QtCore import Qt
    from mellon.loader import load_document

    w, root = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    row = grid.row_for_obj_id("VCU3-SRS-2")
    desc_col = grid.column_index("heading_or_text")
    new_text = "Paragraph one line one.\nParagraph one line two.\n\nParagraph two."
    assert grid.setData(
        grid.index(row, desc_col), new_text, Qt.ItemDataRole.EditRole
    )

    doc = load_document(root / "VCU3-SRS")
    assert doc.objects["VCU3-SRS-2"].description == new_text

    # Grid display shows the full multi-paragraph text (no first-line truncation).
    assert grid.data(grid.index(row, desc_col)) == new_text


def test_grid_row_height_grows_with_line_count(window, qtbot):
    from PySide6.QtCore import Qt

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    table = w.grid_pane.table
    desc_col = grid.column_index("heading_or_text")

    row = grid.row_for_obj_id("VCU3-SRS-2")
    # Commit a single-line description, then a 5-line one, then compare.
    assert grid.setData(
        grid.index(row, desc_col), "One-liner.", Qt.ItemDataRole.EditRole
    )
    table.resizeRowsToContents()
    short_h = table.rowHeight(row)

    assert grid.setData(
        grid.index(row, desc_col), "L1\nL2\nL3\nL4\nL5", Qt.ItemDataRole.EditRole
    )
    table.resizeRowsToContents()
    tall_h = table.rowHeight(row)

    assert tall_h > short_h


def test_description_resize_refits_rows_once_after_drag(window, qtbot):
    """A header drag emits sectionResized per mouse move; rows are re-measured
    once the drag settles, not on every step (that hung large documents)."""
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    table = w.grid_pane.table
    desc_col = grid.column_index("heading_or_text")
    row = grid.row_for_obj_id("VCU3-SRS-1")
    before = table.rowHeight(row)

    header = table.horizontalHeader()
    for width in range(400, 60, -20):
        header.resizeSection(desc_col, width)

    assert table.rowHeight(row) == before
    qtbot.waitUntil(lambda: table.rowHeight(row) > before)


def test_grid_description_uses_multiline_editor(window):
    from mellon.gui.delegates import MultiLineTextDelegate

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    grid = w.grid_pane._model
    desc_col = grid.column_index("heading_or_text")
    delegate = w.grid_pane.table.itemDelegateForColumn(desc_col)
    assert isinstance(delegate, MultiLineTextDelegate)


def test_inspector_description_multiline_roundtrip(window):
    from mellon.loader import load_document

    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    insp = w.inspector

    multi = "First paragraph.\n\nSecond paragraph has\ntwo lines."
    insp._description.setPlainText(multi)
    insp._commit_description()

    doc = load_document(root / "VCU3-SRS")
    assert doc.objects["VCU3-SRS-10"].description == multi


def test_commit_blocked_by_validation_errors(window, monkeypatch):
    from mellon.gui import main_window as mw

    w, root = window
    w.controller.select_document("VCU3-SRS")
    # Break the doc: write an invalid status on an object directly to disk,
    # then re-load via the controller so validate() picks up the corrupt state.
    obj_path = root / "VCU3-SRS" / "objects" / "VCU3-SRS-0.xml"
    corrupted = obj_path.read_text(encoding="utf-8").replace(
        ">Draft<", ">Nonsense<"
    )
    obj_path.write_text(corrupted, encoding="utf-8")
    w.controller.reload_document("VCU3-SRS")

    critical_calls: list[tuple] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "critical",
        lambda *args, **kwargs: critical_calls.append(args)
        or mw.QMessageBox.StandardButton.Ok,
    )
    # If the commit dialog opens, validation didn't gate the flow — fail loudly.
    monkeypatch.setattr(
        mw,
        "CommitDialog",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("commit dialog should not open on invalid doc")
        ),
    )

    w._commit_act.trigger()

    assert len(critical_calls) == 1
    title = critical_calls[0][1]
    body = critical_calls[0][2]
    assert "validation failed" in title.lower()
    assert "VCU3-SRS-0" in body or "Nonsense" in body
    msg = w._status.currentMessage()
    assert "blocked" in msg
    assert "validation error" in msg


# --- document properties dialog --------------------------------------------


def test_properties_metadata_apply_writes_manifest(window):
    from mellon.gui.dialogs.document_properties import DocumentPropertiesDialog
    from mellon.loader import load_document

    w, root = window
    w.controller.select_document("VCU3-SRS")
    dlg = DocumentPropertiesDialog(w.controller, "VCU3-SRS", w)

    dlg._metadata_tab._title_edit.setText("New title")
    dlg._metadata_tab._author_edit.setText("New author")
    dlg._metadata_tab._on_apply()

    doc = load_document(root / "VCU3-SRS")
    assert doc.metadata.title == "New title"
    assert doc.metadata.author == "New author"


@pytest.mark.parametrize(
    "answer, closes, applied",
    [("Apply", True, True), ("Discard", True, False), ("Cancel", False, False)],
)
def test_properties_close_asks_about_unapplied_metadata(
    window, monkeypatch, answer, closes, applied
):
    from PySide6.QtWidgets import QMessageBox

    from mellon.gui.dialogs import document_properties as dp
    from mellon.loader import load_document

    w, root = window
    dlg = dp.DocumentPropertiesDialog(w.controller, "VCU3-SRS", w)
    asked = []
    monkeypatch.setattr(
        dp.QMessageBox,
        "question",
        lambda *args, **kwargs: asked.append(args)
        or getattr(QMessageBox.StandardButton, answer),
    )

    # Nothing typed: Close needs no question.
    dlg.show()
    dlg.reject()
    assert asked == [] and not dlg.isVisible()

    dlg.show()
    dlg._metadata_tab._title_edit.setText("Typed but not applied")
    dlg.reject()  # what Close, Esc and the title-bar X do

    assert len(asked) == 1
    assert dlg.isVisible() is not closes
    title = load_document(root / "VCU3-SRS").metadata.title
    assert (title == "Typed but not applied") is applied


def test_properties_parents_add_remove(window):
    from mellon.gui.dialogs.document_properties import DocumentPropertiesDialog
    from mellon.loader import load_document

    w, root = window
    # VCU3-SyRS has no parents in the committed fixture; add one then remove it.
    w.controller.select_document("VCU3-SyRS")
    dlg = DocumentPropertiesDialog(w.controller, "VCU3-SyRS", w)
    parents_tab = dlg._parents_tab

    # AddDialog uses QInputDialog interactively; simulate the user's choice by
    # calling the service directly, then exercise the tab's reload path.
    w.controller.require_service().add_parent("VCU3-SyRS", "VCU3-SRS")
    w.controller.reload_document("VCU3-SyRS")
    parents_tab._reload()

    assert load_document(root / "VCU3-SyRS").parents == ("VCU3-SRS",)
    items = [parents_tab._list.item(i).text() for i in range(parents_tab._list.count())]
    assert items == ["VCU3-SRS"]

    parents_tab._list.setCurrentRow(0)
    parents_tab._on_remove()

    assert load_document(root / "VCU3-SyRS").parents == ()


def test_properties_attribute_add_text(window):
    from mellon.gui.dialogs.document_properties import DocumentPropertiesDialog
    from mellon.loader import load_document

    w, root = window
    w.controller.select_document("VCU3-SRS")
    dlg = DocumentPropertiesDialog(w.controller, "VCU3-SRS", w)
    attr_tab = dlg._attributes_tab

    # Drive the underlying service directly; the AddAttributeDialog is a thin
    # form whose only job is to collect those four arguments.
    w.controller.require_service().add_attribute(
        "VCU3-SRS", "comment", "text", False, ()
    )
    w.controller.reload_document("VCU3-SRS")
    attr_tab._reload()

    doc = load_document(root / "VCU3-SRS")
    assert doc.attribute("comment") is not None
    names = [
        attr_tab._tree.topLevelItem(i).text(0)
        for i in range(attr_tab._tree.topLevelItemCount())
    ]
    assert "comment" in names


def test_properties_attribute_delete_strips_objects(window):
    from mellon.gui.dialogs.document_properties import DocumentPropertiesDialog
    from mellon.loader import load_document

    w, root = window
    w.controller.select_document("VCU3-SRS")
    # Add a fresh attribute, set it on one object, then delete it through the dialog
    # path. The cascade should strip the value from the object file.
    svc = w.controller.require_service()
    svc.add_attribute("VCU3-SRS", "scratch", "text", False, ())
    svc.set_attribute("VCU3-SRS", "VCU3-SRS-0", "scratch", "hello")
    w.controller.reload_document("VCU3-SRS")

    dlg = DocumentPropertiesDialog(w.controller, "VCU3-SRS", w)
    attr_tab = dlg._attributes_tab

    # Select the new attribute in the tree and invoke delete via the service
    # (bypassing the QMessageBox confirmation in tests).
    svc.delete_attribute("VCU3-SRS", "scratch")
    w.controller.reload_document("VCU3-SRS")
    attr_tab._reload()

    doc = load_document(root / "VCU3-SRS")
    assert doc.attribute("scratch") is None
    assert "scratch" not in doc.objects["VCU3-SRS-0"].attrs


# --- field edits update one row, with no model reset -----------------------


@pytest.fixture
def srs(window):
    """The editor on VCU3-SRS, shown small so the grid can scroll."""
    w, root = window
    w.controller.select_document("VCU3-SRS")
    w.resize(900, 300)
    w.show()
    return w, root


def _set(w, obj_id: str, column: str, value: str) -> None:
    grid = w.grid_pane._model
    index = grid.index(grid.row_for_obj_id(obj_id), grid.column_index(column))
    assert grid.setData(index, value)


def test_field_edit_keeps_model_scroll_and_selection(srs, qapp):
    w, _ = srs
    grid = w.grid_pane._model
    table = w.grid_pane.table
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-15")
    bar = table.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum() // 2)
    scrolled = bar.value()
    resets = []
    grid.modelReset.connect(lambda: resets.append(1))

    _set(w, "VCU3-SRS-15", "status", "Draft")
    _set(w, "VCU3-SRS-15", "heading_or_text", "Changed text.")
    qapp.processEvents()

    assert resets == []
    assert bar.value() == scrolled
    assert grid.obj_id_at(w.grid_pane._proxy.mapToSource(table.currentIndex()).row()) == "VCU3-SRS-15"
    row = grid.row_for_obj_id("VCU3-SRS-15")
    assert grid.data(grid.index(row, grid.column_index("heading_or_text"))) == "Changed text."
    assert grid.data(grid.index(row, grid.column_index("status"))) == "Draft"


def test_field_edit_reruns_no_suspect_scan(srs, monkeypatch):
    import mellon.service.document as service_module

    w, _ = srs
    w.controller.build_link_states("VCU3-SRS")
    scans = []
    real = service_module.scan_document
    monkeypatch.setattr(
        service_module, "scan_document", lambda *a: (scans.append(a), real(*a))[1]
    )

    _set(w, "VCU3-SRS-15", "status", "Draft")
    _set(w, "VCU3-SRS-15", "heading_or_text", "Changed text.")
    w.controller.build_link_states("VCU3-SRS")
    assert scans == []

    # The control: a real reload does re-scan.
    w.controller.reload_document("VCU3-SRS")
    assert scans


def test_field_edit_keeps_link_states_right(srs):
    """Link states are kept across a field edit, so they must still be correct."""
    w, _ = srs
    before = w.controller.object_worst_state("VCU3-SRS", "VCU3-SRS-15")
    assert before == "suspect"
    _set(w, "VCU3-SRS-15", "status", "Draft")
    assert w.controller.object_worst_state("VCU3-SRS", "VCU3-SRS-15") == before


def test_field_edit_refits_only_its_row(srs):
    w, _ = srs
    grid = w.grid_pane._model
    table = w.grid_pane.table
    row = grid.row_for_obj_id("VCU3-SRS-15")
    other = grid.row_for_obj_id("VCU3-SRS-16")
    tall, other_height = table.rowHeight(row), table.rowHeight(other)
    _set(w, "VCU3-SRS-15", "heading_or_text", "\n".join(["line"] * 12))
    assert table.rowHeight(row) > tall
    assert table.rowHeight(other) == other_height


def test_field_edit_marks_only_its_object_dirty(srs):
    from mellon.gui.models.objects_grid import OBJ_DIRTY_ROLE

    w, _ = srs
    grid = w.grid_pane._model
    dirty = lambda oid: grid.index(grid.row_for_obj_id(oid), 0).data(OBJ_DIRTY_ROLE)
    assert not dirty("VCU3-SRS-15")
    _set(w, "VCU3-SRS-15", "status", "Draft")
    assert dirty("VCU3-SRS-15")
    assert not dirty("VCU3-SRS-16")


def test_field_edit_updates_outline_label(srs):
    w, _ = srs
    outline = w.outline_pane._outline_model
    _set(w, "VCU3-SRS-15", "heading_or_text", "First line\nsecond line")
    assert outline._items["VCU3-SRS-15"].text() == "VCU3-SRS-15: First line"


def test_type_change_renumbers_chapters_like_a_rebuild(srs):
    """The first heading stops being one, so the later chapters move up. Then back."""
    w, _ = srs
    outline = w.outline_pane._outline_model
    labels = lambda: {oid: item.text() for oid, item in outline._items.items()}
    bold = lambda: {oid: item.font().bold() for oid, item in outline._items.items()}
    original = labels()
    assert original["VCU3-SRS-14"].startswith("2 ")
    assert outline._items["VCU3-SRS-0"].font().bold()

    _set(w, "VCU3-SRS-0", "type", "Requirement")
    assert labels()["VCU3-SRS-14"].startswith("1 ")
    assert not outline._items["VCU3-SRS-0"].font().bold()
    in_place, in_place_bold = labels(), bold()
    outline._rebuild("VCU3-SRS")
    assert labels() == in_place
    assert bold() == in_place_bold

    _set(w, "VCU3-SRS-0", "type", "Heading")
    assert labels() == original


def test_edit_that_stops_matching_the_saved_filter_hides_the_row(srs):
    from mellon.schema import Filter, FilterRule

    w, _ = srs
    grid = w.grid_pane._model
    proxy = w.grid_pane._proxy
    w.grid_pane.apply_filter(
        Filter(
            slug="drafts",
            name="Drafts",
            rules=(FilterRule(kind="attr", attr="status", op="eq", value="Draft"),),
            keep_ancestors=False,
        )
    )
    row = grid.row_for_obj_id("VCU3-SRS-2")
    assert proxy.mapFromSource(grid.index(row, 0)).isValid()
    shown = proxy.rowCount()
    _set(w, "VCU3-SRS-2", "status", "Approved")
    assert not proxy.mapFromSource(grid.index(row, 0)).isValid()
    assert proxy.rowCount() == shown - 1


def test_edit_that_stops_matching_the_text_filter_hides_the_row(srs):
    w, _ = srs
    grid = w.grid_pane._model
    proxy = w.grid_pane._proxy
    row = grid.row_for_obj_id("VCU3-SRS-2")
    w.grid_pane.filter_box.setText("VCU3-SRS-2")
    assert proxy.mapFromSource(grid.index(row, 0)).isValid()
    w.grid_pane.filter_box.setText("zzzunique")
    assert not proxy.mapFromSource(grid.index(row, 0)).isValid()
    _set(w, "VCU3-SRS-2", "heading_or_text", "now has zzzunique in it")
    assert proxy.mapFromSource(grid.index(row, 0)).isValid()


def test_grid_edit_shows_in_the_inspector(srs):
    w, _ = srs
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-15")
    _set(w, "VCU3-SRS-15", "heading_or_text", "From the grid.")
    assert w.inspector._description.toPlainText() == "From the grid."
    # Another object's edit leaves the shown one alone.
    _set(w, "VCU3-SRS-16", "heading_or_text", "Elsewhere.")
    assert w.inspector._description.toPlainText() == "From the grid."


def test_inspector_edit_shows_in_the_grid(srs):
    w, _ = srs
    grid = w.grid_pane._model
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-15")
    resets = []
    grid.modelReset.connect(lambda: resets.append(1))
    w.inspector._description.setPlainText("From the inspector.")
    w.inspector._commit_description()
    row = grid.row_for_obj_id("VCU3-SRS-15")
    assert grid.data(grid.index(row, grid.column_index("heading_or_text"))) == "From the inspector."
    assert resets == []


def test_field_edit_can_be_undone_and_redone(srs):
    w, _ = srs
    grid = w.grid_pane._model
    index = grid.index(grid.row_for_obj_id("VCU3-SRS-15"), grid.column_index("heading_or_text"))
    original = grid.data(index)
    _set(w, "VCU3-SRS-15", "heading_or_text", "Changed.")
    w._undo_act.trigger()
    index = grid.index(grid.row_for_obj_id("VCU3-SRS-15"), grid.column_index("heading_or_text"))
    assert grid.data(index) == original
    w._redo_act.trigger()
    index = grid.index(grid.row_for_obj_id("VCU3-SRS-15"), grid.column_index("heading_or_text"))
    assert grid.data(index) == "Changed."


def test_field_edit_does_not_flag_the_window_stale(srs):
    w, _ = srs
    _set(w, "VCU3-SRS-15", "status", "Draft")
    assert not w.controller.is_doc_stale()


def test_reset_reuses_measured_text_heights(srs, monkeypatch):
    """A reset only lays out text it hasn't measured, and new text is measured."""
    import mellon.gui.delegates as delegates

    w, _ = srs
    table = w.grid_pane.table
    laid_out = []
    real = delegates.QTextDocument
    monkeypatch.setattr(
        delegates, "QTextDocument", lambda *a: (laid_out.append(1), real(*a))[1]
    )
    w.controller.reload_document("VCU3-SRS")
    assert laid_out == []

    grid = w.grid_pane._model
    row = grid.row_for_obj_id("VCU3-SRS-15")
    before = table.rowHeight(row)
    w.controller.service.set_description("VCU3-SRS", "VCU3-SRS-15", "\n".join(["line"] * 12))
    w.controller.reload_document("VCU3-SRS")
    assert laid_out
    assert table.rowHeight(row) > before


# MELLON-STORE-SRS blind tests


def test_store_srs_174_gui_delete_with_failing_manifest_write_restores_all(gui, tmp_path, git_env, monkeypatch):
    """MELLON-STORE-SRS-174: if a file can't be written during a GUI operation
    that edits a document, Mellon shall return each file the operation already
    wrote or deleted to its state before. Force-deleting DOC-1, linked from
    CHILD, with the manifest write failing, shows an error and leaves every file
    of the repository (working copy and .git) with its bytes."""
    root = git_init(git_env, tmp_path / "del")
    make_doc(root, "DOC", n=2)
    write(root / "DOC" / "figures" / "DOC-1.svg", _svg_file(tmp_path).read_bytes())
    write(root / "DOC" / "links" / "DOC-1.xml", links_xml("DOC-1", [("DOC", "DOC-2", "refines")]))
    make_doc(root, "CHILD", n=1, parents=("DOC",))
    write(root / "CHILD" / "links" / "CHILD-1.xml", links_xml("CHILD-1", [("DOC", "DOC-1", "satisfies")]))
    git_commit_all(git_env, root, "seed")
    ed = gui.editor(root, "DOC")
    gui.set_read_only(ed, False)
    gui.select(ed, "DOC-1")
    before = snapshot(root)
    manifest = (root / "DOC" / "DOC.xml").resolve()
    real_replace = os.replace
    fired = []

    def replace(src, dst, *a, **k):
        if Path(dst).resolve() == manifest:
            fired.append(dst)
            raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    gui.messages.clear()
    gui.trigger(ed, "Delete")
    monkeypatch.setattr(os, "replace", real_replace)
    assert fired, f"test error: the manifest write wasn't reached: {gui.messages} {gui.status(ed)}"
    assert changed(before, snapshot(root)) == []
    shown = " ".join(t for _, _, t in gui.messages) + " " + gui.status(ed)
    assert "fail" in shown.lower() or "error" in shown.lower(), f"no error shown: {shown!r}"


def test_store_srs_174_gui_delete_attribute_with_failing_second_write_restores_all(
    gui, tmp_path, git_env, monkeypatch
):
    """MELLON-STORE-SRS-174: deleting an attribute set on objects, in the
    Properties dialog, with the operation's second file write failing, shows an
    error and leaves every file of the repository (working copy and .git) with
    its bytes from before."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTreeWidget

    root = git_init(git_env, tmp_path / "attr")
    folder = make_doc(root, "DOC", n=3)
    for obj_id in ("DOC-1", "DOC-2", "DOC-3"):
        editor.set_attribute(folder, obj_id, "rationale", f"Because {obj_id}")
    git_commit_all(git_env, root, "seed")
    ed = gui.editor(root, "DOC")
    gui.set_read_only(ed, False)
    before = snapshot(root)
    real_replace = os.replace
    writes = []

    def replace(src, dst, *a, **k):
        if folder.resolve() in Path(dst).resolve().parents:
            writes.append(Path(dst).name)
            if len(writes) == 2:
                raise OSError(errno.ENOSPC, "No space left on device (injected)")
        return real_replace(src, dst, *a, **k)

    shown_in_dialog: list[str] = []

    def run_dialog(dialog):
        tree = dialog.findChildren(QTreeWidget)[0]
        (item,) = tree.findItems("rationale", Qt.MatchFlag.MatchExactly, 0)
        tree.setCurrentItem(item)
        gui.process()
        (delete,) = [b for b in dialog.findChildren(QPushButton) if b.text() == "Delete attribute"]
        monkeypatch.setattr(os, "replace", replace)
        delete.click()
        gui.process()
        monkeypatch.setattr(os, "replace", real_replace)
        shown_in_dialog.extend(lbl.text() for lbl in dialog.findChildren(QLabel) if lbl.text())
        dialog.reject()
        return 0

    monkeypatch.setattr(QDialog, "exec", run_dialog)
    gui.trigger(ed, "Properties")
    assert len(writes) >= 2, f"test error: the second write wasn't reached: {writes}"
    assert changed(before, snapshot(root)) == []
    shown = " ".join(shown_in_dialog + [t for _, _, t in gui.messages] + [gui.status(ed)])
    assert "fail" in shown.lower() or "error" in shown.lower(), f"no error shown: {shown!r}"


# MELLON-EDIT-SRS blind tests (1-175)
#
# Written blind from MELLON-EDIT-SRS and the public signatures (docs/specification.md,
# step 7). Helpers are in tests/edit_support.py, which builds on tests/store_support.py.

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QListWidget, QMessageBox, QTreeWidget  # noqa: E402

from mellon.loader import load_document  # noqa: E402
from tests.edit_support import (  # noqa: E402
    BOOL,
    KIND_CASES,
    add_attribute_handler,
    assert_link_kept,
    attr_tree_item,
    basic_repo,
    button,
    child_parent_repo,
    choose_in_combo,
    combo_items,
    commit_all,  # noqa: F401, F811 -- fixture
    current_id,
    delete_repo,
    edit_repo,
    field_for_label,
    grid_delete_key,
    grid_enter,
    grid_headers,
    grid_index,
    inspector_enter,
    inspector_fields,
    kind_window,
    labelled_repo,
    long_repo,
    manifest_attr,
    manifest_options,
    middle_row,
    modals,  # noqa: F401, F811 -- fixture
    obj_file,
    open_cell_editor,
    open_editor_window,
    open_properties,
    outline_delete,
    props_attributes,
    read_obj,
    required_control,
    select,
    type_into,
    us_locale,  # noqa: F401, F811 -- fixture
    widget_text,
    write_doc,
)
from tests.store_support import changed, git_env, mcp_at, snapshot  # noqa: E402,F401,F811


# --- Metadata, parents and attributes in the Properties dialog --------------------------


@pytest.mark.parametrize("field,label,value", [
    ("date", "Date", "2026-11-11"),
    ("title", "Title", "New title"),
    ("author", "Author", "A. Person"),
])
def test_edit_srs_35_properties_dialog_edits_metadata(qtbot, tmp_path, modals, commit_all, field, label, value):
    """MELLON-EDIT-SRS-35: the Properties dialog edits the title, author and date; typing a
    value and applying writes it to the manifest."""
    from mellon.gui.dialogs.document_properties import MetadataTab
    root = basic_repo(tmp_path)
    commit_all(root)
    w = open_editor_window(qtbot, root, "SRS")
    d = open_properties(qtbot, w, "SRS")
    tab = d.findChild(MetadataTab)
    type_into(qtbot, field_for_label(tab, label), value)
    button(tab, "Apply").click()
    qtbot.wait(50)
    assert getattr(load_document(root / "SRS").metadata, field) == value


def test_edit_srs_43_properties_dialog_adds_parent_document(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-43: the Properties dialog's parents tab adds a parent document; the
    manifest declares it and the tab lists it."""
    from mellon.gui.dialogs.document_properties import ParentsTab
    root = tmp_path
    write_doc(root, "SYS", objects={"SYS-0": ({"type": "Requirement"}, "S")}, outline=["SYS-0"])
    write_doc(root, "SRS", objects={"SRS-0": ({"type": "Requirement"}, "R")}, outline=["SRS-0"])
    commit_all(root)
    w = open_editor_window(qtbot, root, "SRS")
    d = open_properties(qtbot, w, "SRS")
    tab = d.findChild(ParentsTab)
    modals.inputs = [("SYS", True)]
    button(tab, "Add").click()
    qtbot.wait(50)
    assert load_document(root / "SRS").parents == ("SYS",)
    lw = tab.findChild(QListWidget)
    assert [lw.item(i).text() for i in range(lw.count())] == ["SYS"]


def test_edit_srs_51_properties_dialog_remove_parent_keeps_links(qtbot, tmp_path, modals, mcp_at, commit_all):
    """MELLON-EDIT-SRS-51 through the Properties dialog's Remove: the link stays,
    byte-identical."""
    from mellon.gui.dialogs.document_properties import ParentsTab
    root = child_parent_repo(tmp_path, commit_all)
    before = (root / "CHILD" / "links" / "CHILD-0.xml").read_bytes()
    w = open_editor_window(qtbot, root, "CHILD")
    d = open_properties(qtbot, w, "CHILD")
    tab = d.findChild(ParentsTab)
    lw = tab.findChild(QListWidget)
    lw.setCurrentRow([lw.item(i).text() for i in range(lw.count())].index("PARENT"))
    modals.question = QMessageBox.StandardButton.Yes
    modals.box_button = "accept"
    button(tab, "Remove").click()
    qtbot.wait(50)
    assert_link_kept(root, before, mcp_at)


def test_edit_srs_59_properties_dialog_defines_attribute(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-59: the Properties dialog's add form defines `prio`, an enumeration
    with the options High and Low; the manifest defines it and the attributes tab lists it."""
    from mellon.gui.dialogs.document_properties import AddAttributeDialog
    root = basic_repo(tmp_path)
    commit_all(root)
    _, _, tab, tree = props_attributes(qtbot, root)
    modals.handlers[AddAttributeDialog] = add_attribute_handler(modals, "prio", "enum", ("High", "Low"))
    button(tab, "Add attribute").click()
    qtbot.wait(50)
    a = manifest_attr(root, "SRS", "prio")
    assert a is not None, "the manifest doesn't define prio"
    assert a.get("kind") == "enum"
    assert manifest_options(root, "SRS", "prio") == ("High", "Low")
    attr_tree_item(tab.findChild(QTreeWidget), "prio")


def test_edit_srs_73_grid_headers_use_labels(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-73: the grid names each attribute by its label: the manifest's label
    (Requirement Type), else Type for `type`, else the name (rationale)."""
    root = labelled_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    headers = grid_headers(w)
    assert "Requirement Type" in headers
    assert "Type" in headers
    assert "rationale" in headers
    assert "req-type" not in headers and "type" not in headers


@pytest.mark.parametrize("editable", [True, False], ids=["editable", "read-only"])
def test_edit_srs_73_inspector_uses_labels(qtbot, tmp_path, modals, commit_all, editable):
    """MELLON-EDIT-SRS-73: the Inspector names each attribute by its label."""
    root = labelled_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS", editable=editable)
    select(qtbot, w, "SRS-0")
    names = set(inspector_fields(w))
    assert {"Requirement Type", "Type", "rationale"} <= names, names
    assert "req-type" not in names and "type" not in names


def test_edit_srs_77_properties_dialog_adds_option(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-77: the Properties dialog adds the option Rejected to status, and the
    manifest lists it last."""
    root = basic_repo(tmp_path)
    commit_all(root)
    _, _, tab, tree = props_attributes(qtbot, root)
    tree.setCurrentItem(attr_tree_item(tree, "status"))
    modals.inputs = [("Rejected", True)]
    button(tab, "Add option").click()
    qtbot.wait(50)
    assert manifest_options(root, "SRS", "status") == ("Draft", "Approved", "Rejected")


def test_edit_srs_82_properties_dialog_removes_option(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-82: the Properties dialog removes the option Draft from status; the
    manifest no longer lists it and SRS-1, which had it, keeps status with an empty value."""
    root = basic_repo(tmp_path)
    commit_all(root)
    _, _, tab, tree = props_attributes(qtbot, root)
    status = attr_tree_item(tree, "status")
    status.setExpanded(True)
    draft = [status.child(i) for i in range(status.childCount())
             if "Draft" in [status.child(i).text(c) for c in range(tree.columnCount())]]
    assert draft, "the dialog doesn't list the option Draft under status"
    tree.setCurrentItem(draft[0])
    modals.question = QMessageBox.StandardButton.Yes
    modals.box_button = "accept"
    button(tab, "Remove option").click()
    qtbot.wait(50)
    assert manifest_options(root, "SRS", "status") == ("Approved",)
    attrs, _ = read_obj(root, "SRS", "SRS-1")
    assert attrs.get("status") == "", f"SRS-1 attributes after removal: {attrs}"


def test_edit_srs_92_properties_dialog_changes_required(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-92: the Properties dialog makes the optional status required, then
    optional again; the manifest follows each change."""
    root = basic_repo(tmp_path)
    commit_all(root)
    _, _, tab, tree = props_attributes(qtbot, root)
    item = attr_tree_item(tree, "status")
    tree.setCurrentItem(item)
    modals.question = QMessageBox.StandardButton.Yes
    modals.box_button = "accept"
    toggle = required_control(tab, tree, item)
    assert toggle is not None, "no control in the Properties dialog changes whether status is required"
    toggle()
    qtbot.wait(50)
    assert manifest_attr(root, "SRS", "status").get("required") == "true"
    toggle = required_control(tab, tree, attr_tree_item(tree, "status"))
    toggle()
    qtbot.wait(50)
    assert manifest_attr(root, "SRS", "status").get("required") in ("false", None)


def test_edit_srs_97_properties_dialog_deletes_attribute(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-97: the Properties dialog's delete control, confirmed, deletes
    rationale: the manifest no longer defines it and no object holds it."""
    root = tmp_path
    write_doc(root, "SRS", objects={"SRS-0": ({"type": "Heading"}, "H"),
                                    "SRS-1": ({"type": "Requirement", "rationale": "why"}, "R")},
              outline=[("SRS-0", ["SRS-1"])], next_number=2)
    commit_all(root)
    _, _, tab, tree = props_attributes(qtbot, root)
    tree.setCurrentItem(attr_tree_item(tree, "rationale"))
    modals.question = QMessageBox.StandardButton.Yes
    modals.box_button = "accept"
    button(tab, "Delete attribute").click()
    qtbot.wait(50)
    assert manifest_attr(root, "SRS", "rationale") is None
    for oid in ("SRS-0", "SRS-1"):
        assert "rationale" not in read_obj(root, "SRS", oid)[0]


# --- The grid and the Inspector ----------------------------------------------------------


@pytest.mark.parametrize("editable", [True, False], ids=["editable", "read-only"])
def test_edit_srs_124_inspector_shows_values_and_description(qtbot, tmp_path, modals, commit_all, editable):
    """MELLON-EDIT-SRS-124: selecting an object with type Requirement, status Draft and a
    rationale shows each attribute with its value, and the description, in the Inspector."""
    root = tmp_path
    write_doc(root, "SRS", objects={
        "SRS-0": ({"type": "Heading"}, "H"),
        "SRS-1": ({"type": "Requirement", "status": "Draft", "rationale": "Asked for by the plant"},
                  "The VCU shall stop."),
    }, outline=[("SRS-0", ["SRS-1"])], next_number=2)
    commit_all(root)
    w = open_editor_window(qtbot, root, "SRS", editable=editable)
    select(qtbot, w, "SRS-1")
    got = {k: widget_text(v) for k, v in inspector_fields(w).items()}
    assert got.get("Type") == "Requirement", got
    assert got.get("status") == "Draft", got
    assert got.get("rationale") == "Asked for by the plant", got
    assert got.get("Description") == "The VCU shall stop.", got


def test_edit_srs_126_grid_description_editor_keeps_line_break(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-126: a line break typed with the keyboard (Enter) in the grid's
    description editor is kept in the saved description."""
    root = edit_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    ed = open_cell_editor(qtbot, w, "SRS-1", "Description")
    type_into(qtbot, ed, "first")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    QTest.keyClicks(ed, "second")
    QTest.keyClick(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(50)
    assert read_obj(root, "SRS", "SRS-1")[1] == "first\nsecond"


def test_edit_srs_126_inspector_description_keeps_line_break(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-126: a line break typed with Enter in the Inspector's description is
    kept in the saved description."""
    root = edit_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    select(qtbot, w, "SRS-1")
    ed = inspector_fields(w)["Description"]
    type_into(qtbot, ed, "first")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    QTest.keyClicks(ed, "second")
    w.inspector.commit_pending()
    qtbot.wait(50)
    assert read_obj(root, "SRS", "SRS-1")[1] == "first\nsecond"


def test_edit_srs_127_escape_cancels_description_edit(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-127: typing in the grid's description editor and pressing Escape
    leaves the object file byte-identical and the grid showing the old description."""
    root = edit_repo(tmp_path, commit_all)
    path = obj_file(root, "SRS", "SRS-1")
    before = path.read_bytes()
    w = open_editor_window(qtbot, root, "SRS")
    ed = open_cell_editor(qtbot, w, "SRS-1", "Description")
    type_into(qtbot, ed, "Typed by mistake")
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    qtbot.wait(50)
    w.flush_pending_edits()
    qtbot.wait(30)
    assert path.read_bytes() == before
    assert grid_index(w, "SRS-1", "Description").data() == "Old text"


def test_edit_srs_130_grid_enum_editor_offers_options_and_empty_entry(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-130: the grid's enumeration editor offers exactly an empty entry and
    each option, and choosing the empty entry stores an empty value."""
    root = edit_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    combo = open_cell_editor(qtbot, w, "SRS-1", "status")
    assert isinstance(combo, QComboBox)
    assert sorted(combo_items(combo)) == ["", "Approved", "Draft"]
    choose_in_combo(qtbot, combo, "")
    w.grid_pane.commit_open_editor()
    qtbot.wait(30)
    assert read_obj(root, "SRS", "SRS-1")[0].get("status", "") == ""


def test_edit_srs_130_inspector_enum_field_offers_empty_entry(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-130: the Inspector's enumeration field on an object with a value
    offers an empty entry and each option; choosing the empty entry stores an empty value."""
    root = edit_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    select(qtbot, w, "SRS-1")
    combo = inspector_fields(w)["status"]
    assert isinstance(combo, QComboBox), type(combo).__name__
    assert sorted(combo_items(combo)) == ["", "Approved", "Draft"]
    choose_in_combo(qtbot, combo, "")
    w.inspector.commit_pending()
    qtbot.wait(30)
    assert read_obj(root, "SRS", "SRS-1")[0].get("status", "") == ""


def test_edit_srs_131_grid_boolean_editor_offers_true_false_empty(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-131: the grid's boolean editor offers exactly an empty entry, true
    and false."""
    root = edit_repo(tmp_path, commit_all, BOOL, {"flag": "true"})
    w = open_editor_window(qtbot, root, "SRS")
    combo = open_cell_editor(qtbot, w, "SRS-1", "flag")
    assert isinstance(combo, QComboBox), type(combo).__name__
    assert sorted(combo_items(combo)) == ["", "false", "true"]


def test_edit_srs_131_inspector_boolean_field_offers_true_false_empty(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-131: the Inspector's boolean field offers exactly an empty entry,
    true and false."""
    root = edit_repo(tmp_path, commit_all, BOOL, {"flag": "true"})
    w = open_editor_window(qtbot, root, "SRS")
    select(qtbot, w, "SRS-1")
    field = inspector_fields(w)["flag"]
    assert isinstance(field, QComboBox), f"the Inspector's boolean field is a {type(field).__name__}"
    assert sorted(combo_items(field)) == ["", "false", "true"]


@pytest.mark.parametrize("kind,text", [("integer", "42"), ("integer", "-7"),
                                       ("real", "3.14"), ("real", "-2")])
def test_edit_srs_132_133_grid_number_editor_stores_fitting_value(qtbot, tmp_path, modals, us_locale, commit_all,
                                                                  kind, text):
    """MELLON-EDIT-SRS-132 and -133: a fitting integer (42, -7) or real (3.14, -2) typed in
    the grid's editor is stored, as the same number."""
    w, name = kind_window(qtbot, tmp_path, commit_all, kind)
    grid_enter(qtbot, w, name, text)
    stored = read_obj(tmp_path, "SRS", "SRS-1")[0].get(name)
    assert KIND_CASES[kind][2](stored), f"stored {stored!r} doesn't fit {kind}"
    assert float(stored) == float(text)


@pytest.mark.parametrize("kind,text", [("integer", "3.14"), ("integer", "abc"),
                                       ("real", "abc"), ("real", "1.2.3"),
                                       ("date", "0000-12-31"), ("date", "10000-01-01")])
def test_edit_srs_132_133_134_grid_editor_stores_no_misfit(qtbot, tmp_path, modals, us_locale, commit_all, kind, text):
    """MELLON-EDIT-SRS-132, -133 and -134: a value that doesn't fit the kind, typed in the
    grid's editor, can't be entered: what is stored afterwards is empty or fits the kind,
    and isn't the typed text."""
    w, name = kind_window(qtbot, tmp_path, commit_all, kind)
    grid_enter(qtbot, w, name, text)
    stored = read_obj(tmp_path, "SRS", "SRS-1")[0].get(name, "")
    assert stored != text
    assert stored == "" or KIND_CASES[kind][2](stored), f"stored {stored!r} doesn't fit {kind}"


@pytest.mark.parametrize("kind", ["integer", "real", "date"])
def test_edit_srs_132_133_134_grid_editor_accepts_empty_value(qtbot, tmp_path, modals, us_locale, commit_all, kind):
    """MELLON-EDIT-SRS-132, -133 and -134: the grid's integer, real and date editors accept
    an empty value: clearing the text and leaving the cell stores an empty value."""
    w, name = kind_window(qtbot, tmp_path, commit_all, kind)
    grid_enter(qtbot, w, name, "")
    assert read_obj(tmp_path, "SRS", "SRS-1")[0].get(name, "") == ""


@pytest.mark.parametrize("text", [
    "0001-01-01",
    "9999-12-31",
])
def test_edit_srs_134_grid_date_editor_stores_range_ends(qtbot, tmp_path, modals, us_locale, commit_all, text):
    """MELLON-EDIT-SRS-134: the grid's date editor accepts the first and last day of the
    range, 0001-01-01 and 9999-12-31, and stores the typed date."""
    w, name = kind_window(qtbot, tmp_path, commit_all, "date")
    grid_enter(qtbot, w, name, text)
    assert read_obj(tmp_path, "SRS", "SRS-1")[0].get(name) == text


@pytest.mark.parametrize("kind,text", [("integer", "42"), ("integer", "-7"), ("integer", ""),
                                       ("real", "3.14"), ("real", "-2"), ("real", ""),
                                       ("date", "0001-01-01"), ("date", "9999-12-31"), ("date", "")])
def test_edit_srs_132_133_134_inspector_stores_fitting_value(qtbot, tmp_path, modals, us_locale, commit_all,
                                                             kind, text):
    """MELLON-EDIT-SRS-132, -133 and -134: in the Inspector, an empty value or a value that
    fits the kind is accepted and stored."""
    w, name = kind_window(qtbot, tmp_path, commit_all, kind)
    inspector_enter(qtbot, w, name, text)
    stored = read_obj(tmp_path, "SRS", "SRS-1")[0].get(name, "")
    if text == "":
        assert stored == ""
    else:
        assert KIND_CASES[kind][2](stored), f"stored {stored!r}"
        assert stored == text if kind == "date" else float(stored) == float(text)


@pytest.mark.parametrize("kind,text", [("integer", "3.14"), ("integer", "abc"),
                                       ("real", "abc"), ("real", "1.2.3"),
                                       ("date", "0000-12-31"), ("date", "10000-01-01")])
def test_edit_srs_132_133_134_inspector_refuses_misfit(qtbot, tmp_path, modals, us_locale, commit_all, kind, text):
    """MELLON-EDIT-SRS-132, -133 and -134: in the Inspector, a value that doesn't fit the
    kind can't be entered or is refused, and the object file keeps its bytes."""
    w, name = kind_window(qtbot, tmp_path, commit_all, kind)
    path = obj_file(tmp_path, "SRS", "SRS-1")
    before = path.read_bytes()
    inspector_enter(qtbot, w, name, text)
    stored = read_obj(tmp_path, "SRS", "SRS-1")[0].get(name, "")
    assert stored == "" or KIND_CASES[kind][2](stored), f"stored {stored!r} doesn't fit {kind}"
    assert path.read_bytes() == before


def test_edit_srs_135_grid_enum_choice_saved_at_once(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-135: choosing Approved from the grid's enumeration list writes it to
    the object file at once, while the cell's editor is still open."""
    root = edit_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    combo = open_cell_editor(qtbot, w, "SRS-1", "status")
    choose_in_combo(qtbot, combo, "Approved")
    assert read_obj(root, "SRS", "SRS-1")[0].get("status") == "Approved"


def test_edit_srs_135_inspector_enum_choice_saved_at_once(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-135: choosing Approved from the Inspector's enumeration list writes
    it to the object file at once."""
    root = edit_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    select(qtbot, w, "SRS-1")
    choose_in_combo(qtbot, inspector_fields(w)["status"], "Approved")
    assert read_obj(root, "SRS", "SRS-1")[0].get("status") == "Approved"


@pytest.mark.parametrize("what", ["description", "attribute", "table"])
def test_edit_srs_137_edit_keeps_scroll_and_selection(qtbot, tmp_path, modals, commit_all, what):
    """MELLON-EDIT-SRS-137: with the grid scrolled halfway down a long document, editing the
    selected object's description, an attribute value or a table cell keeps the grid's
    scroll position and the selection."""
    root = long_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "L")
    t = w.grid_pane.table
    obj = "L-80" if what == "table" else None
    if obj:
        t.verticalScrollBar().setValue(t.verticalScrollBar().maximum() // 2)
        qtbot.wait(30)
        t.scrollTo(grid_index(w, obj, "ID"))
        qtbot.wait(30)
    else:
        obj = middle_row(qtbot, w)
        assert obj != "L-80", "precondition: the middle row has no table"
    select(qtbot, w, obj)
    bar = t.verticalScrollBar()
    pos = bar.value()
    assert pos > 0
    if what == "description":
        ed = open_cell_editor(qtbot, w, obj, "Description")
        ed.setFocus()
        QTest.keyClick(ed, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(ed, "x")
        QTest.keyClick(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    elif what == "attribute":
        combo = open_cell_editor(qtbot, w, obj, "status")
        choose_in_combo(qtbot, combo, "Approved")
        w.grid_pane.commit_open_editor()
    else:
        from mellon.gui.widgets.inline_table import InlineTableWidget
        w.grid_pane.begin_table_edit(obj)
        qtbot.wait(50)
        tw = [x for x in w.findChildren(InlineTableWidget) if x.isVisible()]
        assert tw, "no table editor opened"
        grid = tw[0].table
        grid.setCurrentCell(0, 0)
        grid.editItem(grid.item(0, 0))
        qtbot.wait(20)
        cell = QApplication.focusWidget()
        type_into(qtbot, cell, "z")
        QTest.keyClick(cell, Qt.Key.Key_Tab)
        w.grid_pane.commit_open_editor()
    qtbot.wait(100)
    attrs, desc = read_obj(root, "L", obj)
    if what == "description":
        assert desc.endswith("x"), "precondition: the description edit was saved"
    elif what == "attribute":
        assert attrs.get("status") == "Approved", "precondition: the attribute edit was saved"
    else:
        tbl = load_document(root / "L").objects[obj].table
        assert tbl.rows[0].cells[0].text == "z", "precondition: the table edit was saved"
    assert current_id(w) == obj, "the selection moved"
    assert bar.value() == pos, f"the scroll position moved from {pos} to {bar.value()}"


# --- Deleting objects --------------------------------------------------------------------


@pytest.mark.parametrize("path", ["outline-pane", "grid-key"])
def test_edit_srs_160_delete_asks_and_declining_changes_nothing(qtbot, tmp_path, modals, commit_all, path):
    """MELLON-EDIT-SRS-160: the outline pane's Delete and the grid's Delete key each ask the
    user to confirm before deleting; at the question no file has changed, and declining
    leaves every file unchanged."""
    root = delete_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    select(qtbot, w, "SRS-2")
    before = snapshot(root)
    at_ask = []
    modals.on_call = lambda kind: at_ask.append(changed(before, snapshot(root)))
    modals.question = QMessageBox.StandardButton.No
    modals.box_button = "cancel"
    (outline_delete(w) if path == "outline-pane" else grid_delete_key(w))()
    qtbot.wait(50)
    assert at_ask, "no confirmation was asked"
    assert at_ask[0] == [], f"files changed before the question: {at_ask[0]}"
    assert changed(before, snapshot(root)) == []


@pytest.mark.parametrize("path", ["outline-pane", "grid-key"])
def test_edit_srs_160_delete_confirmed_deletes(qtbot, tmp_path, modals, commit_all, path):
    """MELLON-EDIT-SRS-160 (other side): confirming the question deletes the object."""
    root = delete_repo(tmp_path, commit_all)
    w = open_editor_window(qtbot, root, "SRS")
    select(qtbot, w, "SRS-2")
    modals.question = QMessageBox.StandardButton.Yes
    modals.box_button = "accept"
    (outline_delete(w) if path == "outline-pane" else grid_delete_key(w))()
    qtbot.wait(50)
    assert modals.calls, "no confirmation was asked"
    assert not obj_file(root, "SRS", "SRS-2").exists()


# MELLON-GUI-SRS blind tests
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QDialog,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
)

from tests.gui_support import (  # noqa: E402
    SRS,
    _add_table,
    _description,
    _edit_attribute,
    _edit_description,
    _external_edit,
    _obj_file,
    _open_menu,
    _shown,
    gui_gui,  # noqa: F401, F811 -- fixture
    repo,  # noqa: F401, F811 -- fixture
)


def _table_cell_editor(gui_gui, ed, oid, row=1, col=0):
    """Open the table editor on `oid` and then the editor of one of its cells."""
    from PySide6.QtWidgets import QTableWidget

    w = gui_gui.open_cell_editor(ed, oid, "Description")
    tw = w if isinstance(w, QTableWidget) else (w.findChild(QTableWidget) if w is not None else None)
    assert tw is not None, f"no table editor opened, focus is {type(w).__name__}"
    rect = tw.visualItemRect(tw.item(row, col))
    QTest.mouseClick(tw.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    gui_gui.pause()
    QTest.mouseDClick(tw.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    gui_gui.app.processEvents()
    cell = QApplication.focusWidget()
    assert isinstance(cell, (QLineEdit, QTextEdit, QPlainTextEdit)), f"no table-cell editor, focus is {type(cell)}"
    return tw, cell


def _editor_closed(gui_gui, ed) -> bool:
    return gui_gui.table(ed).state() != QAbstractItemView.State.EditingState


def _save_table_editor(gui_gui, ed, cell):
    """Save the table with Ctrl+Enter. A first Ctrl+Enter may only end the cell edit (the defect that
    test_gui_srs_165_ctrl_enter_saves_table_cell_editor records); then a second one saves the table."""
    QTest.keyClick(cell, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    gui_gui.app.processEvents()
    if gui_gui.table(ed).state() == QAbstractItemView.State.EditingState:
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        gui_gui.app.processEvents()


def _edit_table(gui_gui, ed, oid, text):
    _, cell = _table_cell_editor(gui_gui, ed, oid)
    QTest.keyClick(cell, Qt.Key.Key_End)
    QTest.keyClicks(cell, text)
    _save_table_editor(gui_gui, ed, cell)


# What is edited, and the object it is edited on: the description cell of an object with a table opens
# the table editor, so the description is edited on an object without one.
_EDITS = {"attribute": (_edit_attribute, "VCU3-SRS-16"), "description": (_edit_description, "VCU3-SRS-15"),
          "table": (_edit_table, "VCU3-SRS-16")}


@pytest.mark.parametrize("what", list(_EDITS))
def test_gui_srs_57_edit_keeps_object_selected(gui_gui, repo, what):
    """MELLON-GUI-SRS-57: editing an attribute value, the description or the table keeps the object selected."""
    _add_table(repo, "VCU3-SRS-16")
    edit, oid = _EDITS[what]
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = _obj_file(repo, SRS, oid).read_bytes()
    edit(gui_gui, ed, oid, "-kept")
    assert _obj_file(repo, SRS, oid).read_bytes() != before, "the edit wasn't written"
    assert ed.controller.current_obj_id() == oid
    assert gui_gui.selected_grid_ids(ed) == [oid]


def _top_row_id(gui_gui, ed) -> str:
    tv = gui_gui.table(ed)
    m = tv.model()
    return m.data(m.index(tv.rowAt(0), gui_gui.col(ed, "ID")))


def _scroll_to_top(gui_gui, ed, oid):
    tv = gui_gui.table(ed)
    m = tv.model()
    tv.scrollTo(m.index(gui_gui.row(ed, oid), gui_gui.col(ed, "ID")), QAbstractItemView.ScrollHint.PositionAtTop)
    gui_gui.app.processEvents()
    assert _top_row_id(gui_gui, ed) == oid, "precondition: the row is at the top"


def test_gui_srs_58_table_edit_keeps_first_visible_row(gui_gui, repo):
    """MELLON-GUI-SRS-58: editing a table keeps the same object in the grid's first visible row."""
    _add_table(repo, "VCU3-SRS-16")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    gui_gui.select(ed, "VCU3-SRS-16")
    _scroll_to_top(gui_gui, ed, "VCU3-SRS-15")
    tw, cell = _table_cell_editor(gui_gui, ed, "VCU3-SRS-16")
    _scroll_to_top(gui_gui, ed, "VCU3-SRS-15")
    QTest.keyClicks(cell, "-top")
    _save_table_editor(gui_gui, ed, cell)
    assert "-top" in _obj_file(repo, SRS, "VCU3-SRS-16").read_text(encoding="utf-8"), "the table edit wasn't written"
    assert _top_row_id(gui_gui, ed) == "VCU3-SRS-15"


def test_gui_srs_58_taller_description_above_keeps_first_visible_row(gui_gui, repo):
    """MELLON-GUI-SRS-58: editing the description of an object above the first visible row, so that its
    row height changes, keeps the same object in the first visible row."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    gui_gui.select(ed, "VCU3-SRS-14")
    _scroll_to_top(gui_gui, ed, "VCU3-SRS-15")
    desc = gui_gui.insp_description(ed)
    desc.setFocus()
    QTest.keyClick(desc, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    for line in ("one", "two", "three", "four"):
        QTest.keyClick(desc, Qt.Key.Key_Return)
        QTest.keyClicks(desc, line)
    gui_gui.table(ed).setFocus()  # the field loses the focus and saves
    gui_gui.app.processEvents()
    assert "four" in _description(repo, SRS, "VCU3-SRS-14"), "the description edit wasn't written"
    assert _top_row_id(gui_gui, ed) == "VCU3-SRS-15"


# --------------------------------------------------------------------------- pending edits


def _type_into(widget, text):
    widget.setFocus()
    QTest.keyClick(widget, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier if isinstance(
        widget, (QTextEdit, QPlainTextEdit)) else Qt.KeyboardModifier.NoModifier)
    QTest.keyClicks(widget, text)


def test_gui_srs_71_inspector_description_saved_on_focus_out(gui_gui, repo):
    """MELLON-GUI-SRS-71: typed text in the Inspector's description is saved when the field loses the focus."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    gui_gui.select(ed, "VCU3-SRS-2")
    _type_into(gui_gui.insp_description(ed), " Focus-out.")
    assert "Focus-out." not in _description(repo, SRS, "VCU3-SRS-2"), "precondition: not saved while typing"
    gui_gui.click_cell(ed, "VCU3-SRS-2", "Status")
    assert _description(repo, SRS, "VCU3-SRS-2").endswith("20 Hz. Focus-out.")


def test_gui_srs_71_inspector_text_attribute_saved_on_focus_out(gui_gui, repo):
    """MELLON-GUI-SRS-71: typed text in an Inspector text attribute is saved when the field loses the focus."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    gui_gui.select(ed, "VCU3-SRS-1")
    _type_into(gui_gui.insp_field(ed, "Rationale"), " More.")
    gui_gui.click_cell(ed, "VCU3-SRS-1", "Status")
    assert "SSA-014. More.</attr>" in _obj_file(repo, SRS, "VCU3-SRS-1").read_text(encoding="utf-8")


def test_gui_srs_71_grid_cell_editor_saved_on_focus_out(gui_gui, repo):
    """MELLON-GUI-SRS-71: typed text in a grid cell editor is saved when it loses the focus to the Inspector."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    editor = gui_gui.open_cell_editor(ed, "VCU3-SRS-2", "Rationale")
    QTest.keyClicks(editor, "focus-out-cell")
    desc = gui_gui.insp_description(ed)
    QTest.mouseClick(desc.viewport(), Qt.MouseButton.LeftButton)
    gui_gui.app.processEvents()
    assert '<attr name="rationale">focus-out-cell</attr>' in _obj_file(repo, SRS, "VCU3-SRS-2").read_text(
        encoding="utf-8")


def _pending_in_inspector(gui_gui, ed):
    gui_gui.select(ed, "VCU3-SRS-2")
    _type_into(gui_gui.insp_description(ed), " Pending74.")


def _pending_in_cell(gui_gui, ed):
    editor = gui_gui.open_cell_editor(ed, "VCU3-SRS-2", "Rationale")
    QTest.keyClicks(editor, "Pending74.")


def _saved74(repo) -> bool:
    return "Pending74." in _obj_file(repo, SRS, "VCU3-SRS-2").read_text(encoding="utf-8")


# Each action: (how to run it, the dialog it opens or None). A dialog handler records whether the
# text was on disk when the dialog opened, which is after the action started.
_ACTIONS_74 = {
    "commit": ("Commit", "CommitDialog"),
    "delete": ("Delete", "question"),
    "add_link": ("Link", "LinkPicker"),
    "indent": ("Indent", None),
    "move_up": ("Move up", None),
    "properties": ("Properties", "DocumentPropertiesDialog"),
    "trace": ("Trace", None),
    "validate": ("Validate", None),
    "discard": ("Discard uncommitted changes to this document", "question"),
    "read_only": ("Read-only", None),
    "compare": ("Compare", None),
}


@pytest.mark.parametrize("field", ["inspector", "grid_cell"])
@pytest.mark.parametrize("action", list(_ACTIONS_74))
def test_gui_srs_74_pending_edit_saved_before_action(gui_gui, repo, action, field):
    """MELLON-GUI-SRS-74: before the Document Editor runs an action, it saves its pending edits."""
    _external_edit(_obj_file(repo, SRS, "VCU3-SRS-11"), "3 degC", "4 degC")  # an uncommitted change
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    (_pending_in_inspector if field == "inspector" else _pending_in_cell)(gui_gui, ed)
    assert not _saved74(repo), "precondition: the typed text is pending"
    at_dialog = []

    def record(d=None, *a):
        at_dialog.append(_saved74(repo))
        return int(QDialog.DialogCode.Rejected) if not isinstance(d, (str, type(None))) else QMessageBox.StandardButton.No

    text, dialog = _ACTIONS_74[action]
    for name in ("CommitDialog", "LinkPicker", "DocumentPropertiesDialog", "TraceView", "QMessageBox"):
        gui_gui.handlers[name] = record
    gui_gui.answers["question"] = lambda *a: (at_dialog.append(_saved74(repo)), QMessageBox.StandardButton.No)[1]
    a = gui_gui.action(ed, text)
    if action in ("discard", "read_only", "compare"):
        _open_menu(gui_gui, ed)  # these are picked from the Menu
    assert a.isEnabled(), f"{text} is disabled"
    a.trigger()
    gui_gui.app.processEvents()
    if dialog is not None:
        assert at_dialog, f"{dialog} didn't open; dialogs: {[e[:2] for e in gui_gui.log]}"
        assert at_dialog[0], f"the typed text wasn't on disk when {text} ran"
    assert _saved74(repo), f"the typed text wasn't written by the time {text} ran"


def _open_description_editor(gui_gui, ed, oid):
    editor = gui_gui.open_cell_editor(ed, oid, "Description")
    assert isinstance(editor, (QTextEdit, QPlainTextEdit)), f"no description editor, focus is {type(editor)}"
    QTest.keyClick(editor, Qt.Key.Key_End, Qt.KeyboardModifier.ControlModifier)
    return editor


def test_gui_srs_165_ctrl_enter_saves_description_editor(gui_gui, repo):
    """MELLON-GUI-SRS-165: Ctrl+Enter in the description editor saves the text, line break included, and closes it."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    editor = _open_description_editor(gui_gui, ed, "VCU3-SRS-2")
    QTest.keyClicks(editor, " First.")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QTest.keyClicks(editor, "Second.")
    assert "Second." not in _description(repo, SRS, "VCU3-SRS-2"), "precondition: Enter didn't save"
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    gui_gui.app.processEvents()
    assert _description(repo, SRS, "VCU3-SRS-2").endswith("20 Hz. First.\nSecond.")
    assert _editor_closed(gui_gui, ed)


def test_gui_srs_165_ctrl_enter_saves_table_cell_editor(gui_gui, repo):
    """MELLON-GUI-SRS-165: Ctrl+Enter in a table-cell editor saves the text and closes the editor."""
    _add_table(repo, "VCU3-SRS-16")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    tw, cell = _table_cell_editor(gui_gui, ed, "VCU3-SRS-16")
    QTest.keyClick(cell, Qt.Key.Key_End)
    QTest.keyClicks(cell, "-ctrl-enter")
    QTest.keyClick(cell, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    gui_gui.app.processEvents()
    assert "a-ctrl-enter" in _obj_file(repo, SRS, "VCU3-SRS-16").read_text(encoding="utf-8"), "the text wasn't saved"
    assert not _shown(tw) or tw.state() != QAbstractItemView.State.EditingState, "the table-cell editor stayed open"


def test_gui_srs_166_esc_cancels_description_editor(gui_gui, repo):
    """MELLON-GUI-SRS-166: Esc in the description editor closes it without saving the typed text, also later."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = _obj_file(repo, SRS, "VCU3-SRS-2").read_bytes()
    editor = _open_description_editor(gui_gui, ed, "VCU3-SRS-2")
    QTest.keyClicks(editor, " Cancelled.")
    QTest.keyClick(editor, Qt.Key.Key_Escape)
    gui_gui.app.processEvents()
    assert _editor_closed(gui_gui, ed)
    gui_gui.click_cell(ed, "VCU3-SRS-11")
    gui_gui.action(ed, "Validate").trigger()
    gui_gui.app.processEvents()
    assert _obj_file(repo, SRS, "VCU3-SRS-2").read_bytes() == before


def test_gui_srs_166_esc_cancels_table_cell_editor(gui_gui, repo):
    """MELLON-GUI-SRS-166: Esc in a table-cell editor closes the editor without saving the typed text, also later."""
    _add_table(repo, "VCU3-SRS-16")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = _obj_file(repo, SRS, "VCU3-SRS-16").read_bytes()
    tw, cell = _table_cell_editor(gui_gui, ed, "VCU3-SRS-16")
    QTest.keyClicks(cell, "-cancelled")
    QTest.keyClick(cell, Qt.Key.Key_Escape)
    gui_gui.app.processEvents()
    assert tw.state() != QAbstractItemView.State.EditingState, "the table-cell editor stayed open"
    QTest.keyClick(QApplication.focusWidget() or ed, Qt.Key.Key_Escape)  # Esc again leaves the table editor
    gui_gui.app.processEvents()
    gui_gui.click_cell(ed, "VCU3-SRS-11")
    gui_gui.action(ed, "Validate").trigger()
    gui_gui.app.processEvents()
    assert _obj_file(repo, SRS, "VCU3-SRS-16").read_bytes() == before
