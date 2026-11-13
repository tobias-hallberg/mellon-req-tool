"""Keyboard shortcuts of the Project Explorer and the Document Editor.

The shortcuts span both windows, so their tests are kept together here, as
test_gui_readonly.py keeps a mode's. Blind tests for MELLON-GUI-SRS-150 to -164 and
-167, written from the requirements and the public signatures alone; the helpers are
in tests/gui_support.py.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QToolButton,
)

from tests.gui_support import (
    SRS,
    STS,
    _ALT,
    _CTRL,
    _SHIFT,
    _change_elsewhere,
    _edit_attribute,
    _external_edit,
    _grid_text,
    _manifest,
    _obj_file,
    _outline,
    _press_in_grid,
    _rationale,
    _reactivated,
    _shown,
    _snapshot,
    _snapshot_diff,
    _stale_shown,
    _uncommitted_change,
    gui_gui,  # noqa: F401, F811 -- fixture
    repo,  # noqa: F401, F811 -- fixture
)


# --------------------------------------------------------------------------- Document Editor shortcuts


def _parent_of(repo, oid, doc=SRS):
    return dict(_outline(repo, doc))[oid]


def _children_of(repo, parent, doc=SRS):
    return [o for o, p in _outline(repo, doc) if p == parent]


def test_gui_srs_150_ctrl_shift_n_runs_new_child(gui_gui, repo):
    """MELLON-GUI-SRS-150: Ctrl+Shift+N runs New child: a new object is created as a sub-object of the selected one."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = {o for o, _ in _outline(repo, SRS)}
    _press_in_grid(gui_gui, ed, "VCU3-SRS-14", Qt.Key.Key_N, _CTRL | _SHIFT)
    new = [(o, p) for o, p in _outline(repo, SRS) if o not in before]
    assert len(new) == 1, f"objects created: {new}"
    assert new[0][1] == "VCU3-SRS-14"


@pytest.mark.parametrize("key", ["ctrl_bracket", "tab"])
def test_gui_srs_151_indent_by_key(gui_gui, repo, key):
    """MELLON-GUI-SRS-151: Ctrl+], or Tab while the grid has the focus and no cell is being edited, runs Indent:
    VCU3-SRS-10 becomes a sub-object of VCU3-SRS-0, the object before it."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    assert _parent_of(repo, "VCU3-SRS-10") is None
    if key == "ctrl_bracket":
        _press_in_grid(gui_gui, ed, "VCU3-SRS-10", Qt.Key.Key_BracketRight, _CTRL)
    else:
        _press_in_grid(gui_gui, ed, "VCU3-SRS-10", Qt.Key.Key_Tab)
    assert _parent_of(repo, "VCU3-SRS-10") == "VCU3-SRS-0"


@pytest.mark.parametrize("key", ["ctrl_bracket", "shift_tab"])
def test_gui_srs_152_outdent_by_key(gui_gui, repo, key):
    """MELLON-GUI-SRS-152: Ctrl+[, or Shift+Tab while the grid has the focus and no cell is being edited, runs
    Outdent: VCU3-SRS-1 moves to the top level."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    assert _parent_of(repo, "VCU3-SRS-1") == "VCU3-SRS-0"
    if key == "ctrl_bracket":
        _press_in_grid(gui_gui, ed, "VCU3-SRS-1", Qt.Key.Key_BracketLeft, _CTRL)
    else:
        _press_in_grid(gui_gui, ed, "VCU3-SRS-1", Qt.Key.Key_Backtab, _SHIFT)
    assert _parent_of(repo, "VCU3-SRS-1") is None


@pytest.mark.parametrize("key", ["tab", "shift_tab"])
def test_gui_srs_151_152_tab_in_cell_editor_leaves_outline(gui_gui, repo, key):
    """MELLON-GUI-SRS-151, -152: Tab and Shift+Tab indent and outdent only while no cell is being edited; in a
    grid cell editor they leave the outline unchanged."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    manifest = _manifest(repo, SRS).read_bytes()
    editor = gui_gui.open_cell_editor(ed, "VCU3-SRS-10" if key == "tab" else "VCU3-SRS-1", "Rationale")
    assert isinstance(editor, QLineEdit), f"precondition: a cell editor is open, focus is {type(editor).__name__}"
    if key == "tab":
        QTest.keyClick(editor, Qt.Key.Key_Tab)
    else:
        QTest.keyClick(editor, Qt.Key.Key_Backtab, _SHIFT)
    gui_gui.app.processEvents()
    assert _manifest(repo, SRS).read_bytes() == manifest, "the outline changed"


def test_gui_srs_153_alt_up_runs_move_up(gui_gui, repo):
    """MELLON-GUI-SRS-153: Alt+Up runs Move up: VCU3-SRS-2 comes before VCU3-SRS-1 among VCU3-SRS-0's sub-objects."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_Up, _ALT)
    kids = _children_of(repo, "VCU3-SRS-0")
    assert kids.index("VCU3-SRS-2") < kids.index("VCU3-SRS-1"), kids


def test_gui_srs_154_alt_down_runs_move_down(gui_gui, repo):
    """MELLON-GUI-SRS-154: Alt+Down runs Move down: VCU3-SRS-1 comes after VCU3-SRS-2."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-1", Qt.Key.Key_Down, _ALT)
    kids = _children_of(repo, "VCU3-SRS-0")
    assert kids.index("VCU3-SRS-1") > kids.index("VCU3-SRS-2"), kids


def _new_dialogs(gui_gui, since: int) -> list[tuple[str, str, str, str]]:
    return gui_gui.log[since:]


def test_gui_srs_155_del_in_grid_runs_delete_object(gui_gui, repo):
    """MELLON-GUI-SRS-155: Del while no text field has the focus runs Delete object, whose confirmation shows."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = _snapshot(repo)
    gui_gui.select(ed, "VCU3-SRS-13")
    since = len(gui_gui.log)
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Delete)
    asked = _new_dialogs(gui_gui, since)
    assert asked and "VCU3-SRS-13" in "".join(asked[0]), f"no Delete confirmation: {asked}"
    assert _snapshot_diff(before, _snapshot(repo)) == [], "answering No still changed files"


