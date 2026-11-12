"""Slice 4: link picker + trace view + remove-link context menu.

All tests drive the dialogs and action handlers directly instead of
simulating clicks — faster and scoped to the layer we changed.
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


# --- repo.all_objects ------------------------------------------------------


def test_all_objects_preorder_and_no_dupes():
    from mellon.repo import all_objects, load_all

    docs = load_all(REQ_TEST)
    triples = all_objects(docs)
    ids = [(d, o) for d, o, _ in triples]

    # Every object exactly once.
    assert len(ids) == len(set(ids))
    # Doc ids sorted; within each doc, outline pre-order.
    srs_ids = [o for d, o in ids if d == "VCU3-SRS"]
    # VCU3-SRS-0 is the first heading; SRS-1 (its first child) comes right after.
    assert srs_ids[:2] == ["VCU3-SRS-0", "VCU3-SRS-1"]
    # Titles populated.
    titles = [t for d, o, t in triples if o == "VCU3-SRS-1"]
    assert titles and "FAULT_OVERTEMP" in titles[0]


# --- LinkPicker ------------------------------------------------------------


def test_link_picker_excludes_source(window):
    from mellon.gui.dialogs.link_picker import LinkPicker, TARGET_ROLE

    w, _ = window
    dlg = LinkPicker(w.controller, "VCU3-SRS", "VCU3-SRS-1")
    targets = []
    for row in range(dlg._model.rowCount()):
        item = dlg._model.item(row)
        targets.append(item.data(TARGET_ROLE))
    assert ("VCU3-SRS", "VCU3-SRS-1") not in targets
    # The other SRS objects are present.
    assert ("VCU3-SRS", "VCU3-SRS-2") in targets


def test_link_picker_filter_narrows_list(window):
    w, _ = window
    from mellon.gui.dialogs.link_picker import LinkPicker

    dlg = LinkPicker(w.controller, "VCU3-SRS", "VCU3-SRS-1")
    total = dlg._proxy.rowCount()
    dlg._search.setText("SyRS-42")
    filtered = dlg._proxy.rowCount()
    assert 0 < filtered < total
    # Every visible row mentions SyRS-42.
    for r in range(filtered):
        idx = dlg._proxy.index(r, 0)
        assert "SyRS-42" in idx.data()


def test_link_picker_selected_returns_tuple(window):
    from PySide6.QtCore import Qt
    from mellon.gui.dialogs.link_picker import LinkPicker

    w, _ = window
    dlg = LinkPicker(w.controller, "VCU3-SRS", "VCU3-SRS-1")
    dlg._search.setText("SyRS-42")
    # Select the first visible row.
    dlg._list.setCurrentIndex(dlg._proxy.index(0, 0))
    dlg._type_combo.setCurrentText("satisfies")
    assert dlg.selected() == ("VCU3-SyRS", "VCU3-SyRS-42", "satisfies")


def test_add_link_action_writes_to_disk(window, monkeypatch):
    """MainWindow._add_link composes + commits via the LinkPicker dialog.

    We short-circuit the dialog by monkey-patching `exec()` to return Accepted
    immediately and pre-filling the selection.
    """
    from PySide6.QtWidgets import QDialog

    w, root = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")  # no outgoing links yet

    from mellon.gui import main_window as mw
    from mellon.gui.dialogs import link_picker as lp_mod

    class StubLinkPicker(lp_mod.LinkPicker):
        def exec(self_):  # noqa: N807 (matches Qt casing)
            # Default selection: first row, type "satisfies". We override to
            # pick SyRS-42 explicitly so the test is deterministic.
            self_._search.setText("SyRS-42")
            self_._list.setCurrentIndex(self_._proxy.index(0, 0))
            self_._type_combo.setCurrentText("satisfies")
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw, "LinkPicker", StubLinkPicker)
    w._add_link()

    links_file = root / "VCU3-SRS" / "links" / "VCU3-SRS-10.xml"
    assert links_file.exists()
    content = links_file.read_text(encoding="utf-8")
    assert "VCU3-SyRS-42" in content
    assert "satisfies" in content


# --- TraceView -------------------------------------------------------------


def test_trace_view_shows_two_roots_with_chain(window):
    from mellon.gui.dialogs.trace_view import TraceView

    w, _ = window
    # VCU3-STS-1 is a test case that verifies VCU3-SRS-1 which satisfies
    # VCU3-SyRS-42; going up from it should walk two levels.
    dlg = TraceView(w.controller, "VCU3-STS", "VCU3-STS-1")
    model = dlg._model
    # Two top-level categories: Up + Down.
    assert model.rowCount() == 2
    up_root = model.item(0)
    down_root = model.item(1)
    assert "Up" in up_root.text()
    assert "Down" in down_root.text()

    # Up: STS-1 → SRS-1 → SyRS-42.
    assert up_root.rowCount() == 1
    srs1 = up_root.child(0)
    assert "VCU3-SRS-1" in srs1.text()
    assert srs1.rowCount() == 1
    syrs42 = srs1.child(0)
    assert "VCU3-SyRS-42" in syrs42.text()


def test_trace_view_double_click_selects_target(window):
    from PySide6.QtWidgets import QDialog
    from mellon.gui.dialogs.trace_view import TARGET_ROLE, TraceView

    w, _ = window
    dlg = TraceView(w.controller, "VCU3-STS", "VCU3-STS-1")
    srs1 = dlg._model.item(0).child(0)  # first up-trace hop
    idx = dlg._model.indexFromItem(srs1)
    # Simulate the double-click handler directly.
    assert srs1.data(TARGET_ROLE) == ("VCU3-SRS", "VCU3-SRS-1")
    dlg._on_double_clicked(idx)
    assert w.controller.current_doc_id() == "VCU3-SRS"
    assert w.controller.current_obj_id() == "VCU3-SRS-1"
    # Dialog finishes in accepted state.
    assert dlg.result() == QDialog.DialogCode.Accepted


# --- inspector context menu: remove link -----------------------------------


def test_inspector_remove_outgoing_link_writes_through(window):
    w, root = window
    # VCU3-SRS-1 has exactly one outgoing link to VCU3-SyRS-42.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    assert (root / "VCU3-SRS" / "links" / "VCU3-SRS-1.xml").exists()

    w.inspector._remove_outgoing_link("VCU3-SyRS", "VCU3-SyRS-42")

    # Links file had only that link, so remove_link deletes the file.
    assert not (root / "VCU3-SRS" / "links" / "VCU3-SRS-1.xml").exists()
    # And the controller's loaded document reflects the removal.
    doc = w.controller.document("VCU3-SRS")
    assert doc.links.get("VCU3-SRS-1", ()) == ()


def test_inspector_remove_nonexistent_link_reports_error(window):
    w, _ = window
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-10")

    caught: list[tuple[str, str, str, str]] = []
    w.controller.edit_failed.connect(
        lambda d, o, f, m: caught.append((d, o, f, m))
    )
    w.inspector._remove_outgoing_link("VCU3-SyRS", "VCU3-SyRS-42")
    assert caught and caught[0][2] == "link"


# MELLON-TRACE-SRS blind tests
import re  # noqa: E402

from tests.trace_support import (  # noqa: E402
    Modals,
    Repo,
    _qt,
    _v_model_repo,
    basic_repo,
    click_row,
    disk_links,
    find_row,
    grid_ids,
    grid_view,
    inspector_lists,
    link_row_menu,
    open_editor,
    remove_from_working_copy,
    row_text,
    selected_ids,
)


def top_editors():
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.main_window import DocumentEditorWindow
    return [t for t in QtWidgets.QApplication.topLevelWidgets() if isinstance(t, DocumentEditorWindow) and t.isVisible()]


def open_explorer(qtbot, root):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    pe = ProjectExplorerWindow()
    qtbot.addWidget(pe)
    pe.show()
    assert pe.open_repo(Path(root)) is not False
    qtbot.wait(200)
    return pe


def explorer_open(qtbot, pe, doc_id):
    """Double-click a document in the Project Explorer; return the editor that opens."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    trees = [v for v in pe.findChildren(QtWidgets.QTreeView) if v.isVisible()]
    assert len(trees) == 1
    tree = trees[0]
    m = tree.model()
    rows = [r for r in range(m.rowCount()) if str(m.data(m.index(r, 0))).split(" ")[0] == doc_id]
    assert len(rows) == 1, f"{doc_id} not listed"
    before = set(map(id, top_editors()))
    rect = tree.visualRect(m.index(rows[0], 0))
    QtTest.QTest.mouseClick(tree.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, rect.center())
    QtTest.QTest.mouseDClick(tree.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, rect.center())
    qtbot.wait(300)
    new = [t for t in top_editors() if id(t) not in before]
    assert len(new) == 1, f"double-clicking {doc_id} opened {len(new)} editors"
    for t in new:
        qtbot.addWidget(t)
    return new[0]


