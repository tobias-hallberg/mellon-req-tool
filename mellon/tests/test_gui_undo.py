"""Tests for undo / redo in the Document Editor window.

Edits go through the service, as the window's own actions do; the window
records each one for this document as an undo step.
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
    """A writable VCU3-SRS editor on a copy of the fixture."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)
    svc = DocumentService(dst, cache=True)
    w = DocumentEditorWindow("VCU3-SRS", svc)
    qtbot.addWidget(w)
    w._set_read_only(False)
    return w, svc, dst


def _files(root: Path) -> dict[Path, bytes]:
    return {
        p: p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


def test_undo_and_redo_an_edit(window):
    w, svc, root = window
    obj = root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    before = obj.read_bytes()
    assert not w._undo_act.isEnabled()

    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "Edited.")
    edited = obj.read_bytes()
    assert w._undo_act.isEnabled()
    assert w._undo_act.text() == "&Undo set description"

    def shown() -> str:
        return w.controller.document("VCU3-SRS").objects["VCU3-SRS-1"].description

    w._undo_act.trigger()
    assert obj.read_bytes() == before
    assert shown() != "Edited."
    assert not w._undo_act.isEnabled()
    assert w._redo_act.text() == "&Redo set description"

    w._redo_act.trigger()
    assert obj.read_bytes() == edited
    assert shown() == "Edited."


def test_steps_undo_newest_first_and_a_new_edit_drops_redo(window):
    w, svc, _root = window
    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "One.")
    svc.set_attribute("VCU3-SRS", "VCU3-SRS-1", "status", "Approved")
    assert w._undo_act.text() == "&Undo set attribute"
    w._undo_act.trigger()
    assert w._undo_act.text() == "&Undo set description"
    assert w._redo_act.isEnabled()
    svc.set_description("VCU3-SRS", "VCU3-SRS-2", "Two.")
    assert not w._redo_act.isEnabled()


def test_undo_a_delete_brings_back_links_in_other_documents(window):
    """Deleting VCU3-SRS-1 strips the STS links to it; one undo restores
    every file, in both documents."""
    w, svc, root = window
    before = _files(root)
    incoming = svc.incoming_links("VCU3-SRS", "VCU3-SRS-1")
    assert incoming  # the STS verifies it
    svc.delete_object("VCU3-SRS", "VCU3-SRS-1", force=True)
    assert "VCU3-SRS-1" not in w.controller.document("VCU3-SRS").objects

    w._undo_act.trigger()
    assert _files(root) == before
    assert "VCU3-SRS-1" in w.controller.document("VCU3-SRS").objects


def test_edits_to_other_documents_are_not_steps_here(window):
    w, svc, _root = window
    svc.set_description("VCU3-STS", "VCU3-STS-1", "Elsewhere.")
    assert not w._undo_act.isEnabled()


def test_undo_disabled_while_read_only(window):
    w, svc, _root = window
    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "Edited.")
    w._set_read_only(True)
    assert not w._undo_act.isEnabled()
    w._set_read_only(False)
    assert w._undo_act.isEnabled()


@pytest.mark.parametrize(
    "event",
    [
        lambda w: w.controller.committed.emit("VCU3-SRS", "abc1234"),
        lambda w: w._reload_from_disk(),  # F5, and Discard
        lambda w: w._set_stale(True),  # the "Changed on disk" banner
    ],
    ids=["commit", "reload", "stale"],
)
def test_history_cleared_by(window, event):
    w, svc, _root = window
    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "Edited.")
    w._undo_act.trigger()
    svc.set_description("VCU3-SRS", "VCU3-SRS-2", "Edited.")
    assert w._undo_steps
    event(w)
    assert w._undo_steps == [] and w._redo_steps == []
    assert not w._undo_act.isEnabled() and not w._redo_act.isEnabled()


def test_undo_refused_after_an_outside_change(window, monkeypatch):
    """A file the step wrote changed since (say MCP edited it): undo would
    clobber that, so it is refused, nothing changes and the history goes."""
    from mellon.gui import main_window as mw

    w, svc, root = window
    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "Mine.")
    obj = root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    obj.write_bytes(obj.read_bytes().replace(b"Mine.", b"Theirs."))
    warnings: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "warning",
        staticmethod(lambda _p, _t, text, *a, **k: warnings.append(text) or 0),
    )
    snap = _files(root)

    w._undo_act.trigger()

    assert _files(root) == snap
    assert len(warnings) == 1 and "changed on disk since" in warnings[0]
    assert w._undo_steps == []
    assert not w._undo_act.isEnabled()


