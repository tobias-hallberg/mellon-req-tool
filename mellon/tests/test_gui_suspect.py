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
