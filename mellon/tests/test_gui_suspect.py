"""Slice 5: suspect review — banner, gutter, filter toggle, per-link ack, ack-all.

Exercises the action handlers directly against a tmp copy of `req-test/`.
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


# --- controller accessors --------------------------------------------------


def test_suspect_cache_matches_fixture(window):
    w, _ = window
    assert w.controller.suspect_count("VCU3-SRS") == 6
    # STS has OK links only.
    assert w.controller.suspect_count("VCU3-STS") == 0


def test_object_worst_state(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    assert w.controller.object_worst_state("VCU3-SRS", "VCU3-SRS-15") == "suspect"
    assert w.controller.object_worst_state("VCU3-SRS", "VCU3-SRS-1") == "ok"


# --- banner ---------------------------------------------------------------


def test_banner_shows_on_suspect_doc_and_hides_on_clean(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    assert not w.grid_pane.banner.isHidden()
    assert "6 suspect" in w.grid_pane.banner._label.text()
    w.controller.select_document("VCU3-STS")
    assert w.grid_pane.banner.isHidden()


def test_banner_suspect_only_toggle_filters_grid(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    assert not w.grid_pane.is_suspect_only()
    w.grid_pane.banner._suspect_only_btn.setChecked(True)
    assert w.grid_pane.is_suspect_only()
    w.grid_pane.banner._suspect_only_btn.setChecked(False)
    assert not w.grid_pane.is_suspect_only()


# --- gutter column --------------------------------------------------------


def test_gutter_column_reports_state(window):
    from mellon.gui.models.objects_grid import OBJ_STATE_ROLE

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    model = w.grid_pane._model
    state_col = model.column_index("state")
    # Find the row for VCU3-SRS-15 (a suspect-source object).
    row = model.row_for_obj_id("VCU3-SRS-15")
    state = model.index(row, state_col).data(OBJ_STATE_ROLE)
    assert state == "suspect"
    # VCU3-SRS-1 is OK.
    row_ok = model.row_for_obj_id("VCU3-SRS-1")
    assert model.index(row_ok, state_col).data(OBJ_STATE_ROLE) == "ok"


def test_gutter_column_reports_per_direction_link_states(window):
    """The gutter's `OBJ_LINK_STATES_ROLE` returns `(outgoing, incoming)`.

    Drives the two-triangle marker: green-for-ok / yellow-for-suspect / red-for-broken
    per direction, rendered by `StateGutterDelegate`.
    """
    from mellon.gui.models.objects_grid import OBJ_LINK_STATES_ROLE

    w, _ = window
    # SRS has outgoing links to SyRS and incoming from STS.
    w.controller.select_document("VCU3-SRS")
    model = w.grid_pane._model
    state_col = model.column_index("state")

    # SRS-1: OK outgoing (to SyRS-42), OK incoming (from STS-1).
    row = model.row_for_obj_id("VCU3-SRS-1")
    assert model.index(row, state_col).data(OBJ_LINK_STATES_ROLE) == ("ok", "ok")
    # SRS-15: suspect outgoing (ack mismatch to SyRS-47), OK incoming (from STS-5).
    row = model.row_for_obj_id("VCU3-SRS-15")
    out, inc = model.index(row, state_col).data(OBJ_LINK_STATES_ROLE)
    assert out == "suspect"
    assert inc in ("ok", "suspect")  # incoming-side state depends on STS ack state

    # SyRS has no outgoing links at all. Its objects show only incoming markers.
    w.controller.select_document("VCU3-SyRS")
    model = w.grid_pane._model
    # SyRS-47 is targeted by suspect SRS-15; expect suspect incoming.
    row = model.row_for_obj_id("VCU3-SyRS-47")
    out, inc = model.index(row, state_col).data(OBJ_LINK_STATES_ROLE)
    assert out == "none"
    assert inc == "suspect"
    # SyRS-42 has only OK incoming from several SRS objects.
    row = model.row_for_obj_id("VCU3-SyRS-42")
    assert model.index(row, state_col).data(OBJ_LINK_STATES_ROLE) == ("none", "ok")


# --- suspect-only filter --------------------------------------------------


def test_suspect_only_filter_narrows_grid(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    proxy = w.grid_pane._proxy
    total = proxy.rowCount()

    w.grid_pane.set_suspect_only(True)
    filtered = proxy.rowCount()
    assert 0 < filtered < total
    # Fixture: 6 suspects sourced from 6 distinct objects.
    assert filtered == 6


def test_row_state_matches_the_scan_for_every_row(window):
    """The filter reads the grid's per-row map, which must agree with the scan."""
    from mellon.gui.models.objects_grid import OBJ_STATE_ROLE

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    model = w.grid_pane._model
    states = set()
    for row in range(model.rowCount()):
        state = model.index(row, 0).data(OBJ_STATE_ROLE)
        assert state == w.controller.object_worst_state("VCU3-SRS", model.obj_id_at(row))
        states.add(state)
    assert states == {"ok", "suspect"}


