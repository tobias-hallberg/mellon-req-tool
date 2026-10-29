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
