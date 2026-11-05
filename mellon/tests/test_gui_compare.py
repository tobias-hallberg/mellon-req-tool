"""Tests for the Compare dialog (`gui/dialogs/compare_dialog.py`) and the
two ways into it: the Document Editor menu and the History tab.

Specified in MELLON-SRS, under "Compare dialog"; MELLON-STS maps each test
to its requirement. Each test builds a small repo with the editor:

    seed      SPEC-0 "Heading" over SPEC-1..3      tagged SPEC-issue-1
    edits     SPEC-1 reworded, SPEC-2 given a rationale,
              SPEC-3 deleted, SPEC-4 added          (HEAD)
"""

from __future__ import annotations

import os
from pathlib import Path

import pygit2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from mellon import editor  # noqa: E402
from mellon.diff import LATEST, WORKING_COPY, Change  # noqa: E402
from mellon.git_ops import commit, create_issue_tag  # noqa: E402
from mellon.schema import Table, TableCell, TableColumn, TableRow  # noqa: E402


_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
    b'<rect width="40" height="20" fill="red"/></svg>\n'
)


def _tag(root: Path, issue: str) -> None:
    repo = pygit2.Repository(str(root))
    create_issue_tag(repo, "SPEC", issue, repo.head.peel(pygit2.Commit).id)


def _build(root: Path, *, issue: bool = True) -> Path:
    pygit2.init_repository(str(root))
    folder = editor.create_document(root, "SPEC", title="Spec")
    editor.create_object(folder, None, "Heading")
    editor.set_attribute(folder, "SPEC-0", "type", "Heading")
    editor.create_object(folder, "SPEC-0", "The VCU shall stop.")
    editor.create_object(folder, "SPEC-0", "Requirement 2.")
    editor.create_object(folder, "SPEC-0", "Requirement 3.")
    commit(root, "seed")
    if issue:
        _tag(root, "1")
    editor.set_description(folder, "SPEC-1", "The VCU must stop.")
    editor.set_attribute(folder, "SPEC-2", "rationale", "Because.")
    # Created before the delete, or the new object would reuse SPEC-3.
    editor.create_object(folder, "SPEC-0", "Requirement 4.")
    editor.delete_object(folder, "SPEC-3")
    commit(root, "edits")
    return folder


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _build(root)
    return root