def test_suspect_only_plus_text_filter(window):
    w, _ = window
    w.controller.select_document("VCU3-SRS")
    w.grid_pane.set_suspect_only(True)
    w.grid_pane.filter_box.setText("watchdog")
    # Only SRS-20 and SRS-21 (watchdog reqs, both suspect) survive.
    assert w.grid_pane._proxy.rowCount() == 2


# --- inspector per-link Ack ----------------------------------------------


def test_inspector_ack_single_link(window):
    w, _ = window
    # SRS-15 has one suspect outgoing link to SyRS-47.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-15")
    # Precondition: the link is suspect.
    before = w.controller.suspects_for_object("VCU3-SRS", "VCU3-SRS-15")
    assert len(before) == 1

    # Grab the LinkStatus and ack it directly.
    w.inspector._acknowledge_link(before[0])

    # Cache was invalidated + rescanned; SRS-15 is now clean.
    after = w.controller.suspects_for_object("VCU3-SRS", "VCU3-SRS-15")
    assert after == []
    # Global count dropped by 1 (5 remaining).
    assert w.controller.suspect_count("VCU3-SRS") == 5


# --- Ack selected (banner) ------------------------------------------------


def test_ack_selected_button_acks_suspects_on_current_object(window):
    w, _ = window
    # SRS-17 has exactly one link (to SyRS-61, never-reviewed). Acking it
    # should clear the object's suspects.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-17")
    assert len(w.controller.suspects_for_object("VCU3-SRS", "VCU3-SRS-17")) == 1
    w.grid_pane.banner.ack_selected_clicked.emit()
    assert w.controller.suspects_for_object("VCU3-SRS", "VCU3-SRS-17") == []


def test_ack_selected_button_disabled_without_ackable_suspects(window):
    w, _ = window
    # OK row — no suspects, button should be disabled.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    assert not w.grid_pane.banner._ack_selected_btn.isEnabled()
    # Suspect row — button enables.
    w.controller.select_object("VCU3-SRS", "VCU3-SRS-17")
    assert w.grid_pane.banner._ack_selected_btn.isEnabled()


# MELLON-TRACE-SRS blind tests
import re  # noqa: E402
import subprocess  # noqa: E402

from mellon.service.document import DocumentService  # noqa: E402
from tests.trace_support import (  # noqa: E402
    Modals,
    Repo,
    _qt,
    all_acks,
    basic_repo,
    click_button,
    click_row,
    context_menu,
    disk_links,
    find_row,
    grid_ids,
    grid_index,
    grid_view,
    inspector_lists,
    link_row_menu,
    open_editor,
    row_text,
    selected_ids,
)


def row_buttons(lw, i) -> list:
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    iw = lw.itemWidget(lw.item(i))
    if iw is None:
        return []
    return [b for b in iw.findChildren(QtWidgets.QAbstractButton) if b.isVisible()]


def _gutter_image(w, obj_id):
    v, idx = grid_index(w, obj_id, column=0)
    return v.viewport().grab(v.visualRect(idx)).toImage()