def test_trace_srs_6_link_picker_leaves_out_a_target_deleted_on_disk(tmp_path, qtbot):
    """MELLON-TRACE-SRS-6: the link picker doesn't offer an object whose file
    another program deleted after the Document Editor read the document."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.dialogs.link_picker import LinkPicker

    r = basic_repo(tmp_path)
    w = open_editor(qtbot, r.root, "CHILD")
    remove_from_working_copy(r, "PARENT", "PARENT-2", ["PARENT-0", "PARENT-1", "PARENT-3"])
    dlg = LinkPicker(w.controller, "CHILD", "CHILD-1", w)
    qtbot.addWidget(dlg)
    views = dlg.findChildren(QtWidgets.QListView)
    rows = [str(v.model().data(v.model().index(i, 0))) for v in views for i in range(v.model().rowCount())]
    assert any("PARENT-1 " in t + " " for t in rows), rows
    assert not any("PARENT-2 " in t + " " for t in rows), rows


def test_trace_srs_6_document_editor_refuses_link_to_target_deleted_on_disk(tmp_path, qtbot):
    """MELLON-TRACE-SRS-6: If a new link's target object doesn't exist in the working copy, Mellon
    shall refuse to create the link. Condition: in a Document Editor, the user picks PARENT-2 in the
    link picker, and another program deletes PARENT-2 while the picker is open."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.dialogs.link_picker import LinkPicker

    r = basic_repo(tmp_path)
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-1")
    offered = []

    def pick():
        mw = QtWidgets.QApplication.activeModalWidget()
        if not isinstance(mw, LinkPicker):
            QtCore.QTimer.singleShot(20, pick)
            return
        views = [v for v in mw.findChildren(QtWidgets.QListView) if v.isVisible()]
        m = views[0].model()
        rows = [str(m.data(m.index(i, 0))) for i in range(m.rowCount())]
        offered.extend(rows)
        hit = [i for i, t in enumerate(rows) if "PARENT-2 " in t + " "]
        # Deleted while the picker is open.
        remove_from_working_copy(r, "PARENT", "PARENT-2", ["PARENT-0", "PARENT-1", "PARENT-3"])
        if hit:
            views[0].setCurrentIndex(m.index(hit[0], 0))
            mw.accept()
        else:
            mw.reject()

    QtCore.QTimer.singleShot(20, pick)
    [a for a in w.findChildren(QtGui.QAction) if a.text().startswith("Link")][0].trigger()
    qtbot.waitUntil(lambda: bool(offered), timeout=5000)
    qtbot.wait(200)
    assert any("PARENT-2 " in t + " " for t in offered), f"precondition: PARENT-2 offered: {offered}"
    assert ("PARENT", "PARENT-2") not in disk_links(r.root, "CHILD", "CHILD-1"), (
        f"a link to the deleted PARENT-2 was created (picker offered {offered})"
    )
    assert "failed" in w.statusBar().currentMessage().lower(), w.statusBar().currentMessage()