def _text_of(w) -> str:
    return w.toPlainText() if isinstance(w, (QTextEdit, QPlainTextEdit)) else w.text()


def _in_text_field(gui_gui, ed, which):
    """Focus a text field holding 'abc' with the cursor at its start; returns the field."""
    if which == "filter_box":
        w = ed.grid_pane.filter_box
        w.setFocus()
        QTest.keyClicks(w, "abc")
    else:
        gui_gui.select(ed, "VCU3-SRS-1")
        w = gui_gui.insp_description(ed) if which == "inspector_description" else gui_gui.insp_field(ed, "Rationale")
        w.setFocus()
        QTest.keyClick(w, Qt.Key.Key_A, _CTRL)
        QTest.keyClicks(w, "abc")
    QTest.keyClick(w, Qt.Key.Key_Home, _CTRL if isinstance(w, (QTextEdit, QPlainTextEdit)) else Qt.KeyboardModifier.NoModifier)
    gui_gui.app.processEvents()
    assert QApplication.focusWidget() is w, "precondition: the field has the focus"
    assert _text_of(w) == "abc", f"precondition: the field holds 'abc', not {_text_of(w)!r}"
    return w


@pytest.mark.parametrize("which", ["filter_box", "inspector_description", "inspector_text_attribute"])
def test_gui_srs_155_del_in_text_field_deletes_a_character(gui_gui, repo, which):
    """MELLON-GUI-SRS-155: Del while a text field has the focus deletes a character and doesn't run Delete object."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    w = _in_text_field(gui_gui, ed, which)
    since = len(gui_gui.log)
    objects = sorted(p.name for p in (repo / SRS / "objects").iterdir())
    gui_gui.key(ed, Qt.Key.Key_Delete)
    assert _text_of(w) == "bc", f"the field holds {_text_of(w)!r}"
    assert _new_dialogs(gui_gui, since) == [], "Delete object ran"
    assert sorted(p.name for p in (repo / SRS / "objects").iterdir()) == objects


def test_gui_srs_155_del_in_dialog_text_box_deletes_a_character(gui_gui, repo):
    """MELLON-GUI-SRS-155: Del in a dialog's text box (the commit dialog's message) deletes a character and doesn't
    run Delete object."""
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    gui_gui.select(ed, "VCU3-SRS-13")
    seen = {}

    def in_dialog(d):
        d.show()
        gui_gui.activate(d)
        boxes = [w for w in d.findChildren(QLineEdit) + d.findChildren(QPlainTextEdit) + d.findChildren(QTextEdit)
                 if w.isVisible() and w.isEnabled() and not getattr(w, "isReadOnly", lambda: False)()]
        assert boxes, "the commit dialog has no text box"
        box = boxes[0]
        box.setFocus()
        QTest.keyClick(box, Qt.Key.Key_A, _CTRL)
        QTest.keyClicks(box, "abc")
        QTest.keyClick(box, Qt.Key.Key_Home, _CTRL if isinstance(box, (QTextEdit, QPlainTextEdit)) else
                       Qt.KeyboardModifier.NoModifier)
        since = len(gui_gui.log)
        QTest.keyClick(box, Qt.Key.Key_Delete)
        gui_gui.app.processEvents()
        seen["text"] = _text_of(box)
        seen["dialogs"] = gui_gui.log[since:]
        d.hide()
        return int(QDialog.DialogCode.Rejected)

    gui_gui.handlers["CommitDialog"] = in_dialog
    objects = sorted(p.name for p in (repo / SRS / "objects").iterdir())
    gui_gui.action(ed, "Commit").trigger()
    gui_gui.app.processEvents()
    assert seen, "the commit dialog didn't open"
    assert seen["text"] == "bc", f"the text box holds {seen['text']!r}"
    assert seen["dialogs"] == [], "Delete object ran"
    assert sorted(p.name for p in (repo / SRS / "objects").iterdir()) == objects


def test_gui_srs_156_ctrl_g_runs_attach_figure(gui_gui, repo):
    """MELLON-GUI-SRS-156: Ctrl+G runs Attach or replace figure: its file chooser opens."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_G, _CTRL)
    assert [e for e in gui_gui.log if e[1] == "QFileDialog"], f"no file chooser opened: {gui_gui.log}"