def test_undo_flushes_pending_text_first_and_undoes_it(window):
    """Typed-but-unsaved Inspector text is written first; Ctrl+Z then
    undoes that, the newest edit."""
    w, svc, root = window
    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "Saved.")
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    w.inspector._description.setPlainText("Typed, not yet saved.")

    w._undo_act.trigger()

    text = w.controller.document("VCU3-SRS").objects["VCU3-SRS-1"].description
    assert text == "Saved."
    assert w._undo_act.text() == "&Undo set description"  # "Saved." is next


def test_close_unhooks_the_window(window):
    w, svc, _root = window
    hooked = len(svc._change_listeners)
    w.close()
    assert len(svc._change_listeners) == hooked - 1
    svc.set_description("VCU3-SRS", "VCU3-SRS-1", "After close.")  # no error


# MELLON-EDIT-SRS blind tests (176 and up)
import hashlib  # noqa: E402

import pygit2  # noqa: E402

from tests.edit_b_support import (  # noqa: E402
    SVG_A,
    SVG_RED,
    MessageBoxes,
    _attr_combos,
    _choose,
    _choose_file,
    _edit_cell,
    _figure_repo,
    _filter_files,
    _filters_repo,
    _gui_table_repo,
    _header_2x2,
    _inspector_field,
    _name_field,
    _new_view_handler,
    _open_table_editor,
    _operator_combo,
    _plain,
    _save_table,
    activate,
    answer_text,
    button,
    changed_files,
    drive_dialogs,
    find_action,
    git_commit_all,
    menu_action,
    open_editor,
    select,
    toolbar_menu,
    tree_hashes,
    write_doc,
)
from tests.store_support import DECL  # noqa: E402


def git_commit_paths(root: Path, message: str, paths: list[str]) -> str:
    """Commit only `paths` (repo-relative, posix) from the working copy."""
    repo = pygit2.Repository(str(root))
    for p in paths:
        if (root / p).exists():
            repo.index.add(p)
        else:
            repo.index.remove(p)
    repo.index.write()
    tree = repo.index.write_tree()
    sig = repo.default_signature
    oid = repo.create_commit("HEAD", sig, sig, message, tree, [repo.head.target])
    return str(oid)


def actions_starting(widget, prefix: str):
    from PySide6.QtGui import QAction

    return [a for a in widget.findChildren(QAction)
            if _plain(a.text()).startswith(prefix)]


def undo_state(window) -> tuple[str, bool, str, bool]:
    """(Undo text, enabled, Redo text, enabled) from the Edit menu."""
    undo = actions_starting(window, "Undo")[0]
    redo = actions_starting(window, "Redo")[0]
    return (_plain(undo.text()), undo.isEnabled(), _plain(redo.text()),
            redo.isEnabled())


# ==========================================================================
# Undo and redo in the Document Editor
# ==========================================================================


def doc_hashes(root: Path, doc_id: str) -> dict[str, str]:
    """sha256 of each file in a document's folder, editor side files aside."""
    folder = root / doc_id
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()
            and not p.name.startswith((".$", "~$"))}


def work_hashes(root: Path) -> dict[str, str]:
    """sha256 of every working-copy file outside .git."""
    return {k: v for k, v in tree_hashes(root).items() if not k.startswith(".git/")}


def undo(window, qtbot) -> None:
    action = actions_starting(window, "Undo")[0]
    assert action.isEnabled(), "Undo is disabled"
    action.trigger()
    qtbot.wait(10)


def redo(window, qtbot) -> None:
    action = actions_starting(window, "Redo")[0]
    assert action.isEnabled(), "Redo is disabled"
    action.trigger()
    qtbot.wait(10)


def set_status(window, qtbot, obj_id: str, value: str) -> None:
    """Set an object's status from the Inspector, as the user does."""
    select(window, obj_id, qtbot)
    combo = _inspector_field(window, "status")
    index = combo.findText(value)
    assert index >= 0
    combo.setCurrentIndex(index)
    qtbot.wait(10)


def nothing_to_undo(window) -> bool:
    _, undo_on, _, redo_on = undo_state(window)
    return not undo_on and not redo_on