# ===========================================================================
# GUI: link picker, Inspector, state gutter, suspect banner, trace view
# ===========================================================================

LINK_TYPES_17 = ["satisfies", "verifies", "refines", "implements"]


def _record_init(monkeypatch, cls) -> list:
    """Record (doc, obj) each time `cls` is constructed for a source or traced object."""
    calls = []
    orig = cls.__init__

    def init(self, controller, doc_id, obj_id, parent=None):
        calls.append((doc_id, obj_id))
        orig(self, controller, doc_id, obj_id, parent)

    monkeypatch.setattr(cls, "__init__", init)
    return calls


def _press(qtbot, w, key):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    w.activateWindow()
    w.raise_()
    qtbot.wait(50)
    target = QtWidgets.QApplication.focusWidget() or grid_view(w)
    QtTest.QTest.keyClick(target, key, QtCore.Qt.ControlModifier)
    qtbot.wait(200)


def test_trace_srs_16_ctrl_l_opens_the_link_picker_for_the_selected_object(tmp_path, qtbot, monkeypatch):
    """MELLON-TRACE-SRS-16: The Document Editor shall have an action, with the shortcut Ctrl+L, that
    opens the link picker for the selected object. Condition: CHILD-2 selected in an editable
    Document Editor; the user presses Ctrl+L. Expected: one action has Ctrl+L, and the link picker
    opens with CHILD/CHILD-2 as its source."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.dialogs.link_picker import LinkPicker

    r = basic_repo(tmp_path)
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-2")
    with_shortcut = [a for a in w.findChildren(QtGui.QAction) if QtGui.QKeySequence("Ctrl+L") in a.shortcuts()]
    assert len(with_shortcut) == 1, [a.text() for a in with_shortcut]
    opened = _record_init(monkeypatch, LinkPicker)
    modals = Modals()
    _press(qtbot, w, QtCore.Qt.Key_L)
    modals.stop()
    assert opened == [("CHILD", "CHILD-2")], f"link pickers opened: {opened}"


def test_trace_srs_17_link_picker_offers_exactly_four_link_types(tmp_path, qtbot):
    """MELLON-TRACE-SRS-17: The link picker shall offer exactly the link types satisfies, verifies,
    refines and implements. Condition: open the link picker for CHILD-1. Expected: its link type
    choices are those four, none missing and no other."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.dialogs.link_picker import LinkPicker

    r = basic_repo(tmp_path)
    w = open_editor(qtbot, r.root, "CHILD")
    lp = LinkPicker(w.controller, "CHILD", "CHILD-1", w)
    qtbot.addWidget(lp)
    lp.show()
    combos = lp.findChildren(QtWidgets.QComboBox)
    assert len(combos) == 1, f"expected one link type choice, found {len(combos)}"
    items = [combos[0].itemText(i) for i in range(combos[0].count())]
    assert sorted(items) == sorted(LINK_TYPES_17) and len(items) == 4, items