def test_gui_srs_157_ctrl_s_and_ctrl_k_run_commit(gui_gui, repo):
    """MELLON-GUI-SRS-157: Ctrl+S and Ctrl+K each run Commit: the commit dialog opens each time."""
    _uncommitted_change(repo)
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_S, _CTRL)
    assert len(gui_gui.opened("CommitDialog")) == 1, "Ctrl+S opened no commit dialog"
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_K, _CTRL)
    assert len(gui_gui.opened("CommitDialog")) == 2, "Ctrl+K opened no commit dialog"


def test_gui_srs_158_ctrl_e_runs_export(gui_gui, repo):
    """MELLON-GUI-SRS-158: Ctrl+E runs Export: its suspect-link warning or its save dialog opens."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_E, _CTRL)
    gui_gui.bot.wait(200)
    started = [e for e in gui_gui.log if e[0] == "getSaveFileName" or "suspect" in e[2].lower()
               or "export" in (e[2] + e[3]).lower()]
    assert started, f"Export didn't start: {gui_gui.log}"


def test_gui_srs_159_ctrl_f_focuses_filter_box(gui_gui, repo):
    """MELLON-GUI-SRS-159: Ctrl+F with the focus in the grid moves the focus to the filter box."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_F, _CTRL)
    assert QApplication.focusWidget() is ed.grid_pane.filter_box


def test_gui_srs_160_ctrl_shift_f_runs_advanced_filter(gui_gui, repo):
    """MELLON-GUI-SRS-160: Ctrl+Shift+F runs Advanced filter: its dialog opens."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_F, _CTRL | _SHIFT)
    assert [e for e in gui_gui.log if "filter" in (e[1] + e[2]).lower()], f"no Advanced filter dialog: {gui_gui.log}"


def test_gui_srs_161_f5_runs_reload_from_disk(gui_gui, repo):
    """MELLON-GUI-SRS-161: F5 in a Document Editor runs Reload from disk: the new text shows and the Changed on disk
    banner hides."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    _external_edit(_obj_file(repo, SRS, "VCU3-SRS-2"), "The VCU shall sample", "Elsewhere the VCU shall sample")
    _reactivated(gui_gui, ed, pe)
    assert _stale_shown(ed), "precondition: the Changed on disk banner shows"
    _press_in_grid(gui_gui, ed, "VCU3-SRS-11", Qt.Key.Key_F5)
    assert not _stale_shown(ed)
    assert _grid_text(gui_gui, ed, "VCU3-SRS-2").startswith("Elsewhere the VCU shall sample")


def test_gui_srs_162_ctrl_z_in_grid_runs_undo(gui_gui, repo):
    """MELLON-GUI-SRS-162: Ctrl+Z while no text field has the focus runs Undo: the last edit is undone on disk."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    before = _obj_file(repo, SRS, "VCU3-SRS-2").read_bytes()
    _edit_attribute(gui_gui, ed, "VCU3-SRS-2", "undo-162")
    assert _rationale(repo, "VCU3-SRS-2") == "undo-162", "precondition: the edit is written"
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Z, _CTRL)
    assert _obj_file(repo, SRS, "VCU3-SRS-2").read_bytes() == before


def test_gui_srs_163_ctrl_y_and_ctrl_shift_z_in_grid_run_redo(gui_gui, repo):
    """MELLON-GUI-SRS-163: Ctrl+Y and Ctrl+Shift+Z while no text field has the focus run Redo."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _edit_attribute(gui_gui, ed, "VCU3-SRS-2", "redo-163")
    for mods, key in ((_CTRL, Qt.Key.Key_Y), (_CTRL | _SHIFT, Qt.Key.Key_Z)):
        _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Z, _CTRL)
        assert _rationale(repo, "VCU3-SRS-2") == "", "precondition: the edit is undone"
        _press_in_grid(gui_gui, ed, None, key, mods)
        assert _rationale(repo, "VCU3-SRS-2") == "redo-163", f"{key.name} with {mods} didn't redo"