def _window(qtbot, root: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    w = DocumentEditorWindow("SPEC", DocumentService(root, cache=True))
    qtbot.addWidget(w)
    return w


@pytest.fixture
def window(qtbot, repo):
    return _window(qtbot, repo)


def _wait(qtbot, dlg) -> None:
    qtbot.waitUntil(lambda: not dlg.is_busy(), timeout=15000)


@pytest.fixture
def dialog(qtbot, window):
    dlg = window.open_compare()
    _wait(qtbot, dlg)
    return dlg


def _rows(dlg) -> list[tuple[str, str, str]]:
    out = []
    for i in range(dlg.list.topLevelItemCount()):
        item = dlg.list.topLevelItem(i)
        if not item.isHidden():
            out.append((item.text(0), item.text(1), item.text(2)))
    return out


def _select(dlg, obj_id: str):
    for i in range(dlg.list.topLevelItemCount()):
        item = dlg.list.topLevelItem(i)
        if item.text(0) == obj_id:
            dlg.list.setCurrentItem(item)
            return item
    raise AssertionError(f"{obj_id} not listed")


def _choose(dlg, combo, ref: str) -> None:
    combo.setCurrentIndex(combo.findData(ref))


# --- opening the dialog (MELLON-SRS-6..8) -----------------------------------


def test_menu_compare_action_opens_the_dialog(qtbot, window) -> None:
    from mellon.gui.dialogs.compare_dialog import CompareDialog

    act = window._compare_act
    assert act in window._main_menu.actions()
    assert act.text().replace("&", "") == "Compare…"
    act.trigger()
    dlg = window._compare_dialog
    assert isinstance(dlg, CompareDialog)
    assert dlg.isVisible()
    assert dlg.windowTitle() == "Compare — SPEC"
    _wait(qtbot, dlg)


def test_compare_is_available_in_read_only_mode(qtbot, window) -> None:
    assert window._read_only
    window._refresh_action_states()
    assert window._compare_act.isEnabled()
    window._set_read_only(False)
    window._set_read_only(True)
    assert window._compare_act.isEnabled()
    dlg = window.open_compare()
    _wait(qtbot, dlg)
    assert dlg.diff is not None and dlg.diff.changes


def test_opening_again_raises_the_same_dialog(qtbot, window) -> None:
    first = window.open_compare()
    _wait(qtbot, first)
    assert window.open_compare() is first


def test_history_tab_compares_a_commit_with_the_latest(qtbot, window, repo) -> None:
    history = window.right_pane.history
    window.right_pane._tabs.setCurrentWidget(history)
    window.show()
    window.controller.select_object("SPEC", "SPEC-2")
    assert not history._compare_button.isEnabled()
    # Newest first: "edits", then "seed".
    history._table.selectRow(1)
    assert history._compare_button.isEnabled()
    history._compare_button.click()
    dlg = window._compare_dialog
    _wait(qtbot, dlg)
    seed = pygit2.Repository(str(repo)).revparse_single("HEAD~1")
    assert dlg.ref(dlg.from_combo) == str(seed.id)
    assert dlg.ref(dlg.to_combo) == LATEST
    assert [r[0] for r in _rows(dlg)] == ["SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"]


# --- choosing versions (MELLON-SRS-10..17) ------------------------------------


def test_versions_offered(dialog) -> None:
    refs = [dialog.from_combo.itemData(i) for i in range(dialog.from_combo.count())]
    labels = [dialog.from_combo.itemText(i) for i in range(dialog.from_combo.count())]
    assert refs[0] == WORKING_COPY
    assert refs[1] == LATEST
    assert "SPEC-issue-1" in refs
    assert any(label.startswith("Branch ") for label in labels)
    assert sum(1 for label in labels if label.endswith("  seed") or label.endswith("  edits")) == 2
    to_refs = [dialog.to_combo.itemData(i) for i in range(dialog.to_combo.count())]
    assert to_refs == refs


def test_typed_version(qtbot, dialog, repo) -> None:
    seed = str(pygit2.Repository(str(repo)).revparse_single("HEAD~1").id)[:7]
    dialog.from_combo.setEditText(seed)
    assert dialog.ref(dialog.from_combo) == seed
    dialog.compare()
    _wait(qtbot, dialog)
    assert dialog.diff.ref_a == seed
    assert [r[0] for r in _rows(dialog)] == ["SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"]


def test_working_copy_as_to(qtbot, dialog, repo) -> None:
    editor.set_description(repo / "SPEC", "SPEC-4", "Requirement 4, not committed.")
    _choose(dialog, dialog.from_combo, LATEST)
    _choose(dialog, dialog.to_combo, WORKING_COPY)
    dialog.compare()
    _wait(qtbot, dialog)
    assert _rows(dialog) == [("SPEC-4", "Modified", "Requirement 4, not committed.")]


def test_opens_with_latest_issue_to_latest_commit(dialog) -> None:
    assert dialog.ref(dialog.from_combo) == "SPEC-issue-1"
    assert dialog.ref(dialog.to_combo) == LATEST
    assert dialog.diff.ref_a == "SPEC-issue-1"


def test_opens_with_the_issue_before_when_the_latest_commit_is_an_issue(
    qtbot, repo
) -> None:
    _tag(repo, "2")
    w = _window(qtbot, repo)
    dlg = w.open_compare()
    _wait(qtbot, dlg)
    assert dlg.ref(dlg.from_combo) == "SPEC-issue-1"
    assert dlg.ref(dlg.to_combo) == LATEST


def test_without_an_issue_prompts_for_from(qtbot, tmp_path) -> None:
    root = tmp_path / "no-issue"
    root.mkdir()
    _build(root, issue=False)
    w = _window(qtbot, root)
    dlg = w.open_compare()
    assert not dlg.is_busy()
    assert dlg.ref(dlg.from_combo) is None
    assert dlg.ref(dlg.to_combo) == LATEST
    assert dlg.diff is None
    assert dlg.empty_label.text() == dlg.PROMPT
    assert dlg._stack.currentWidget() is dlg.empty_label
    # Choosing a version then compares.
    _choose(dlg, dlg.from_combo, LATEST)
    dlg.compare()
    _wait(qtbot, dlg)
    assert dlg.diff is not None


def test_swap(qtbot, dialog) -> None:
    dialog.swap_button.click()
    _wait(qtbot, dialog)
    assert dialog.ref(dialog.from_combo) == LATEST
    assert dialog.ref(dialog.to_combo) == "SPEC-issue-1"
    kinds = {r[0]: r[1] for r in _rows(dialog)}
    assert kinds["SPEC-3"] == "Added"
    assert kinds["SPEC-4"] == "Deleted"


def test_unresolvable_version_is_named_and_the_result_kept(qtbot, dialog) -> None:
    before = _rows(dialog)
    previous = dialog.diff
    summary = dialog.summary_label.text()
    dialog.from_combo.setEditText("no-such-version")
    dialog.compare()
    _wait(qtbot, dialog)
    assert dialog.message_label.isVisible()
    assert "no-such-version" in dialog.message_label.text()
    assert dialog.diff is previous
    assert _rows(dialog) == before
    # Still describes the result shown, not the versions that failed.
    assert dialog.summary_label.text() == summary
    # A good comparison clears the message.
    _choose(dialog, dialog.from_combo, "SPEC-issue-1")
    dialog.compare()
    _wait(qtbot, dialog)
    assert not dialog.message_label.isVisible()


# --- listing changes (MELLON-SRS-19..26) --------------------------------------


def test_rows_in_outline_order_with_change_and_text(dialog) -> None:
    assert _rows(dialog) == [
        ("SPEC-1", "Modified", "The VCU must stop."),
        ("SPEC-2", "Modified", "Requirement 2."),
        # Deleted: shown where it stood, with its text at From.
        ("SPEC-3", "Deleted", "Requirement 3."),
        ("SPEC-4", "Added", "Requirement 4."),
    ]


def test_summary_counts(dialog) -> None:
    text = dialog.summary_label.text()
    assert "1 added, 1 deleted, 2 modified" in text
    assert "Issue 1" in text and "Latest commit" in text


def test_no_differences_says_so(qtbot, dialog) -> None:
    _choose(dialog, dialog.from_combo, LATEST)
    _choose(dialog, dialog.to_combo, LATEST)
    dialog.compare()
    _wait(qtbot, dialog)
    assert dialog._stack.currentWidget() is dialog.empty_label
    assert dialog.empty_label.text().startswith("No differences: SPEC is the same at")
    assert not dialog.copy_button.isEnabled()


def test_moved_and_link_reviewed_rows(qtbot, repo) -> None:
    folder = repo / "SPEC"
    editor.move_object(folder, "SPEC-2", to_top=True)
    links = folder / "links"
    links.mkdir(exist_ok=True)
    (links / "SPEC-4.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<links source="SPEC-4">\n'
        '  <link target-doc="SPEC" target-id="SPEC-1" type="refines" ack="abc1234"/>\n'
        "</links>\n",
        encoding="utf-8",
    )
    commit(repo, "move and link")
    (links / "SPEC-4.xml").write_text(
        (links / "SPEC-4.xml").read_text(encoding="utf-8").replace("abc1234", "def5678"),
        encoding="utf-8",
    )
    commit(repo, "review link")
    w = _window(qtbot, repo)
    dlg = w.open_compare("HEAD~1", LATEST)
    _wait(qtbot, dlg)
    assert _rows(dlg) == [("SPEC-4", "Link reviewed", "Requirement 4.")]
    assert "1 link reviewed" in dlg.summary_label.text()
    # New refs reuse the open dialog.
    assert w.open_compare("HEAD~2", "HEAD~1") is dlg
    _wait(qtbot, dlg)
    assert ("SPEC-2", "Moved", "Requirement 2.") in _rows(dlg)
    assert "1 moved" in dlg.summary_label.text()


def test_filters_hide_rows_by_change(qtbot, dialog) -> None:
    dialog.filter_boxes[Change.MODIFIED].setChecked(False)
    assert [r[0] for r in _rows(dialog)] == ["SPEC-3", "SPEC-4"]
    dialog.filter_boxes[Change.ADDED].setChecked(False)
    dialog.filter_boxes[Change.DELETED].setChecked(False)
    assert _rows(dialog) == []
    assert dialog.details.toPlainText() == ""
    for box in dialog.filter_boxes.values():
        box.setChecked(True)
    assert len(_rows(dialog)) == 4


# --- showing an object's changes (MELLON-SRS-28..33) --------------------------


def test_modified_object_shows_each_field_at_from_and_to(dialog) -> None:
    _select(dialog, "SPEC-2")
    text = dialog.details.toPlainText()
    assert "SPEC-2 — Modified" in text
    assert "rationale" in text
    # Set on one side only.
    assert "(not set)" in text and "Because." in text


def test_description_change_highlights_words(dialog) -> None:
    _select(dialog, "SPEC-1")
    page = dialog.details.toHtml()
    plain = dialog.details.toPlainText()
    assert "The VCU shall stop." in plain and "The VCU must stop." in plain
    assert "line-through" in page and "underline" in page


def test_words_html_marks_only_changed_words() -> None:
    from mellon.gui.dialogs.compare_dialog import words_html
    from mellon.gui.theme import tokens_for

    tokens = tokens_for("light")
    a = words_html("The VCU shall stop.", "The VCU must stop.", "a", tokens)
    b = words_html("The VCU shall stop.", "The VCU must stop.", "b", tokens)
    assert a.startswith("The VCU <span") and "line-through\">shall</span>" in a
    assert "must" not in a
    assert "underline\">must</span>" in b and "shall" not in b


def test_added_and_deleted_objects_show_all_fields(dialog) -> None:
    _select(dialog, "SPEC-3")
    text = dialog.details.toPlainText()
    assert "SPEC-3 — Deleted" in text
    assert "As it is at From" in text
    assert "Requirement 3." in text
    assert "under SPEC-0, after SPEC-2" in text
    _select(dialog, "SPEC-4")
    text = dialog.details.toPlainText()
    assert "As it is at To" in text and "Requirement 4." in text


def test_table_and_figure_changes_shown_at_both_versions(qtbot, repo, tmp_path) -> None:
    folder = repo / "SPEC"
    table = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("speed"), TableCell("10 km/h"))),),
    )
    editor.set_table(folder, "SPEC-2", table)
    svg = tmp_path / "fig.svg"
    svg.write_bytes(_SVG)
    editor.set_figure(folder, "SPEC-2", svg)
    commit(repo, "table and figure")
    w = _window(qtbot, repo)
    dlg = w.open_compare("HEAD~1", LATEST)
    _wait(qtbot, dlg)
    _select(dlg, "SPEC-2")
    text = dlg.details.toPlainText()
    assert "(no table)" in text and "10 km/h" in text
    assert "(no figure)" in text
    doc = dlg.details.document()
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QTextDocument

    image = doc.resource(QTextDocument.ResourceType.ImageResource, QUrl("fig-b"))
    assert image is not None and not image.isNull()