def test_trace_srs_84_state_gutter_marks_an_object_with_a_broken_link(tmp_path, qtbot):
    """MELLON-TRACE-SRS-84: The state gutter shall mark each object that has a broken outgoing link.
    Condition: CHILD-1's only link targets a missing object (no suspect link); CHILD-2..CHILD-5 have
    no links; CHILD-6 is selected. Expected: CHILD-1's gutter cell is drawn differently from an
    unlinked object's in a row of the same parity (setup check: two unlinked rows look alike)."""
    r = Repo(tmp_path / "repo")
    r.add_doc("PARENT", {"PARENT-1": "p1"})
    r.add_doc("CHILD", {f"CHILD-{i}": f"c{i}" for i in range(1, 7)}, parents=["PARENT"])
    r.commit("seed")
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-9", "satisfies", None)])
    r.commit("broken link")
    w = open_editor(qtbot, r.root, "CHILD", editable=False)
    assert grid_ids(w)[:6] == [f"CHILD-{i}" for i in range(1, 7)], grid_ids(w)
    click_row(qtbot, w, "CHILD-6")
    qtbot.wait(100)
    assert _gutter_image(w, "CHILD-3") == _gutter_image(w, "CHILD-5"), "setup: unlinked rows differ"
    assert _gutter_image(w, "CHILD-1") != _gutter_image(w, "CHILD-3"), "CHILD-1 (broken link) is not marked"


def test_trace_srs_91_suspect_banner_shows_for_a_broken_link_alone(tmp_path, qtbot):
    """MELLON-TRACE-SRS-91: The suspect banner shall show while the document has a suspect or broken
    outgoing link. Condition: the document's only link problem is a broken link; then the user
    removes it in the Inspector. Expected: the banner shows, and hides once the link is gone."""
    from mellon.gui.panels.grid_pane import SuspectBanner

    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-9", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-1", "satisfies", r.c0[:7])])
    r.commit("links")
    w = open_editor(qtbot, r.root, "CHILD")
    banner = w.findChild(SuspectBanner)
    assert banner is not None and banner.isVisible(), "the banner doesn't show for a broken link"
    click_row(qtbot, w, "CHILD-1")
    out, _ = inspector_lists(w)
    modals = Modals()
    link_row_menu(qtbot, out, find_row(out, "PARENT-9"), choose="Remove")
    qtbot.wait(100)
    modals.stop()
    assert disk_links(r.root, "CHILD", "CHILD-1") == {}, "setup: the broken link was not removed"
    assert not banner.isVisible(), "the banner still shows with no suspect or broken link"


def test_trace_srs_93_suspects_only_shows_filtered_objects_with_suspect_or_broken_links(tmp_path, qtbot):
    """MELLON-TRACE-SRS-93: While Suspects only is on, the grid shall show each object that passes the
    active filter and has a suspect or broken outgoing link. Condition: A (suspect, passes the filter),
    B (broken, passes), C (suspect, fails), D (OK only, passes), E (no links, passes); filter
    status = Approved; Suspects only on. Expected: exactly A and B."""
    from mellon.schema import FilterRule

    r = Repo(tmp_path / "repo")
    r.add_doc("PARENT", {"PARENT-1": "p1", "PARENT-2": "p2", "PARENT-3": "p3"})
    r.add_doc("CHILD", {
        "CHILD-1": ("A", "Requirement", "Approved"), "CHILD-2": ("B", "Requirement", "Approved"),
        "CHILD-3": ("C", "Requirement", "Draft"), "CHILD-4": ("D", "Requirement", "Approved"),
        "CHILD-5": ("E", "Requirement", "Approved")}, parents=["PARENT"])
    c0 = r.commit("seed")
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-9", "satisfies", None)])
    r.set_links("CHILD", "CHILD-3", [("PARENT", "PARENT-2", "satisfies", None)])
    r.set_links("CHILD", "CHILD-4", [("PARENT", "PARENT-3", "satisfies", c0[:7])])
    svc = DocumentService(r.root)
    svc.create_filter("CHILD", "Approved", "all", False, [FilterRule(kind="attr", attr="status", op="eq", value="Approved")])
    r.commit("links and filter")
    filt = DocumentService(r.root).get_document("CHILD").filters[0]
    w = open_editor(qtbot, r.root, "CHILD", editable=False)
    w.grid_pane.apply_filter(filt)
    qtbot.wait(100)
    assert sorted(grid_ids(w)) == ["CHILD-1", "CHILD-2", "CHILD-4", "CHILD-5"], f"setup: filter shows {grid_ids(w)}"
    click_button(qtbot, w, "Suspects only")
    assert w.grid_pane.is_suspect_only(), "setup: Suspects only is not on"
    assert sorted(grid_ids(w)) == ["CHILD-1", "CHILD-2"], f"grid shows {grid_ids(w)}"