def _field_for_undo(gui_gui, ed, which):
    """A text field of `which` kind with 'xyz' typed into it, the focus in it."""
    if which == "grid_cell_editor":
        w = gui_gui.open_cell_editor(ed, "VCU3-SRS-1", "Rationale")
        assert isinstance(w, QLineEdit), f"precondition: a cell editor is open, focus is {type(w).__name__}"
    elif which == "filter_box":
        w = ed.grid_pane.filter_box
    else:
        gui_gui.select(ed, "VCU3-SRS-1")
        w = gui_gui.insp_description(ed)
    w.setFocus()
    QTest.keyClick(w, Qt.Key.Key_End, _CTRL if isinstance(w, (QTextEdit, QPlainTextEdit)) else
                   Qt.KeyboardModifier.NoModifier)
    original = _text_of(w)
    QTest.keyClicks(w, "xyz")
    gui_gui.app.processEvents()
    assert _text_of(w) == original + "xyz", "precondition: the typed text is in the field"
    return w, original


_TEXT_FIELDS = ["inspector_description", "filter_box", "grid_cell_editor"]


@pytest.mark.parametrize("which", _TEXT_FIELDS)
def test_gui_srs_162_ctrl_z_in_text_field_undoes_its_text(gui_gui, repo, which):
    """MELLON-GUI-SRS-162: Ctrl+Z while a text field has the focus undoes the field's own text and doesn't run the
    window's Undo: no file changes."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _edit_attribute(gui_gui, ed, "VCU3-SRS-2", "kept-162")  # what the window's Undo would take back
    w, original = _field_for_undo(gui_gui, ed, which)
    before = _snapshot(repo)
    QTest.keyClick(w, Qt.Key.Key_Z, _CTRL)
    gui_gui.app.processEvents()
    assert _snapshot_diff(before, _snapshot(repo)) == [], "a file changed"
    assert _rationale(repo, "VCU3-SRS-2") == "kept-162", "the window's Undo ran"
    assert _shown(w) and _text_of(w) == original, "the field's text wasn't undone"


@pytest.mark.parametrize("which", _TEXT_FIELDS)
@pytest.mark.parametrize("keys", ["ctrl_y", "ctrl_shift_z"])
def test_gui_srs_163_redo_in_text_field_redoes_its_text(gui_gui, repo, which, keys):
    """MELLON-GUI-SRS-163: Ctrl+Y and Ctrl+Shift+Z while a text field has the focus redo the field's own text and
    don't run the window's Redo: no file changes."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _edit_attribute(gui_gui, ed, "VCU3-SRS-2", "undone-163")
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Z, _CTRL)  # the window's Redo would put it back
    assert _rationale(repo, "VCU3-SRS-2") == "", "precondition: the edit is undone"
    w, original = _field_for_undo(gui_gui, ed, which)
    QTest.keyClick(w, Qt.Key.Key_Z, _CTRL)
    gui_gui.app.processEvents()
    assert _text_of(w) == original, "precondition: the field's text is undone"
    before = _snapshot(repo)
    if keys == "ctrl_y":
        QTest.keyClick(w, Qt.Key.Key_Y, _CTRL)
    else:
        QTest.keyClick(w, Qt.Key.Key_Z, _CTRL | _SHIFT)
    gui_gui.app.processEvents()
    assert _snapshot_diff(before, _snapshot(repo)) == [], "a file changed"
    assert _rationale(repo, "VCU3-SRS-2") == "", "the window's Redo ran"
    assert _text_of(w) == original + "xyz", "the field's text wasn't redone"


