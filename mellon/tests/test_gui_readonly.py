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


# MELLON-GUI-SRS blind tests
from PySide6.QtCore import QMimeData, QPointF, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolButton,
)

from tests.gui_support import (  # noqa: E402
    SRS,
    SYRS,
    _ALT,
    _CTRL,
    _SHIFT,
    _add_link_by_key,
    _add_table,
    _answer_yes,
    _attach_figure_by_key,
    _change_elsewhere,
    _choose_filter,
    _choose_view,
    _commit_accepting,
    _drag_column,
    _edit_attribute,
    _edit_properties,
    _git_commit_all,
    _make_filter,
    _obj_file,
    _open_menu,
    _paste_in_cell_editor,
    _plain,
    _press_in_grid,
    _rationale,
    _reactivated,
    _read_only_banner_shown,
    _refs,
    _shown,
    _snapshot,
    _snapshot_diff,
    _stale_shown,
    _svg_file,
    _uncommitted_change,
    _write,
    gui_gui,  # noqa: F401, F811 -- fixture
    repo,  # noqa: F401, F811 -- fixture
)


# --------------------------------------------------------------------------- read-only mode


def test_gui_srs_89_commit_disabled_in_read_only_mode(gui_gui, repo):
    """MELLON-GUI-SRS-89: while in read-only mode the Document Editor disables Commit: the action and its toolbar
    button are disabled, Ctrl+S and Ctrl+K open no commit dialog, and no commit or tag is made. With read-only mode
    off, Commit is enabled."""
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    assert gui_gui.is_read_only(ed), "precondition: a Document Editor opens in read-only mode"
    refs = _refs(repo)
    _commit_accepting(gui_gui)
    assert not gui_gui.action(ed, "Commit").isEnabled()
    assert not gui_gui.toolbar_button(ed, "Commit").isEnabled()
    gui_gui.select(ed, "VCU3-SRS-2")
    for key in (Qt.Key.Key_S, Qt.Key.Key_K):
        gui_gui.key(ed, key, Qt.KeyboardModifier.ControlModifier)
    assert not gui_gui.opened("CommitDialog"), "a commit dialog opened in read-only mode"
    assert _refs(repo) == refs
    gui_gui.action(ed, "Read-only").trigger()
    assert not gui_gui.is_read_only(ed), "precondition: read-only mode is off"
    assert gui_gui.action(ed, "Commit").isEnabled()
    assert gui_gui.toolbar_button(ed, "Commit").isEnabled()


def _close_compare_dialogs(gui_gui):
    gui_gui.bot.wait(300)
    for w in list(QApplication.topLevelWidgets()):
        if type(w).__name__ == "CompareDialog" and _shown(w):
            w.close()
    gui_gui.app.processEvents()


@pytest.mark.parametrize("mode", ["read_only", "editing"])
def test_gui_srs_86_only_read_only_and_enable_editing_change_mode(gui_gui, repo, mode):
    """MELLON-GUI-SRS-86: the Document Editor leaves read-only mode only when the user chooses Read-only or Enable
    editing: reload, a theme change, Compare, Validate, Trace, selecting, a filter, a view and a change on disk
    leave it as it was, on or off."""
    from mellon.service.document import DocumentService

    DocumentService(repo).create_view(SRS, "Pair", ["priority", "status"])
    _make_filter(repo, "Nothing", "no-object-has-this-rationale")
    _git_commit_all(repo, "Add a view and a filter")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=(mode == "editing"))
    want = mode == "read_only"

    def f5():
        gui_gui.table(ed).setFocus()
        gui_gui.key(ed, Qt.Key.Key_F5)

    steps = {
        "selecting objects": lambda: (gui_gui.select(ed, "VCU3-SRS-2"), gui_gui.select(ed, "VCU3-SRS-11")),
        "Validate": lambda: gui_gui.action(ed, "Validate").trigger(),
        "Trace": lambda: gui_gui.action(ed, "Trace").trigger(),
        "a theme change": lambda: (gui_gui.action(ed, "Light").trigger(), gui_gui.action(ed, "Dark").trigger()),
        "choosing a filter": lambda: _choose_filter(gui_gui, ed, "Nothing"),
        "choosing a view": lambda: _choose_view(gui_gui, ed, "Pair"),
        "Compare": lambda: (_open_menu(gui_gui, ed), gui_gui.action(ed, "Compare").trigger(), _close_compare_dialogs(gui_gui)),
        "Reload from disk": f5,
        "a change on disk and activation": lambda: (_change_elsewhere(_obj_file(repo, SRS, "VCU3-SRS-2")),
                                                    _reactivated(gui_gui, ed, pe)),
        "Reload after the change": f5,
    }
    changed = []
    for name, step in steps.items():
        step()
        gui_gui.app.processEvents()
        if gui_gui.is_read_only(ed) != want or _read_only_banner_shown(ed) != want:
            changed.append(name)
    assert changed == [], f"read-only mode changed after: {changed}"


