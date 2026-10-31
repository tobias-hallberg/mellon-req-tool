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


def _show_history_tab(w) -> None:
    w.right_pane._tabs.setCurrentWidget(w.right_pane.history)
    w.show()


def test_history_tab_populates_on_selection(window) -> None:
    w, _ = window
    history = w.right_pane.history
    _show_history_tab(w)
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
    _show_history_tab(w)
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    assert history._table.rowCount() > 0
    assert not history._view_button.isEnabled()
    history._table.selectRow(0)
    assert history._view_button.isEnabled()


# --- HistoryTab loads only while visible ----------------------------------


@pytest.fixture
def history_calls(window, monkeypatch) -> list[str]:
    w, _ = window
    calls: list[str] = []
    real = w.controller.object_history

    def counting(doc_id, obj_id, *args, **kwargs):
        calls.append(obj_id)
        return real(doc_id, obj_id, *args, **kwargs)

    monkeypatch.setattr(w.controller, "object_history", counting)
    return calls


def test_hidden_history_tab_walks_no_history(window, history_calls) -> None:
    w, _ = window
    w.show()  # the Inspector tab is the current one
    assert not w.right_pane.history.isVisible()
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-1")
    assert history_calls == []


def test_showing_history_tab_loads_the_current_object(window, history_calls) -> None:
    w, _ = window
    w.show()
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-1")
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    w.right_pane._tabs.setCurrentWidget(w.right_pane.history)
    assert history_calls == ["VCU3-SyRS-42"]
    assert w.right_pane.history._table.rowCount() == 5


def test_visible_history_tab_follows_selection(window, history_calls) -> None:
    w, _ = window
    _show_history_tab(w)
    history_calls.clear()
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    assert history_calls == ["VCU3-SyRS-42"]
    assert w.right_pane.history._table.rowCount() == 5


def test_reshowing_an_unchanged_history_tab_loads_nothing(window, history_calls) -> None:
    w, _ = window
    _show_history_tab(w)
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    history_calls.clear()
    w.right_pane._tabs.setCurrentWidget(w.right_pane.inspector)
    w.right_pane._tabs.setCurrentWidget(w.right_pane.history)
    assert history_calls == []


def test_edit_walks_no_history(window, history_calls) -> None:
    """An uncommitted edit can't change history, even with the tab showing."""
    w, _ = window
    _show_history_tab(w)
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    history_calls.clear()
    grid = w.grid_pane._model
    index = grid.index(grid.row_for_obj_id("VCU3-SyRS-42"), grid.column_index("heading_or_text"))
    assert grid.setData(index, "An uncommitted edit.")
    assert history_calls == []


def test_commit_while_hidden_shows_when_the_tab_opens(window) -> None:
    w, _ = window
    history = w.right_pane.history
    w.show()
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    w.controller.require_service().set_description("VCU3-SyRS", "VCU3-SyRS-42", "Changed.")
    w.controller.commit_document("VCU3-SyRS", "Commit while hidden")
    w.right_pane._tabs.setCurrentWidget(history)
    message = history._table.item(0, history.COLUMNS.index("Message")).text()
    assert message == "Commit while hidden"


def test_commit_from_a_git_client_shows_on_window_activation(window) -> None:
    from PySide6.QtCore import QEvent

    w, root = window
    history = w.right_pane.history
    _show_history_tab(w)
    w.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    path = root / "VCU3-SyRS" / "objects" / "VCU3-SyRS-42.xml"
    path.write_text(path.read_text(encoding="utf-8").replace("<description>", "<description>x "), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    repo.index.add("VCU3-SyRS/objects/VCU3-SyRS-42.xml")
    repo.index.write()
    sig = pygit2.Signature("t", "t@example.invalid")
    repo.create_commit("HEAD", sig, sig, "From a git client", repo.index.write_tree(), [repo.head.target])

    w.event(QEvent(QEvent.Type.WindowActivate))
    message = history._table.item(0, history.COLUMNS.index("Message")).text()
    assert message == "From a git client"


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