def test_trace_srs_26_inspector_offers_remove_on_each_outgoing_link(tmp_path, qtbot):
    """MELLON-TRACE-SRS-26: The Inspector shall offer a Remove action on each outgoing link of the
    selected object. Condition: CHILD-1 has two outgoing links; open each row's context menu, then
    trigger Remove on the link to PARENT-2. Expected: both menus offer Remove; only that link goes."""
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7]), ("PARENT", "PARENT-2", "satisfies", r.c0[:7])])
    r.commit("links")
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-1")
    out, _ = inspector_lists(w)
    assert out.count() == 2, [row_text(out, i) for i in range(out.count())]
    for i in range(2):
        menu = link_row_menu(qtbot, out, i)
        assert menu and any(t.startswith("Remove") for t in menu), f"row {row_text(out, i)!r}: menu {menu}"
    modals = Modals()
    link_row_menu(qtbot, out, find_row(out, "PARENT-2"), choose="Remove")
    qtbot.wait(100)
    modals.stop()
    assert set(disk_links(r.root, "CHILD", "CHILD-1")) == {("PARENT", "PARENT-1")}


def test_trace_srs_34_inspector_shows_each_incoming_link(tmp_path, qtbot):
    """MELLON-TRACE-SRS-34: The Inspector shall show each incoming link of the selected object.
    Condition: SYS-1 has incoming links from SW-1..SW-4 (another document) and SYS-2 (its own);
    SW-5 links to SYS-3. Expected: the Inspector lists exactly those five sources for SYS-1."""
    r = Repo(tmp_path / "repo")
    r.add_doc("SYS", {"SYS-1": "s1", "SYS-2": "s2", "SYS-3": "s3"})
    r.add_doc("SW", {f"SW-{i}": f"w{i}" for i in range(1, 6)}, parents=["SYS"])
    for i in range(1, 5):
        r.set_links("SW", f"SW-{i}", [("SYS", "SYS-1", "satisfies", None)])
    r.set_links("SW", "SW-5", [("SYS", "SYS-3", "satisfies", None)])
    r.set_links("SYS", "SYS-2", [("SYS", "SYS-1", "refines", None)])
    r.commit("seed")
    w = open_editor(qtbot, r.root, "SYS", editable=False)
    click_row(qtbot, w, "SYS-1")
    _, inc = inspector_lists(w)
    named = [set(re.findall(r"\b(?:SW|SYS)-\d+\b", row_text(inc, i))) - {"SYS-1"} for i in range(inc.count())]
    named = sorted(sorted(n) for n in named if n)
    assert named == [["SW-1"], ["SW-2"], ["SW-3"], ["SW-4"], ["SYS-2"]], [row_text(inc, i) for i in range(inc.count())]