def test_double_click_selects_the_object_in_the_editor(qtbot, window, dialog) -> None:
    item = _select(dialog, "SPEC-1")
    dialog._on_double_clicked(item)
    assert window.controller.current_obj_id() == "SPEC-1"
    assert dialog.isVisible()
    item = _select(dialog, "SPEC-3")
    dialog._on_double_clicked(item)
    assert window.controller.current_obj_id() == "SPEC-1"
    assert "SPEC-3 isn't in the open document" in dialog.message_label.text()


# --- general (MELLON-SRS-35..40) ----------------------------------------------


def test_comparison_runs_in_the_background(qtbot, dialog) -> None:
    dialog.compare()
    # The call returns before the result is in: the window stays responsive.
    assert dialog.is_busy()
    assert not dialog.compare_button.isEnabled()
    assert dialog.summary_label.text() == "Comparing…"
    _wait(qtbot, dialog)
    assert dialog.compare_button.isEnabled()


def test_only_the_latest_comparison_is_shown(qtbot, dialog) -> None:
    _choose(dialog, dialog.from_combo, LATEST)
    _choose(dialog, dialog.to_combo, LATEST)
    dialog.compare()
    _choose(dialog, dialog.from_combo, "SPEC-issue-1")
    dialog.compare()
    _wait(qtbot, dialog)
    QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(50)
    assert dialog.diff.ref_a == "SPEC-issue-1"
    assert len(_rows(dialog)) == 4