@pytest.mark.parametrize("key", [
    "ctrl_w",
    "ctrl_f4",
])
def test_gui_srs_164_close_key_closes_the_window(gui_gui, repo, key):
    """MELLON-GUI-SRS-164: Ctrl+W or Ctrl+F4 in a Document Editor closes it, and the Project Explorer no longer
    lists it; the other Document Editor stays open."""
    pe = gui_gui.explorer(repo)
    other = gui_gui.open_doc(pe, STS)
    ed = gui_gui.open_doc(pe, SRS)
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_W if key == "ctrl_w" else Qt.Key.Key_F4, _CTRL)
    assert not _shown(ed), "the Document Editor didn't close"
    assert [t.split(" ")[0] for t in gui_gui.open_list(pe)] == [STS]
    assert _shown(other)


def _collapse(gui_gui, ed, tooltip):
    btn = [b for b in ed.findChildren(QToolButton) if b.toolTip() == tooltip]
    assert btn, f"no {tooltip!r} button"
    gui_gui.bot.mouseClick(btn[0], Qt.MouseButton.LeftButton)
    gui_gui.app.processEvents()


def _pane_collapsed(pane) -> bool:
    return not pane.isVisible() or pane.width() == 0


@pytest.mark.parametrize("collapsed", ["right_pane", "both_panes"])
def test_gui_srs_167_shortcuts_run_with_panes_collapsed(gui_gui, repo, collapsed):
    """MELLON-GUI-SRS-167: whichever panes are collapsed, each Document Editor shortcut runs its action
    (Ctrl+N, Ctrl+Shift+N, Ctrl+], Ctrl+[, Alt+Up, Alt+Down, Ctrl+F, Ctrl+Shift+F, Ctrl+Z, F5)."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    _collapse(gui_gui, ed, "Show/hide inspector pane")
    assert _pane_collapsed(ed.right_pane), "precondition: the right pane is collapsed"
    if collapsed == "both_panes":
        _collapse(gui_gui, ed, "Show/hide outline pane")
        assert _pane_collapsed(ed.outline_pane), "precondition: the outline pane is collapsed"
    failed = []

    def check(name, ok):
        if not ok:
            failed.append(name)

    ids = {o for o, _ in _outline(repo, SRS)}
    _press_in_grid(gui_gui, ed, "VCU3-SRS-13", Qt.Key.Key_N, _CTRL)
    new = [(o, p) for o, p in _outline(repo, SRS) if o not in ids]
    check("Ctrl+N", len(new) == 1 and new[0][1] == "VCU3-SRS-0")
    ids = {o for o, _ in _outline(repo, SRS)}
    _press_in_grid(gui_gui, ed, "VCU3-SRS-13", Qt.Key.Key_N, _CTRL | _SHIFT)
    new = [(o, p) for o, p in _outline(repo, SRS) if o not in ids]
    check("Ctrl+Shift+N", len(new) == 1 and new[0][1] == "VCU3-SRS-13")
    _press_in_grid(gui_gui, ed, "VCU3-SRS-10", Qt.Key.Key_BracketRight, _CTRL)
    check("Ctrl+]", _parent_of(repo, "VCU3-SRS-10") == "VCU3-SRS-0")
    _press_in_grid(gui_gui, ed, "VCU3-SRS-10", Qt.Key.Key_BracketLeft, _CTRL)
    check("Ctrl+[", _parent_of(repo, "VCU3-SRS-10") is None)
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_Up, _ALT)
    kids = _children_of(repo, "VCU3-SRS-0")
    check("Alt+Up", kids.index("VCU3-SRS-2") < kids.index("VCU3-SRS-1"))
    _press_in_grid(gui_gui, ed, "VCU3-SRS-2", Qt.Key.Key_Down, _ALT)
    kids = _children_of(repo, "VCU3-SRS-0")
    check("Alt+Down", kids.index("VCU3-SRS-2") > kids.index("VCU3-SRS-1"))
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_Z, _CTRL)  # undoes Alt+Down
    kids = _children_of(repo, "VCU3-SRS-0")
    check("Ctrl+Z", kids.index("VCU3-SRS-2") < kids.index("VCU3-SRS-1"))
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_F, _CTRL)
    check("Ctrl+F", QApplication.focusWidget() is ed.grid_pane.filter_box)
    since = len(gui_gui.log)
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_F, _CTRL | _SHIFT)
    check("Ctrl+Shift+F", any("filter" in (e[1] + e[2]).lower() for e in gui_gui.log[since:]))
    _change_elsewhere(_obj_file(repo, SRS, "VCU3-SRS-11"))
    _reactivated(gui_gui, ed, pe)
    assert _stale_shown(ed), "precondition: the Changed on disk banner shows"
    _press_in_grid(gui_gui, ed, None, Qt.Key.Key_F5)
    check("F5", not _stale_shown(ed))
    assert failed == [], f"shortcuts that didn't run their action: {failed}"
