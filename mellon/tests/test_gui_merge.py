"""Tests for the Project Explorer's merge banner and the Resolve merge dialog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from mellon import (
    create_object,
    delete_object,
    load_document,
    set_attribute,
    set_description,
)
from tests.test_merge import (  # noqa: F401 — `repo` is a fixture
    OBJ1,
    SRS,
    _both_edit,
    _branch,
    _clash,
    _commit,
    _conflicts,
    _merge,
    repo,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def explorer(qtbot):
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    w = ProjectExplorerWindow()
    qtbot.addWidget(w)
    return w


def _dialog(qtbot, repo: Path):
    from mellon import merge
    from mellon.gui.dialogs.merge_dialog import MergeDialog

    dlg = MergeDialog(repo, merge.plan(repo))
    qtbot.addWidget(dlg)
    return dlg


def _texts(dlg) -> str:
    return "\n".join(label.text() for label in dlg.findChildren(QLabel))


def _clash_and_both_edited(repo: Path) -> None:
    """VCU3-SRS-34 created on both branches (b later), and VCU3-SRS-1's text
    and status changed on both."""
    srs = repo / SRS
    _branch(repo, "a")
    a1 = create_object(srs, parent_id="VCU3-SRS-14", description="A first")
    set_attribute(srs, a1, "type", "Requirement")
    set_description(srs, "VCU3-SRS-1", "Mine.")
    set_attribute(srs, "VCU3-SRS-1", "status", "Approved")
    _commit(repo, "a", when=1_700_000_000)
    _branch(repo, "b")
    b1 = create_object(srs, parent_id="VCU3-SRS-19", description="B only")
    set_attribute(srs, b1, "type", "Requirement")
    set_description(srs, "VCU3-SRS-1", "Theirs.")
    set_attribute(srs, "VCU3-SRS-1", "status", "Reviewed")
    _commit(repo, "b", when=1_700_000_100)
    _merge(repo, "a", "b")


def test_banner_follows_the_merge(explorer, repo: Path):
    explorer.open_repo(repo)
    assert explorer.merge_banner.isHidden()

    _clash(repo)
    explorer._refresh_documents()  # F5
    assert not explorer.merge_banner.isHidden()
    assert "file(s) conflict" in explorer.merge_banner._label.text()
    assert explorer.merge_banner._button.isVisibleTo(explorer.merge_banner)

    from mellon import merge

    merge.resolve(repo)
    explorer._refresh_documents()
    assert "No conflicts left" in explorer.merge_banner._label.text()
    assert not explorer.merge_banner._button.isVisibleTo(explorer.merge_banner)


def test_dialog_renumbers_without_asking_and_merges_by_hand(qtbot, repo: Path):
    _clash_and_both_edited(repo)
    dlg = _dialog(qtbot, repo)
    shown = _texts(dlg)
    # The clash is a line, not a choice; Resolve works at once.
    assert "VCU3-SRS-34 on b was created later" in shown and "VCU3-SRS-35" in shown
    assert dlg._resolve_btn.isEnabled()
    # The requirement's block shows what it traces to.
    syrs42 = load_document(repo / "VCU3-SyRS").objects["VCU3-SyRS-42"].description
    assert "satisfies VCU3-SyRS-42: " + " ".join(syrs42.split()) in shown
    # Defaults: mine.
    assert dlg.edits() == {OBJ1: {"description": "Mine.", "attr:status": "ours"}}

    # "Use theirs" fills the result; then edit it and take their status.
    text = dlg._fields[OBJ1]["description"]
    [use_theirs] = [b for b in dlg.findChildren(QPushButton) if b.text() == "Use theirs"]
    use_theirs.click()
    assert text.toPlainText() == "Theirs."
    text.setPlainText("Mine and theirs.")
    dlg._fields[OBJ1]["attr:status"].setCurrentIndex(1)
    dlg._resolve()

    assert dlg.result() == dlg.DialogCode.Accepted
    assert _conflicts(repo) == []
    doc = load_document(repo / SRS)
    assert doc.objects["VCU3-SRS-1"].description == "Mine and theirs."
    assert doc.objects["VCU3-SRS-1"].attrs["status"] == "Reviewed"
    assert doc.objects["VCU3-SRS-34"].description == "A first"
    assert doc.objects["VCU3-SRS-35"].description == "B only"


def test_dialog_keep_checkbox_for_a_deleted_object(qtbot, repo: Path):
    _both_edit(
        repo,
        lambda r: set_description(r / SRS, "VCU3-SRS-10", "Edited on a."),
        lambda r: delete_object(r / SRS, "VCU3-SRS-10", force=True),
    )
    dlg = _dialog(qtbot, repo)
    [(path, keep)] = dlg._keeps.items()
    assert keep.isChecked() and "deleted on b" in keep.text()
    keep.setChecked(False)
    assert dlg.delete() == {path}
    dlg._resolve()
    assert "VCU3-SRS-10" not in load_document(repo / SRS).objects


def test_explorer_resolve_refreshes_after_the_dialog(explorer, repo: Path, monkeypatch):
    from mellon.gui import project_explorer_window as pew

    _clash(repo)
    explorer.open_repo(repo)
    assert not explorer.merge_banner.isHidden()

    def accept(dlg):
        dlg._resolve()
        return dlg.result()

    monkeypatch.setattr(pew.MergeDialog, "exec", accept)
    explorer._resolve_merge()

    assert _conflicts(repo) == []
    assert "No conflicts left" in explorer.merge_banner._label.text()
    assert "Make the merge commit" in explorer._status.currentMessage()
    # The renumbered object loads with the rest.
    assert "VCU3-SRS-36" in explorer.controller.document(SRS).objects
