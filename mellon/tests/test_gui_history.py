"""Tests for the right-pane tabs: RightPane, HistoryTab, ObjectAtCommitDialog,
and the controller's `object_history` / `object_at_commit`.

Runs against the committed `req-test/` fixture in offscreen Qt.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pygit2
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


# --- RightPane shape -----------------------------------------------------


def test_right_pane_has_inspector_and_history_tabs(window) -> None:
    w, _ = window
    rp = w.right_pane
    assert rp._tabs.count() == 2
    assert rp._tabs.tabText(0) == "Inspector"
    assert rp._tabs.tabText(1) == "History"
    # Inspector tab is the default.
    assert rp._tabs.currentIndex() == 0


def test_right_pane_inspector_alias_points_at_tab(window) -> None:
    w, _ = window
    assert w.inspector is w.right_pane.inspector


def test_outline_pane_toggle_hides_pane(window) -> None:
    w, _ = window
    assert not w.outline_pane.isHidden()
    assert w._toggle_left_btn.isChecked()
    w._toggle_left_btn.setChecked(False)
    assert w.outline_pane.isHidden()
    w._toggle_left_btn.setChecked(True)
    assert not w.outline_pane.isHidden()


def test_right_pane_toggle_hides_pane(window) -> None:
    w, _ = window
    assert not w.right_pane.isHidden()
    assert w._toggle_right_btn.isChecked()
    w._toggle_right_btn.setChecked(False)
    assert w.right_pane.isHidden()
    w._toggle_right_btn.setChecked(True)
    assert not w.right_pane.isHidden()


# --- HistoryTab population from controller --------------------------------


def test_history_tab_populates_on_selection(window) -> None:
    w, _ = window
    history = w.right_pane.history
    # Selecting an object the fixture has touched in four commits.
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    table = history._table
    # Fixture: VCU3-SyRS-42 has five commits in its file history.
    assert table.rowCount() == 5
    messages = [table.item(r, history.COLUMNS.index("Message")).text() for r in range(5)]
    assert messages == [
        "Add req-type attribute to VCU3-SyRS and VCU3-SRS",
        "Migrate type schema: replace domain types with Requirement",
        "Tighten SyRS-42 to 300ms",
        "Fixed VCU3-SyRS-42",
        "Added SyRS.",
    ]
    # All five rows have all columns populated.
    for r in range(5):
        for c in range(len(history.COLUMNS)):
            assert table.item(r, c) is not None


def test_history_tab_empty_state_until_selection(qtbot) -> None:
    from mellon.gui.controller import RepoController
    from mellon.gui.panels.history_tab import HistoryTab

    controller = RepoController()
    tab = HistoryTab(controller)
    qtbot.addWidget(tab)
    assert tab._stack.currentWidget() is tab._empty
    assert not tab._view_button.isEnabled()


def test_history_tab_view_button_enables_on_row_selection(window) -> None:
    w, _ = window
    history = w.right_pane.history
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    assert history._table.rowCount() > 0
    assert not history._view_button.isEnabled()
    history._table.selectRow(0)
    assert history._view_button.isEnabled()


# --- controller methods ---------------------------------------------------


def test_object_history_returns_expected_commits(window) -> None:
    w, _ = window
    commits = w.controller.object_history("VCU3-SyRS", "VCU3-SyRS-42")
    assert [c["message"] for c in commits] == [
        "Add req-type attribute to VCU3-SyRS and VCU3-SRS",
        "Migrate type schema: replace domain types with Requirement",
        "Tighten SyRS-42 to 300ms",
        "Fixed VCU3-SyRS-42",
        "Added SyRS.",
    ]
    for c in commits:
        assert len(c["short_hash"]) == 7
        assert isinstance(c["timestamp"], int)
        assert c["author"]


def test_object_history_includes_figure_only_commits(window, tmp_path) -> None:
    w, _ = window
    svc = w.controller.require_service()
    svg = tmp_path / "fig.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    svc.set_figure("VCU3-SyRS", "VCU3-SyRS-42", svg)
    svc.commit_document("VCU3-SyRS", "Draw figure")
    commits = w.controller.object_history("VCU3-SyRS", "VCU3-SyRS-42")
    assert commits[0]["message"] == "Draw figure"
    assert len(commits) == 6


def test_object_history_unknown_object_returns_empty(window) -> None:
    w, _ = window
    commits = w.controller.object_history("VCU3-SyRS", "DOES-NOT-EXIST")
    assert commits == []


def test_object_at_commit_returns_historical_description(
    window, fixture_commit
) -> None:
    w, dst = window
    # The same fixture pattern test_mcp_git uses: SyRS-47 was tightened in
    # "Tighten VCU3-SyRS-47"; its parent commit must still have the original
    # text.
    repo = pygit2.Repository(str(dst))
    tight = fixture_commit(repo, "Tighten VCU3-SyRS-47")
    parent_short = str(tight.parents[0].id)[:7]

    snapshot = w.controller.object_at_commit(
        "VCU3-SyRS", "VCU3-SyRS-47", parent_short
    )
    assert snapshot.short_hash == parent_short
    assert snapshot.obj_id == "VCU3-SyRS-47"
    assert "50 ms" not in snapshot.object.description
    # Outline path on the historical manifest still resolves.
    assert "VCU3-SyRS-47" in snapshot.outline_path

    head = w.controller.object_at_commit("VCU3-SyRS", "VCU3-SyRS-47", "HEAD")
    assert "50 ms detection budget" in head.object.description


# --- ObjectAtCommitDialog render ------------------------------------------


def test_object_at_commit_dialog_renders_snapshot(window, qtbot) -> None:
    from mellon.gui.dialogs.object_at_commit import ObjectAtCommitDialog

    w, _ = window
    snapshot = w.controller.object_at_commit("VCU3-SyRS", "VCU3-SyRS-47", "HEAD")
    dialog = ObjectAtCommitDialog(snapshot)
    qtbot.addWidget(dialog)
    assert snapshot.short_hash in dialog.windowTitle()
    assert dialog._description.toPlainText() == snapshot.object.description
    assert dialog._description.isReadOnly()
    # Attribute rows reflect the historical attrs map.
    assert dialog._attrs_form.rowCount() == len(snapshot.object.attrs)
