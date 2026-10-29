"""Slice 2: write-through inline edits from the GUI.

Runs against a tmp copy of `req-test/` so mutations never dirty the committed
fixture. Tests drive the models + widgets directly rather than simulating
clicks — faster and scoped to the layer we actually changed.
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


def test_spin_and_date_editors_write_only_when_touched(window, monkeypatch, qtbot):
    from PySide6.QtCore import QDate, Qt
    from PySide6.QtTest import QTest

    w, root = window
    svc = w.controller.require_service()
    svc.add_attribute("VCU3-SRS", "effort", "integer", False, ())
    svc.add_attribute("VCU3-SRS", "reviewed-on", "date", False, ())
    w.controller.select_document("VCU3-SRS")
    w.controller.reload_document("VCU3-SRS")
    calls = _record_writes(monkeypatch, svc)

    # Untouched: the 0 / today shown for an empty cell is not written.
    for col in ("effort", "reviewed-on"):
        _editor, idx = _open_cell_editor(w, "VCU3-SRS-2", col)
        _leave_cell(w, idx)
    assert calls == []

    # A changed value is written.
    editor, idx = _open_cell_editor(w, "VCU3-SRS-2", "effort")
    editor.setValue(5)
    _leave_cell(w, idx)
    assert calls == [("set_attribute", "VCU3-SRS", "VCU3-SRS-2", "effort", "5")]

    # Enter confirms the shown date even though it wasn't changed.
    calls.clear()
    editor, _idx = _open_cell_editor(w, "VCU3-SRS-2", "reviewed-on")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: bool(calls))  # Qt commits on Enter via a queued call
    today = QDate.currentDate().toString("yyyy-MM-dd")
    assert calls == [("set_attribute", "VCU3-SRS", "VCU3-SRS-2", "reviewed-on", today)]


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


def test_inspector_enum_combo_shows_empty_value_as_no_selection(window):
    """An empty enum value must not show as the first option: that misstates
    the object, and picking that option would then change nothing."""
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
    assert cb.currentIndex() == -1
    assert cb.currentText() == ""
    first = cb.itemText(0)

    cb.setCurrentIndex(0)  # picking the first option is now a real change

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