# --- -341: each operation the window makes is one named undo step ---------


def _one_step_check(window, qtbot, root: Path, before: dict, operation: str):
    """After one operation: exactly one step, named, which Undo reverses."""
    after = work_hashes(root)
    assert changed_files(before, after), f"{operation} changed no file"
    text, undo_on, _, _ = undo_state(window)
    assert undo_on, f"{operation} recorded no undo step"
    assert text != "Undo" and text.startswith("Undo "), (
        f"the undo step of {operation} is not named: {text!r}")
    undo(window, qtbot)
    assert changed_files(before, work_hashes(root)) == [], (
        f"Undo didn't reverse {operation}")
    assert undo_state(window)[1] is False, (
        f"{operation} recorded more than one undo step")


def test_edit_srs_341_table_editor_save_is_one_undo_step(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-341: a table edit saved from the table editor is one
    undo step, named, which Undo reverses."""
    root = _gui_table_repo(tmp_path, _header_2x2())
    window, editor = _open_table_editor(qtbot, root, monkeypatch)
    before = work_hashes(root)
    _edit_cell(qtbot, editor, 1, 1, ["changed"])
    _save_table(qtbot, editor)
    _one_step_check(window, qtbot, root, before, "a table edit")


def test_edit_srs_341_figure_attach_is_one_undo_step(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-341: attaching a figure from the Figure menu is one
    undo step, named, which Undo reverses (the figure file goes again)."""
    root = _figure_repo(tmp_path)
    MessageBoxes(monkeypatch)
    src = tmp_path / "a.svg"
    src.write_bytes(SVG_A)
    window = open_editor(qtbot, root, "F")
    select(window, "F-0", qtbot)
    _choose_file(monkeypatch, src)
    before = work_hashes(root)
    menu_action(window, "Figure", "Attach figure").trigger()
    qtbot.wait(10)
    _one_step_check(window, qtbot, root, before, "attaching a figure")


def test_edit_srs_341_properties_dialog_change_is_one_undo_step(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-341: a Properties dialog attribute change (adding the
    option Rejected to status) is one undo step, named, which Undo
    reverses."""
    from PySide6.QtWidgets import QTabWidget, QTreeWidget

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    answer_text(monkeypatch, "Rejected")
    window = open_editor(qtbot, root, "D")
    before = work_hashes(root)

    def handler(dialog):
        tabs = dialog.findChildren(QTabWidget)[0]
        tabs.setCurrentIndex([tabs.tabText(i) for i in range(tabs.count())].index(
            "Attributes"))
        tree = [t for t in dialog.findChildren(QTreeWidget) if t.isVisible()][0]
        item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                    if "status" in [tree.topLevelItem(i).text(c)
                                    for c in range(tree.columnCount())])
        tree.setCurrentItem(item)
        qtbot.wait(5)
        button(dialog, "Add option").click()
        qtbot.wait(5)
        button(dialog, "Close").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    find_action(window, "Properties").trigger()
    qtbot.wait(10)
    manifest = (root / "D" / "D.xml").read_text(encoding="utf-8")
    assert "<option>Rejected</option>" in manifest
    _one_step_check(window, qtbot, root, before, "a Properties dialog change")


def test_edit_srs_341_saving_filter_from_dialog_is_one_undo_step(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-341: saving a filter from the Advanced filter dialog
    is one undo step, named, which Undo reverses (the filter file goes)."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    before = work_hashes(root)

    def handler(dialog):
        _name_field(dialog).setText("Musts")
        combo = _attr_combos(dialog)[0]
        _choose(combo, "priority")
        _choose(_operator_combo(combo), "eq")
        button(dialog, "OK").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "New filter").trigger()
    qtbot.wait(10)
    assert list(_filter_files(root)) == ["musts.xml"]
    _one_step_check(window, qtbot, root, before, "saving a filter")


def test_edit_srs_341_saving_new_view_is_one_undo_step(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-341: saving a view with New view is one undo step,
    named, which Undo reverses (the view file goes)."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    before = work_hashes(root)
    drive_dialogs(monkeypatch, qtbot, _new_view_handler(qtbot, "QA", ["status"]))
    menu_action(window, "Views", "New view").trigger()
    qtbot.wait(10)
    assert (root / "D" / "views" / "qa.xml").exists()
    _one_step_check(window, qtbot, root, before, "saving a view")


# --- -347 / -348: text fields and pending edits ----------------------------


def _description_field(window):
    from PySide6.QtWidgets import QPlainTextEdit, QTextEdit

    candidates = (window.inspector.findChildren(QTextEdit)
                  + window.inspector.findChildren(QPlainTextEdit))
    fields = [f for f in candidates if f.isVisible() and not f.isReadOnly()]
    assert fields, "no editable description in the Inspector"
    return fields[0]


def _text_of(field) -> str:
    return field.toPlainText()


def test_edit_srs_347_undo_shortcut_in_text_field_acts_on_its_text(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-347: with the Inspector's description focused, type abc
    after its text and press the undo shortcut: abc leaves the field, no
    file changes and the window's undo history is unchanged; the redo
    shortcut puts abc back."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    window.activateWindow()
    set_status(window, qtbot, "D-2", "Draft")  # one step in the history
    history = undo_state(window)
    assert history[1] is True
    select(window, "D-1", qtbot)
    field = _description_field(window)
    field.setFocus()
    qtbot.wait(5)
    field.moveCursor(QTextCursor.End)
    original = _text_of(field)
    qtbot.keyClicks(field, "abc")
    assert _text_of(field) == original + "abc"
    files = work_hashes(root)
    qtbot.keyClick(field, Qt.Key_Z, Qt.ControlModifier)
    qtbot.wait(10)
    assert _text_of(field) == original
    assert changed_files(files, work_hashes(root)) == []
    assert undo_state(window) == history
    qtbot.keyClick(field, Qt.Key_Y, Qt.ControlModifier)
    qtbot.wait(10)
    assert _text_of(field) == original + "abc"
    assert undo_state(window) == history


def test_edit_srs_348_undo_discards_pending_text_and_redo_brings_it_back(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-348: after an earlier edit (so that Undo is offered),
    type text into the Inspector's description (a pending edit) and choose
    Undo: the pending text is discarded, the stored description and the
    earlier edit are unchanged; choosing Redo makes the description the
    typed text."""
    from PySide6.QtGui import QTextCursor

    from mellon.loader import load_document

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-2", "Draft")  # the earlier edit
    select(window, "D-1", qtbot)
    field = _description_field(window)
    field.setFocus()
    field.moveCursor(QTextCursor.End)
    qtbot.keyClicks(field, " typed")
    stored = load_document(root / "D").objects["D-1"].description
    assert stored == "alpha requirement"
    undo(window, qtbot)
    doc = load_document(root / "D")
    assert doc.objects["D-1"].description == stored
    assert doc.objects["D-2"].attrs["status"] == "Draft", (
        "Undo undid the edit before the typing")
    select(window, "D-1", qtbot)
    assert _text_of(_description_field(window)) == stored
    redo(window, qtbot)
    window.flush_pending_edits()
    qtbot.wait(10)
    select(window, "D-1", qtbot)
    assert _text_of(_description_field(window)) == "alpha requirement typed"


# --- -349 to -354: how far back the history goes ---------------------------


def test_edit_srs_349_three_undos_restore_three_states_newest_first(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-342/-349: three edits, then Undo three times: each
    Undo restores the state before one edit, newest first, and a fourth Undo
    is unavailable."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    states = [doc_hashes(root, "D")]
    set_status(window, qtbot, "D-1", "Approved")
    states.append(doc_hashes(root, "D"))
    select(window, "D-1", qtbot)
    find_action(window, "Move down").trigger()
    qtbot.wait(10)
    states.append(doc_hashes(root, "D"))
    select(window, "D-5", qtbot)
    find_action(window, "Indent").trigger()
    qtbot.wait(10)
    states.append(doc_hashes(root, "D"))
    assert len({tuple(sorted(s.items())) for s in states}) == 4, "an edit changed nothing"
    for expected in (states[2], states[1], states[0]):
        undo(window, qtbot)
        assert changed_files(expected, doc_hashes(root, "D")) == []
    assert undo_state(window)[1] is False


def _commit_from_window(window, qtbot, monkeypatch) -> None:
    def handler(dialog):
        button(dialog, "Commit").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    find_action(window, "Commit").trigger()
    qtbot.wait(20)


def test_edit_srs_349_commit_leaves_nothing_to_undo(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-349/-350: the history holds the steps since the latest
    commit: after committing from the window there is nothing to undo."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-1", "Approved")
    set_status(window, qtbot, "D-2", "Draft")
    head = pygit2.Repository(str(root)).head.target
    _commit_from_window(window, qtbot, monkeypatch)
    assert pygit2.Repository(str(root)).head.target != head, "nothing was committed"
    assert nothing_to_undo(window)


def test_edit_srs_352_discard_clears_undo_history(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-352: with a step to undo and one to redo, choose
    Discard and confirm: Undo and Redo are disabled."""
    root = _filters_repo(tmp_path)
    boxes = MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-1", "Approved")
    set_status(window, qtbot, "D-2", "Draft")
    undo(window, qtbot)
    _, undo_on, _, redo_on = undo_state(window)
    assert undo_on and redo_on
    toolbar_menu(window, "Menu")  # the user opens the menu that holds Discard
    discard = find_action(window, "Discard uncommitted changes to this document")
    assert discard.isEnabled()
    discard.trigger()
    qtbot.wait(20)
    assert boxes.calls, "Discard didn't ask to confirm"
    assert pygit2.Repository(str(root)).status() == {}, "Discard didn't run"
    assert nothing_to_undo(window)


def test_edit_srs_354_commit_in_git_client_clears_history(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-354: edit the document from the window, commit all of
    its changes in a git client and return to the window: there is no step
    to undo."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-1", "Approved")
    assert undo_state(window)[1] is True
    git_commit_all(root, "commit from a git client")
    activate(window, qtbot)
    assert nothing_to_undo(window)


def test_edit_srs_354_commit_of_other_document_keeps_history(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-354, other side: a git-client commit of another
    document's files only, while this document keeps an uncommitted change,
    leaves the history (as built; -355 would change this)."""
    root = _filters_repo(tmp_path)
    write_doc(root, "Z", objects={"Z-0": ({"type": "Requirement"}, "unrelated")},
              next_number=1)
    git_commit_all(root, "add Z")
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-1", "Approved")
    set_status(window, qtbot, "D-2", "Draft")
    history = undo_state(window)
    assert history[1] is True
    # Z is neither D nor one of D's parent documents.
    obj = root / "Z" / "objects" / "Z-0.xml"
    obj.write_text(obj.read_text(encoding="utf-8").replace("unrelated", "unrelated v2"),
                   encoding="utf-8", newline="\n")
    git_commit_paths(root, "other document", ["Z/objects/Z-0.xml"])
    activate(window, qtbot)
    assert undo_state(window) == history


# --- -356 / -358: refusing when a file changed since ----------------------


def _refusal_check(window, qtbot, root: Path, boxes, action) -> None:
    """The undo or redo is refused with a message, no file changes and the
    history is cleared."""
    files = tree_hashes(root)
    calls = len(boxes.calls)
    action(window, qtbot)
    qtbot.wait(10)
    work_now = tree_hashes(root)
    assert [p for p in changed_files(files, work_now) if not p.startswith(".git/")] == [], (
        "a refused undo/redo changed files")
    assert len(boxes.calls) > calls, "the refusal told the user nothing"
    assert nothing_to_undo(window), "the refusal didn't clear the history"


def _delete_repo(tmp_path: Path) -> Path:
    """D-1 is the target of a link from E-0 in document E."""
    root = _filters_repo(tmp_path)
    write_doc(root, "E", objects={"E-0": ({"type": "Requirement"}, "uses D-1")},
              links={"E-0": [("D", "D-1", "satisfies")]}, parents=["D"],
              next_number=1)
    git_commit_all(root, "add E")
    return root


def test_edit_srs_356_undo_refused_when_other_documents_links_file_changed(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-356 (a): undo of a step that deleted an object with
    incoming links from another document is refused, with the reason, when
    that other document's links file changed outside afterwards; no file
    changes and the history is cleared (MELLON-EDIT-SRS-358)."""
    root = _delete_repo(tmp_path)
    boxes = MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    select(window, "D-1", qtbot)
    find_action(window, "Delete").trigger()
    qtbot.wait(20)
    assert not (root / "D" / "objects" / "D-1.xml").exists(), "the delete didn't run"
    links = root / "E" / "links" / "E-0.xml"
    # Another program changes E's links file after the step.
    links.parent.mkdir(exist_ok=True)
    links.write_text(DECL + '<links source="E-0">\n  <link target-doc="D" '
                     'target-id="D-2" type="satisfies"/>\n</links>\n',
                     encoding="utf-8", newline="\n")
    _refusal_check(window, qtbot, root, boxes, undo)


def test_edit_srs_356_undo_refused_after_same_size_rewrite_within_2_s(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-356 (b): rewrite the step's object file with different
    bytes of the same size within 2 s of the step (keeping its modified
    time), then Undo: refused with the reason, no file changes, history
    cleared."""
    root = _filters_repo(tmp_path)
    boxes = MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-1", "Approved")
    obj = root / "D" / "objects" / "D-1.xml"
    data = obj.read_bytes()
    stat = obj.stat()
    rewritten = data.replace(b"alpha requirement", b"ALPHA requirement")
    assert rewritten != data and len(rewritten) == len(data)
    obj.write_bytes(rewritten)
    os.utime(obj, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    _refusal_check(window, qtbot, root, boxes, undo)
    assert obj.read_bytes() == rewritten


def test_edit_srs_358_refused_redo_clears_history(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-356 (c) / -358: Undo, change the file outside, then
    Redo: refused with the reason, no file changes, and the history is
    cleared."""
    root = _filters_repo(tmp_path)
    boxes = MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-2", "Draft")
    set_status(window, qtbot, "D-1", "Approved")
    undo(window, qtbot)
    obj = root / "D" / "objects" / "D-1.xml"
    obj.write_text(obj.read_text(encoding="utf-8").replace("alpha", "omega"),
                   encoding="utf-8", newline="\n")
    _refusal_check(window, qtbot, root, boxes, redo)


# --- -359 / -360: operations from outside the window ----------------------


def test_edit_srs_359_drawio_save_adds_no_undo_step(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-359: attach a figure from the window (one step), then
    overwrite the figure file in place as draw.io desktop saves and return to
    the window: no step is added (Undo still names the attach), and choosing
    Undo is refused because the figure file changed since the step."""
    root = _figure_repo(tmp_path)
    boxes = MessageBoxes(monkeypatch)
    src = tmp_path / "a.svg"
    src.write_bytes(SVG_A)
    window = open_editor(qtbot, root, "F")
    select(window, "F-0", qtbot)
    _choose_file(monkeypatch, src)
    menu_action(window, "Figure", "Attach figure").trigger()
    qtbot.wait(10)
    label = undo_state(window)[0]
    assert undo_state(window)[1] and label != "Undo"
    fig = root / "F" / "figures" / "F-0.svg"
    with open(fig, "r+b") as fh:
        fh.write(SVG_RED)
        fh.truncate()
    activate(window, qtbot)
    assert undo_state(window)[0] == label
    _refusal_check(window, qtbot, root, boxes, undo)
    assert fig.read_bytes() == SVG_RED


def test_edit_srs_360_other_service_edit_adds_no_undo_step(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-360: a second, separate service standing for the MCP
    server edits this window's document; on return to the window no undo
    step has been added."""
    from mellon.service.document import DocumentService

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    assert nothing_to_undo(window)
    DocumentService(root).set_attribute("D", "D-1", "status", "Approved")
    activate(window, qtbot)
    assert undo_state(window)[1] is False


def test_edit_srs_360_other_window_edit_adds_no_undo_step(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-360: an edit made in another Document Editor window on
    the same document adds no step to this window's history."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    first = open_editor(qtbot, root, "D")
    second = open_editor(qtbot, root, "D")
    set_status(second, qtbot, "D-1", "Approved")
    assert undo_state(second)[1] is True
    activate(first, qtbot)
    assert undo_state(first)[1] is False


def test_edit_srs_361_read_only_disables_undo_and_redo(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-361: make an edit and undo it, so Undo and Redo both
    have a step; read-only mode disables both, and leaving it enables them
    again."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    set_status(window, qtbot, "D-2", "Draft")
    set_status(window, qtbot, "D-1", "Approved")
    undo(window, qtbot)
    _, undo_on, _, redo_on = undo_state(window)
    assert undo_on and redo_on
    read_only = find_action(window, "Read-only")
    read_only.trigger()
    qtbot.wait(10)
    assert read_only.isChecked()
    _, undo_on, _, redo_on = undo_state(window)
    assert (undo_on, redo_on) == (False, False)
    read_only.trigger()
    qtbot.wait(10)
    _, undo_on, _, redo_on = undo_state(window)
    assert (undo_on, redo_on) == (True, True)