def test_trace_srs_90_inspector_shows_each_outgoing_links_state(tmp_path, qtbot):
    """MELLON-TRACE-SRS-90: The Inspector shall show the state of each outgoing link of the selected
    object. Condition: CHILD-1 has an OK, a suspect and a broken outgoing link. Expected: each row
    shows its own state and neither of the other two."""
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7]),
                                     ("PARENT", "PARENT-2", "satisfies", None),
                                     ("PARENT", "PARENT-9", "satisfies", None)])
    r.commit("links")
    w = open_editor(qtbot, r.root, "CHILD", editable=False)
    click_row(qtbot, w, "CHILD-1")
    out, _ = inspector_lists(w)
    states = {"ok", "suspect", "broken"}
    for target, state in [("PARENT-1", "ok"), ("PARENT-2", "suspect"), ("PARENT-9", "broken")]:
        words = set(re.findall(r"[a-z]+", row_text(out, find_row(out, target)).lower()))
        assert state in words and not (states - {state}) & words, f"{target}: row shows {words & states}"


PARENT_IDS = ["PARENT-0", "PARENT-1", "PARENT-2", "PARENT-3"]


def _jump_setup(tmp_path, qtbot, open_parent_first: bool):
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7]), ("PARENT", "PARENT-2", "satisfies", r.c0[:7])])
    r.commit("links")
    pe = open_explorer(qtbot, r.root)
    parent_w = explorer_open(qtbot, pe, "PARENT") if open_parent_first else None
    child_w = explorer_open(qtbot, pe, "CHILD")
    click_row(qtbot, child_w, "CHILD-1")
    out, _ = inspector_lists(child_w)
    return parent_w, child_w, out, find_row(out, "PARENT-1")


def _double_click_row(qtbot, lw, i):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    rect = lw.visualItemRect(lw.item(i))
    QtTest.QTest.mouseClick(lw.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, rect.center())
    QtTest.QTest.mouseDClick(lw.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, rect.center())
    qtbot.wait(400)


def _view_of(w) -> tuple:
    return (w.isVisible(), grid_ids(w), selected_ids(w))


def test_trace_srs_100_double_click_selects_target_in_the_open_target_document_editor(tmp_path, qtbot):
    """MELLON-TRACE-SRS-100: When the user double-clicks an outgoing link in the Inspector, Mellon
    shall show the link's target object selected in the target document's Document Editor.
    Condition: PARENT's and CHILD's Document Editors are open (from the Project Explorer); the user
    double-clicks CHILD-1's link to PARENT-1. Expected: PARENT's editor shows PARENT-1 selected."""
    parent_w, child_w, out, i = _jump_setup(tmp_path, qtbot, open_parent_first=True)
    _double_click_row(qtbot, out, i)
    assert _view_of(parent_w) == (True, PARENT_IDS, ["PARENT-1"]), (
        f"PARENT's editor: {_view_of(parent_w)}; CHILD's editor: {_view_of(child_w)}")


def test_trace_srs_100_double_click_shows_target_selected_when_no_target_editor_is_open(tmp_path, qtbot):
    """MELLON-TRACE-SRS-100: When the user double-clicks an outgoing link in the Inspector, Mellon
    shall show the link's target object selected in the target document's Document Editor.
    Condition: only CHILD's Document Editor is open. Expected: a Document Editor shows PARENT's
    objects with PARENT-1 selected."""
    parent_w, child_w, out, i = _jump_setup(tmp_path, qtbot, open_parent_first=False)
    _double_click_row(qtbot, out, i)
    views = [_view_of(t) for t in top_editors()]
    assert (True, PARENT_IDS, ["PARENT-1"]) in views, views


def test_trace_srs_101_inspector_offers_jump_on_each_outgoing_link(tmp_path, qtbot):
    """MELLON-TRACE-SRS-101: The Inspector shall offer a Jump action on each outgoing link, with the
    same result as double-clicking the link. Condition: CHILD-1 has two outgoing links. Expected:
    each row's context menu offers Jump."""
    parent_w, child_w, out, i = _jump_setup(tmp_path, qtbot, open_parent_first=False)
    for row in range(out.count()):
        menu = link_row_menu(qtbot, out, row)
        assert menu and any(t.startswith("Jump") for t in menu), f"row {row_text(out, row)!r}: {menu}"