def test_trace_srs_131_inspector_offers_no_ack_on_a_broken_link(tmp_path, qtbot):
    """MELLON-TRACE-SRS-131: If a link is broken, Mellon shall refuse to acknowledge the link.
    Condition: CHILD-1 has a broken link to PARENT-9 and a suspect link to PARENT-2. Expected: the
    broken link's row has no Ack control and its context menu no Ack action (setup check: the
    suspect row has an Ack control)."""
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-9", "satisfies", None), ("PARENT", "PARENT-2", "satisfies", None)])
    r.commit("links")
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-1")
    out, _ = inspector_lists(w)
    broken, suspect = find_row(out, "PARENT-9"), find_row(out, "PARENT-2")
    assert any(b.text().startswith("Ack") for b in row_buttons(out, suspect)), "setup: no Ack on the suspect link"
    assert not any(b.text().startswith("Ack") for b in row_buttons(out, broken)), "Ack offered on a broken link"
    menu = link_row_menu(qtbot, out, broken) or []
    assert not any(t.startswith("Ack") for t in menu), f"broken link's menu: {menu}"


def test_trace_srs_135_inspector_ack_acknowledges_that_link_alone(tmp_path, qtbot):
    """MELLON-TRACE-SRS-135: The Inspector shall offer an Ack action on each suspect outgoing link of
    the selected object, acknowledging that link alone. Condition: CHILD-1 has two suspect links;
    the user presses Ack on the link to PARENT-1. Expected: that link records PARENT-1's last change,
    the link to PARENT-2 keeps no acknowledged commit."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", None), ("PARENT", "PARENT-2", "satisfies", None)])
    r.commit("links")
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-1")
    out, _ = inspector_lists(w)
    for i in range(out.count()):
        assert [b.text() for b in row_buttons(out, i)].count("Ack") == 1, f"row {row_text(out, i)!r} has no Ack"
    ack = [b for b in row_buttons(out, find_row(out, "PARENT-1")) if b.text() == "Ack"][0]
    modals = Modals()
    QtTest.QTest.mouseClick(ack, QtCore.Qt.LeftButton)
    qtbot.wait(200)
    modals.stop()
    assert disk_links(r.root, "CHILD", "CHILD-1") == {
        ("PARENT", "PARENT-1"): ("satisfies", r.c0[:7]), ("PARENT", "PARENT-2"): ("satisfies", None)}


def test_trace_srs_152_ack_selected_acknowledges_each_suspect_link_of_the_selected_object(tmp_path, qtbot):
    """MELLON-TRACE-SRS-152: The suspect banner shall have an Ack selected action that acknowledges
    each suspect outgoing link of the selected object. Condition: CHILD-1 has two suspect links,
    CHILD-2 one; CHILD-1 selected; Ack selected pressed. Expected: both of CHILD-1's links record
    their target's last change, CHILD-2's stays without one."""
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", None), ("PARENT", "PARENT-2", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-1", "satisfies", None)])
    r.commit("links")
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-1")
    modals = Modals()
    click_button(qtbot, w, "Ack selected")
    modals.stop()
    assert disk_links(r.root, "CHILD", "CHILD-1") == {
        ("PARENT", "PARENT-1"): ("satisfies", r.c0[:7]), ("PARENT", "PARENT-2"): ("satisfies", r.c0[:7])}
    assert disk_links(r.root, "CHILD", "CHILD-2") == {("PARENT", "PARENT-1"): ("satisfies", None)}


def _three_suspect_objects(tmp_path, name="repo") -> Repo:
    r = basic_repo(tmp_path, name)
    for i in (1, 2, 3):
        r.set_links("CHILD", f"CHILD-{i}", [("PARENT", f"PARENT-{i}", "satisfies", None)])
    r.commit("links")
    return r


def _acknowledged(before: dict, after: dict) -> set:
    """Source objects with an existing link whose acknowledged commit changed to a value."""
    return {(k[0], k[1]) for k, v in after.items() if k in before and v and before[k] != v}


def test_trace_srs_154_ack_selected_after_selecting_everything_acknowledges_one_object_at_most(tmp_path, qtbot):
    """MELLON-TRACE-SRS-154: The Document Editor shall not offer an action that acknowledges the
    suspect links of the whole document at once. Condition: each of CHILD-1..3 has a suspect link;
    the user tries to select every object (Ctrl+A, then Shift+click on CHILD-3) and presses Ack
    selected, which MELLON-TRACE-SRS-153's rationale calls acknowledging a whole document in one
    action. Expected: no more than one object's links acknowledged."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    r = _three_suspect_objects(tmp_path)
    w = open_editor(qtbot, r.root, "CHILD")
    click_row(qtbot, w, "CHILD-1")
    v = grid_view(w)
    QtTest.QTest.keyClick(v, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
    _, idx = grid_index(w, "CHILD-3")
    QtTest.QTest.mouseClick(v.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.ShiftModifier, v.visualRect(idx).center())
    qtbot.wait(100)
    before = all_acks(r.root)
    buttons = [b for b in w.findChildren(QtWidgets.QAbstractButton) if b.text() == "Ack selected" and b.isVisible()]
    modals = Modals()
    if buttons and buttons[0].isEnabled():
        QtTest.QTest.mouseClick(buttons[0], QtCore.Qt.LeftButton)
        qtbot.wait(200)
    modals.stop()
    acked = _acknowledged(before, all_acks(r.root))
    assert len(acked) <= 1, f"one action acknowledged the links of {sorted(acked)} (selected {selected_ids(w)})"


def _close(w) -> None:
    from shiboken6 import isValid

    if isValid(w):
        w.close()
        if isValid(w):
            w.deleteLater()


def _block_external_programs(monkeypatch) -> list:
    """Stub what could start another program (draw.io, tectonic, a viewer); record each attempt."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    attempts = []

    def blocked(*a, **k):
        attempts.append(repr(a[:1])[:80])
        raise OSError("starting programs is blocked by the test")

    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(os, "startfile", blocked, raising=False)
    monkeypatch.setattr(QtCore.QProcess, "startDetached", staticmethod(lambda *a, **k: (attempts.append("QProcess"), False)[1]))
    monkeypatch.setattr(QtCore.QProcess, "start", lambda self, *a, **k: attempts.append("QProcess"))
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", staticmethod(lambda *a, **k: (attempts.append("openUrl"), False)[1]))
    return attempts


def _grid_menu(qtbot, w, obj_id, choose=None):
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    v, idx = grid_index(w, obj_id)
    pos = v.visualRect(idx).center()

    def fire():
        if v.contextMenuPolicy() == QtCore.Qt.CustomContextMenu:
            v.customContextMenuRequested.emit(pos)
        else:
            ev = QtGui.QContextMenuEvent(QtGui.QContextMenuEvent.Mouse, pos, v.viewport().mapToGlobal(pos))
            QtWidgets.QApplication.sendEvent(v.viewport(), ev)

    return context_menu(qtbot, fire, choose)


def _set_selection(qtbot, w, selected: bool) -> None:
    if selected:
        click_row(qtbot, w, "CHILD-1")
    else:
        v = grid_view(w)
        v.clearSelection()
        v.selectionModel().clearCurrentIndex()
        qtbot.wait(50)


def _row_target(lw, i) -> str | None:
    m = re.search(r"PARENT-\d+", row_text(lw, i))
    return m.group(0) if m else None


def _controls(qtbot, w) -> list:
    """Every action of the window's menus and toolbars, its visible push buttons (banners), and the
    items of the grid's and the Inspector's context menus and link-row buttons."""
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    for m in w.findChildren(QtWidgets.QMenu):
        m.aboutToShow.emit()
    qtbot.wait(50)
    out = set()
    for a in w.findChildren(QtGui.QAction):
        if not a.isSeparator() and a.menu() is None and a.text().strip():
            out.add(("action", a.text()))
    for b in w.findChildren(QtWidgets.QPushButton):
        if b.isVisible() and b.text().strip():
            out.add(("button", b.text()))
    lw, _ = inspector_lists(w)
    for i in range(lw.count()):
        target = _row_target(lw, i)
        if target is None:
            continue
        for b in row_buttons(lw, i):
            out.add(("row-button", target, b.text()))
        for t in link_row_menu(qtbot, lw, i) or []:
            out.add(("row-menu", target, t))
    if selected_ids(w):
        for t in _grid_menu(qtbot, w, "CHILD-1") or []:
            out.add(("grid-menu", t))
    return sorted(out)


def _fire(qtbot, w, control) -> None:
    QtCore, QtGui, QtWidgets, QtTest = _qt()
    kind = control[0]
    if kind == "action":
        for m in w.findChildren(QtWidgets.QMenu):
            m.aboutToShow.emit()
        acts = [a for a in w.findChildren(QtGui.QAction) if a.text() == control[1] and a.menu() is None]
        if acts and acts[0].isEnabled():
            acts[0].trigger()
    elif kind == "button":
        bs = [b for b in w.findChildren(QtWidgets.QPushButton) if b.isVisible() and b.text() == control[1]]
        if bs and bs[0].isEnabled():
            QtTest.QTest.mouseClick(bs[0], QtCore.Qt.LeftButton)
    elif kind in ("row-button", "row-menu"):
        lw, _ = inspector_lists(w)
        rows = [i for i in range(lw.count()) if _row_target(lw, i) == control[1]]
        if rows and kind == "row-button":
            bs = [b for b in row_buttons(lw, rows[0]) if b.text() == control[2]]
            if bs and bs[0].isEnabled():
                QtTest.QTest.mouseClick(bs[0], QtCore.Qt.LeftButton)
        elif rows:
            link_row_menu(qtbot, lw, rows[0], choose=control[2])
    elif kind == "grid-menu":
        _grid_menu(qtbot, w, "CHILD-1", choose=control[1])
    qtbot.wait(150)


def _multi_link_template(tmp_path) -> Repo:
    r = basic_repo(tmp_path, "template")
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", None), ("PARENT", "PARENT-2", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-2", "satisfies", None)])
    r.set_links("CHILD", "CHILD-3", [("PARENT", "PARENT-3", "satisfies", None)])
    r.commit("links")
    return r


@pytest.mark.parametrize("selected", [False, True], ids=["no-selection", "one-object-selected"])
def test_trace_srs_154_no_control_acknowledges_more_than_one_objects_links(tmp_path, qtbot, monkeypatch, selected):
    """MELLON-TRACE-SRS-154: The Document Editor shall not offer an action that acknowledges the
    suspect links of the whole document at once. Condition: suspect links on CHILD-1 (two), CHILD-2
    and CHILD-3; every menu and toolbar action, banner button, grid and Inspector context-menu item
    and link-row button is triggered on its own, in a fresh editable window, with no object or with
    CHILD-1 selected, dialogs answered with accept. Expected: none acknowledges links of more than one
    object. Setup check: with CHILD-1 selected, some control does acknowledge CHILD-1's links."""
    _block_external_programs(monkeypatch)
    template = _multi_link_template(tmp_path)
    w0 = open_editor(qtbot, template.root, "CHILD", register=False)
    _set_selection(qtbot, w0, selected)
    controls = _controls(qtbot, w0)
    _close(w0)
    assert ("button", "Ack selected") in controls, f"setup: controls found: {controls}"
    results = {}
    for n, control in enumerate(controls):
        root = tmp_path / f"run{n}"
        shutil.copytree(template.root, root)
        before = all_acks(root)
        w = open_editor(qtbot, root, "CHILD", register=False)
        _set_selection(qtbot, w, selected)
        modals = Modals(accept_dialogs=True)
        try:
            _fire(qtbot, w, control)
        finally:
            _close(w)
            qtbot.wait(50)
            modals.stop()
        results[control] = _acknowledged(before, all_acks(root))
    too_many = {c: sorted(v) for c, v in results.items() if len(v) > 1}
    assert too_many == {}, f"controls that acknowledged more than one object's links: {too_many}"
    if selected:
        assert any(v == {("CHILD", "CHILD-1")} for v in results.values()), "setup: no control acknowledged CHILD-1"
