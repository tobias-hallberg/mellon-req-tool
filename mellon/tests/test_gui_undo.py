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