@pytest.mark.parametrize("mode", ["read_only", "editing"])
@pytest.mark.parametrize("how", ["f5", "banner_reload"])
def test_gui_srs_87_reload_keeps_read_only_mode(gui_gui, repo, mode, how):
    """MELLON-GUI-SRS-87: when the Document Editor reloads the document, by F5 or the banner's Reload, it keeps
    read-only mode on or off as before, with the read-only banner shown only while it is on."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=(mode == "editing"))
    _change_elsewhere(_obj_file(repo, SRS, "VCU3-SRS-2"))
    _reactivated(gui_gui, ed, pe)
    assert _stale_shown(ed), "precondition: the Changed on disk banner shows"
    if how == "f5":
        gui_gui.table(ed).setFocus()
        gui_gui.key(ed, Qt.Key.Key_F5)
    else:
        btn = [b for b in ed.grid_pane.stale_banner.findChildren(QPushButton) if _plain(b.text()) == "Reload"]
        assert btn, "no Reload button on the Changed on disk banner"
        gui_gui.bot.mouseClick(btn[0], Qt.MouseButton.LeftButton)
        gui_gui.app.processEvents()
    assert not _stale_shown(ed), "precondition: the document reloaded"
    assert gui_gui.is_read_only(ed) == (mode == "read_only")
    assert _read_only_banner_shown(ed) == (mode == "read_only")


def _paste_in_grid(gui_gui, ed, oid="VCU3-SRS-2", text="pasted-text"):
    """Select the row, move to its Description cell with the arrow key, and paste."""
    QApplication.clipboard().setText(text)
    gui_gui.select(ed, oid)
    gui_gui.table(ed).setFocus()
    gui_gui.key(ed, Qt.Key.Key_Right)
    gui_gui.key(ed, Qt.Key.Key_V, _CTRL)


def _paste_in_inspector(gui_gui, ed, oid="VCU3-SRS-2", text="pasted-text"):
    QApplication.clipboard().setText(text)
    gui_gui.select(ed, oid)
    w = gui_gui.insp_description(ed)
    w.setFocus()
    QTest.keyClick(w, Qt.Key.Key_End, _CTRL)
    QTest.keyClick(w, Qt.Key.Key_V, _CTRL)
    gui_gui.click_cell(ed, "VCU3-SRS-11")  # the field loses the focus


def _drop(view, mime, pos):
    """Drag `mime` over `view` and drop it at `pos`, as a drag and drop from the user does."""
    vp = view.viewport()
    actions = Qt.DropAction.MoveAction | Qt.DropAction.CopyAction
    for ev in (QDragEnterEvent(pos, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
               QDragMoveEvent(pos, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier),
               QDropEvent(QPointF(pos), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)):
        QApplication.sendEvent(vp, ev)
    QApplication.processEvents()


def _drag_grid_row(gui_gui, ed):
    tv = gui_gui.table(ed)
    m = tv.model()
    mime = m.mimeData([m.index(gui_gui.row(ed, "VCU3-SRS-2"), gui_gui.col(ed, "ID"))])
    _drop(tv, mime, gui_gui.cell_rect(ed, "VCU3-SRS-14", "ID").center())


def _drag_outline_row(gui_gui, ed):
    view = ed.outline_pane.outline_view
    view.expandAll()
    items = {text.split(":")[0]: idx for text, _, idx in gui_gui.outline_items(ed)}
    mime = view.model().mimeData([items["VCU3-SRS-2"]])
    _drop(view, mime, view.visualRect(items["VCU3-SRS-15"]).center())


def _drop_svg_on_grid(gui_gui, ed, svg):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(svg))])
    _drop(gui_gui.table(ed), mime, gui_gui.cell_rect(ed, "VCU3-SRS-2", "Description").center())


def _type_in_grid_cell(gui_gui, ed):
    w = gui_gui.open_cell_editor(ed, "VCU3-SRS-2", "Rationale")
    target = w if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit)) else gui_gui.table(ed)
    QTest.keyClicks(target, "typed-91")
    QTest.keyClick(target, Qt.Key.Key_Return)
    gui_gui.app.processEvents()


def _type_in_inspector(gui_gui, ed, which):
    gui_gui.select(ed, "VCU3-SRS-1")
    w = gui_gui.insp_description(ed) if which == "description" else gui_gui.insp_field(ed, "Rationale")
    w.setFocus()
    QTest.keyClicks(w, "typed-91")
    gui_gui.click_cell(ed, "VCU3-SRS-11")  # the field loses the focus


def _pick_in_inspector(gui_gui, ed):
    gui_gui.select(ed, "VCU3-SRS-2")
    w = gui_gui.insp_field(ed, "Status")
    w.setFocus()
    QTest.keyClick(w, Qt.Key.Key_Down)
    gui_gui.app.processEvents()


def _table_menu_actions(gui_gui, ed, oid):
    gui_gui.select(ed, oid)
    menu = gui_gui.toolbar_button(ed, "Table").menu()
    menu.aboutToShow.emit()
    for a in list(menu.actions()):
        if a.isEnabled() and a.text() and not a.isSeparator():
            a.trigger()
            gui_gui.app.processEvents()


def _click_buttons(gui_gui, parent, text):
    for b in parent.findChildren(QPushButton) + parent.findChildren(QToolButton):
        if _plain(b.text()).startswith(text) and b.isVisible() and b.isEnabled():
            gui_gui.bot.mouseClick(b, Qt.MouseButton.LeftButton)
            gui_gui.app.processEvents()


def _ack_in_inspector(gui_gui, ed):
    gui_gui.select(ed, "VCU3-SRS-15")  # its link to VCU3-SyRS-47 is suspect
    _click_buttons(gui_gui, ed.inspector, "Ack")


def _ack_selected(gui_gui, ed):
    gui_gui.select(ed, "VCU3-SRS-15")
    _click_buttons(gui_gui, ed.grid_pane.banner, "Ack selected")


def _discard_from_menu(gui_gui, ed):
    _open_menu(gui_gui, ed)
    a = gui_gui.action(ed, "Discard uncommitted changes to this document")
    if a.isEnabled():
        a.trigger()
        gui_gui.app.processEvents()


def _inputs_91(gui_gui, ed, svg):
    """Every input path the user has, by name."""
    commit = lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_S, _CTRL)  # noqa: E731
    return {
        "typing in a grid cell": lambda: _type_in_grid_cell(gui_gui, ed),
        "typing in the Inspector's description": lambda: _type_in_inspector(gui_gui, ed, "description"),
        "typing in an Inspector text attribute": lambda: _type_in_inspector(gui_gui, ed, "rationale"),
        "a list pick in the Inspector": lambda: _pick_in_inspector(gui_gui, ed),
        "paste into the grid": lambda: _paste_in_grid(gui_gui, ed),
        "paste into a grid cell editor": lambda: _paste_in_cell_editor(gui_gui, ed),
        "paste into the Inspector": lambda: _paste_in_inspector(gui_gui, ed),
        "dragging a grid row": lambda: _drag_grid_row(gui_gui, ed),
        "dragging an outline-pane row": lambda: _drag_outline_row(gui_gui, ed),
        "dropping an SVG file on the grid": lambda: _drop_svg_on_grid(gui_gui, ed, svg),
        "the Table menu on an object with a table": lambda: _table_menu_actions(gui_gui, ed, "VCU3-SRS-16"),
        "the Table menu on an object without one": lambda: _table_menu_actions(gui_gui, ed, "VCU3-SRS-2"),
        "Ack in the Inspector": lambda: _ack_in_inspector(gui_gui, ed),
        "Ack selected": lambda: _ack_selected(gui_gui, ed),
        "Commit (Ctrl+S)": commit,
        "Delete (Del)": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-13", Qt.Key.Key_Delete),
        "Undo (Ctrl+Z)": lambda: _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Z, _CTRL),
        "Redo (Ctrl+Y)": lambda: _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Y, _CTRL),
        "dragging a column of the active view": lambda: _drag_column(gui_gui, ed),
        "New (Ctrl+N)": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-13", Qt.Key.Key_N, _CTRL),
        "New child (Ctrl+Shift+N)": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-13", Qt.Key.Key_N, _CTRL | _SHIFT),
        "Indent (Ctrl+])": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-10", Qt.Key.Key_BracketRight, _CTRL),
        "Indent (Tab)": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-10", Qt.Key.Key_Tab),
        "Outdent (Ctrl+[)": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-1", Qt.Key.Key_BracketLeft, _CTRL),
        "Move up (Alt+Up)": lambda: _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_Up, _ALT),
        "Attach figure (Ctrl+G)": lambda: _attach_figure_by_key(gui_gui, ed, svg),
        "Add link (Ctrl+L)": lambda: _add_link_by_key(gui_gui, ed),
        "Properties": lambda: _edit_properties(gui_gui, ed),
        "Discard": lambda: _discard_from_menu(gui_gui, ed),
    }


@pytest.mark.parametrize("state", ["plain", "changed_on_disk", "failed_to_load"])
def test_gui_srs_91_read_only_mode_changes_nothing(gui_gui, repo, state):
    """MELLON-GUI-SRS-91: while in read-only mode, whatever the user's input, the Document Editor leaves unchanged the
    git index, each git reference, and each file and folder of the work tree; in a plain window, one showing the
    Changed on disk banner, and one holding a document that failed to load."""
    _add_table(repo, "VCU3-SRS-16")
    from mellon.service.document import DocumentService

    DocumentService(repo).create_view(SRS, "Pair", ["priority", "status"])
    _git_commit_all(repo, "Add a table and a view")
    _uncommitted_change(repo)  # something for Commit and Discard to act on
    svg = _svg_file(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _edit_attribute(gui_gui, ed, "VCU3-SRS-1", "undo-me")  # something for Undo to take back
    assert _rationale(repo, "VCU3-SRS-1") == "undo-me", "precondition: the edit is written"
    _choose_view(gui_gui, ed, "Pair")  # a column drag changes the active view's file
    _open_menu(gui_gui, ed)
    gui_gui.action(ed, "Read-only").trigger()
    assert gui_gui.is_read_only(ed), "precondition: read-only mode is on"
    if state == "changed_on_disk":
        _change_elsewhere(_obj_file(repo, SYRS, "VCU3-SyRS-42"))
        _reactivated(gui_gui, ed, pe)
        assert _stale_shown(ed), "precondition: the Changed on disk banner shows"
    elif state == "failed_to_load":
        _write(_obj_file(repo, SRS, "VCU3-SRS-12"), "<object id='VCU3-SRS-12'><broken")
        _press_in_grid(gui_gui, ed, None, Qt.Key.Key_F5)
        assert [e for e in gui_gui.log if "failed to load" in e[2].lower()], "precondition: the reload failed"
    _commit_accepting(gui_gui)
    _answer_yes(gui_gui)
    gui_gui.answers["getText"] = ("typed-91", True)
    gui_gui.answers["getInt"] = (2, True)
    gui_gui.answers["getItem"] = lambda *a: (a[3][0] if len(a) > 3 and a[3] else "", True)
    # A click that selects another row than the one aimed at isn't this requirement's concern: the input still runs.
    gui_gui.select = lambda e, oid: gui_gui.click_cell(e, oid, "ID")
    changed = {}
    # Unexpected errors raised by an input are reported to the lead, not judged here.
    with gui_gui.bot.captureExceptions() as errors:
        for name, run in _inputs_91(gui_gui, ed, svg).items():
            before = _snapshot(repo)
            run()
            gui_gui.app.processEvents()
            diff = _snapshot_diff(before, _snapshot(repo))
            if diff:
                changed[name] = diff
            assert gui_gui.is_read_only(ed), f"read-only mode went off after {name}"
    assert changed == {}, (f"inputs that changed the repository in read-only mode: {changed}; "
                           f"unexpected errors: {[str(e[1]) for e in errors]}")