def test_closing_while_comparing_is_safe(qtbot, window) -> None:
    dlg = window.open_compare()
    dlg.close()
    QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(50)
    assert window._compare_dialog is None


def test_unreadable_object_is_marked(qtbot, repo) -> None:
    path = repo / "SPEC" / "objects" / "SPEC-2.xml"
    good = path.read_bytes()
    path.write_text("<object id=", encoding="utf-8")
    commit(repo, "break SPEC-2")
    path.write_bytes(good)
    commit(repo, "fix SPEC-2")  # so the editor can open the document
    w = _window(qtbot, repo)
    dlg = w.open_compare("HEAD~2", "HEAD~1")
    _wait(qtbot, dlg)
    assert _rows(dlg) == [("SPEC-2", "Modified (unreadable at To)", "Requirement 2.")]
    _select(dlg, "SPEC-2")
    assert "Can't read at To" in dlg.details.toPlainText()


def test_copy_list_to_clipboard(qtbot, dialog) -> None:
    dialog.filter_boxes[Change.MODIFIED].setChecked(False)
    text = dialog.copy_to_clipboard()
    assert QGuiApplication.clipboard().text() == text
    lines = text.splitlines()
    assert lines[0].startswith("SPEC: Issue 1")
    assert lines[1] == "1 added, 1 deleted, 2 modified"
    assert lines[2:] == ["SPEC-3\tDeleted\tRequirement 3.", "SPEC-4\tAdded\tRequirement 4."]


def test_unreadable_outline_is_reported(qtbot, repo) -> None:
    manifest = repo / "SPEC" / "SPEC.xml"
    good = manifest.read_bytes()
    manifest.write_text("<document id=", encoding="utf-8")
    commit(repo, "break manifest")
    manifest.write_bytes(good)
    commit(repo, "fix manifest")
    w = _window(qtbot, repo)
    dlg = w.open_compare("HEAD~2", "HEAD~1")
    _wait(qtbot, dlg)
    assert dlg.message_label.isVisible()
    assert "Can't read the outline at To" in dlg.message_label.text()
    assert dlg.empty_label.text().startswith("No differences")