def test_trace_srs_101_jump_has_the_same_result_as_double_click(tmp_path, qtbot):
    """MELLON-TRACE-SRS-101: The Inspector shall offer a Jump action on each outgoing link, with the
    same result as double-clicking the link. Condition: PARENT's and CHILD's editors open; Jump on
    CHILD-1's link to PARENT-1 in one setup, a double-click in an identical one. Expected: both
    windows end up the same way."""
    parent_w, child_w, out, i = _jump_setup(tmp_path / "a", qtbot, open_parent_first=True)
    _double_click_row(qtbot, out, i)
    by_double_click = (_view_of(parent_w), _view_of(child_w))
    for t in top_editors():
        t.close()
    parent_w, child_w, out, i = _jump_setup(tmp_path / "b", qtbot, open_parent_first=True)
    link_row_menu(qtbot, out, i, choose="Jump")
    qtbot.wait(400)
    assert (_view_of(parent_w), _view_of(child_w)) == by_double_click


def test_trace_srs_177_ctrl_t_opens_the_trace_view_for_the_selected_object(tmp_path, qtbot, monkeypatch):
    """MELLON-TRACE-SRS-177: The Document Editor shall have an action, with the shortcut Ctrl+T, that
    opens the trace view for the selected object. Condition: CHILD-2 selected; the user presses
    Ctrl+T. Expected: one action has Ctrl+T, and the trace view opens for CHILD/CHILD-2."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.dialogs.trace_view import TraceView

    r = basic_repo(tmp_path)
    w = open_editor(qtbot, r.root, "CHILD", editable=False)
    click_row(qtbot, w, "CHILD-2")
    with_shortcut = [a for a in w.findChildren(QtGui.QAction) if QtGui.QKeySequence("Ctrl+T") in a.shortcuts()]
    assert len(with_shortcut) == 1, [a.text() for a in with_shortcut]
    opened = _record_init(monkeypatch, TraceView)
    modals = Modals()
    _press(qtbot, w, QtCore.Qt.Key_T)
    modals.stop()
    for t in QtWidgets.QApplication.topLevelWidgets():
        if isinstance(t, TraceView):
            t.close()
    assert opened == [("CHILD", "CHILD-2")], f"trace views opened: {opened}"


def _tree_sections(view) -> dict:
    """Top-level rows of a tree view -> the texts below each."""
    m = view.model()

    def below(parent):
        out = []
        for r_ in range(m.rowCount(parent)):
            i = m.index(r_, 0, parent)
            out.append(" ".join(str(m.data(m.index(r_, c, parent))) for c in range(m.columnCount(parent))))
            out += below(i)
        return out

    root = m.index(-1, -1)
    return {str(m.data(m.index(r_, 0, root))): " | ".join(below(m.index(r_, 0, root))) for r_ in range(m.rowCount(root))}


def test_trace_srs_178_trace_view_shows_trace_up_and_trace_down(tmp_path, qtbot):
    """MELLON-TRACE-SRS-178: The trace view shall show the selected object's trace up and its trace
    down. Condition: SRS-1 satisfies SYS-1, and STS-1 verifies SRS-1. Expected: the Up part holds
    SYS-1 and not STS-1; the Down part holds STS-1 and not SYS-1."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    from mellon.gui.dialogs.trace_view import TraceView

    r = _v_model_repo(tmp_path)
    w = open_editor(qtbot, r.root, "SRS", editable=False)
    tv = TraceView(w.controller, "SRS", "SRS-1", w)
    qtbot.addWidget(tv)
    tv.show()
    qtbot.wait(100)
    views = [v for v in tv.findChildren(QtWidgets.QTreeView) if v.isVisible()]
    assert len(views) == 1
    sections = _tree_sections(views[0])
    up = [t for h, t in sections.items() if re.search(r"\bUp\b", h)]
    down = [t for h, t in sections.items() if re.search(r"\bDown\b", h)]
    assert len(up) == 1 and len(down) == 1, f"sections: {list(sections)}"
    assert "SYS-1" in up[0] and "STS-1" not in up[0], up[0]
    assert "STS-1" in down[0] and "SYS-1" not in down[0], down[0]
