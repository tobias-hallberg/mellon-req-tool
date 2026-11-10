"""Tests for the Compare dialog (`gui/dialogs/compare_dialog.py`) and the
two ways into it: the Document Editor menu and the History tab.

Specified in MELLON-VER-SRS, under "Compare"; MELLON-VER-STS maps each test
to its requirement. Each test builds a small repo with the editor:

    seed      SPEC-0 "Heading" over SPEC-1..3      tagged SPEC-issue-1
    edits     SPEC-1 reworded, SPEC-2 given a rationale,
              SPEC-3 deleted, SPEC-4 added          (HEAD)
"""

from __future__ import annotations

import html
import html.parser
import inspect
import os
import re
import shutil
import threading
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

import pygit2
import pytest
from lxml import etree

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QObject, Qt, QThreadPool, QUrl  # noqa: E402
from PySide6.QtGui import QAction, QGuiApplication, QTextDocument  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTabWidget,
    QTextEdit,
    QWidget,
)

from mellon import diff, editor, git_ops  # noqa: E402
from mellon.diff import LATEST, WORKING_COPY, Change  # noqa: E402
from mellon.git_ops import commit  # noqa: E402
from mellon.gui.dialogs.compare_dialog import CompareDialog  # noqa: E402
from mellon.gui.main_window import DocumentEditorWindow  # noqa: E402
from mellon.gui.panels.grid_pane import GridPane, ReadOnlyBanner  # noqa: E402
from mellon.gui.panels.history_tab import HistoryTab  # noqa: E402
from mellon.schema import Table, TableCell, TableColumn, TableRow  # noqa: E402
from mellon.service import DocumentService  # noqa: E402
from tests.compare_support import (  # noqa: E402
    DOC,
    PARTS,
    _ambiguous_prefix,
    _build,
    _build_dirty_repo,
    _changed,
    _commit,
    _commit_of,
    _everything,
    _init,
    _oid,
    _r2_commit,
    _relinked_repo,
    _rewrite,
    _shows_hash_of,
    _side_commit,
    _snapshot,
    _tag,
)
from tests.conftest import _ensure_req_test_fixture  # noqa: E402


_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
    b'<rect width="40" height="20" fill="red"/></svg>\n'
)


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


# --- opening the dialog (MELLON-VER-SRS-1..5) -------------------------------


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


# --- choosing versions (MELLON-VER-SRS-6..17, -62) --------------------------


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


def test_working_copy_compare_saves_pending_inspector_text(qtbot, window, repo) -> None:
    # MELLON-VER-SRS-73: the Inspector's pending text too, not only the grid's.
    window._set_read_only(False)
    window.controller.select_object("SPEC", "SPEC-2")
    window.inspector._description.setPlainText("Typed in the Inspector.")
    dlg = window.open_compare(LATEST, WORKING_COPY)
    _wait(qtbot, dlg)
    assert _rows(dlg) == [("SPEC-2", "Modified", "Typed in the Inspector.")]
    obj = repo / "SPEC" / "objects" / "SPEC-2.xml"
    assert "Typed in the Inspector." in obj.read_text(encoding="utf-8")


def test_comparing_two_commits_leaves_pending_text_unsaved(qtbot, window, repo) -> None:
    # MELLON-VER-SRS-73 applies only to the working copy; -56 asks that
    # nothing else is written.
    window._set_read_only(False)
    window.controller.select_object("SPEC", "SPEC-2")
    obj = repo / "SPEC" / "objects" / "SPEC-2.xml"
    before = obj.read_bytes()
    window.inspector._description.setPlainText("Typed in the Inspector.")
    dlg = window.open_compare("SPEC-issue-1", LATEST)
    _wait(qtbot, dlg)
    assert obj.read_bytes() == before
    assert window.inspector._description.toPlainText() == "Typed in the Inspector."


def test_result_names_the_commit_compared_not_the_one_listed(qtbot, dialog, repo) -> None:
    # MELLON-VER-SRS-67: "Latest commit (…)" holds the hash HEAD had when the
    # list was made; after a new commit, the result names the one compared.
    listed = str(pygit2.Repository(str(repo)).head.target)[:7]
    editor.set_description(repo / "SPEC", "SPEC-1", "The VCU must halt.")
    latest = commit(repo, "later")
    dialog.compare()
    _wait(qtbot, dialog)
    text = dialog.summary_label.text()
    assert f"Latest commit ({latest[:7]})" in text
    assert listed not in text


def test_version_text_adds_the_hash_only_once() -> None:
    from mellon.gui.dialogs.compare_dialog import version_text

    full = "0123456789abcdef0123456789abcdef01234567"
    short = full[:7]
    assert version_text("HEAD~1", short, full) == f"HEAD~1 ({short})"
    assert version_text("Issue 1 (fedcba9)", short, full) == f"Issue 1 ({short})"
    assert version_text(f"{short}  seed", short, full) == f"{short}  seed"
    assert version_text(full, short, full) == full
    assert version_text("Working copy (uncommitted edits)", WORKING_COPY, None) == (
        "Working copy (uncommitted edits)"
    )


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


# --- listing changes (MELLON-VER-SRS-30..42) ---------------------------------


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
    assert dialog.empty_label.text().startswith("No object changed: SPEC's objects are the same at")
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


# --- showing an object's changes (MELLON-VER-SRS-43..54) ---------------------


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


# --- general (MELLON-VER-SRS-55..61) -----------------------------------------


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
    assert dlg.empty_label.text().startswith("No object changed")


# --- written blind from MELLON-VER-SRS (round 1) -----------------------------
#
# Written from MELLON-VER-SRS alone. Each test names the requirement it checks;
# expected values are worked out by hand from the requirement and the
# definitions, never copied from a run. The tests drive the dialog through its
# public widgets and methods, and build repositories in tmp_path with
# mellon.editor and mellon.git_ops. How they read the dialog:
#
# - The details pane is read as HTML: a table whose header row has a "From …"
#   and a "To …" column holds each changed field, so a value's side is the
#   column it sits in, not just its presence somewhere in the text.
# - A comparison is "held" by wrapping the `compare_document` method of the
#   window's `DocumentService` so chosen calls wait on a `threading.Event`.
# - A typed version is entered with `QComboBox.setEditText`, which is what the
#   line edit holds after typing, without the combo's completer filling it in.


REQ_TEST = Path(__file__).resolve().parents[2] / "req-test"

# The standard repository (`_build`): seed, then edits. Worked out from the
# helper's description: SPEC-1's text changed, SPEC-2's rationale set,
# SPEC-3 deleted, SPEC-4 created.
STD_ROWS = {
    "SPEC-1": ("Modified", "The VCU must stop."),
    "SPEC-2": ("Modified", "Requirement 2."),
    "SPEC-3": ("Deleted", "Requirement 3."),
    "SPEC-4": ("Added", "Requirement 4."),
}

RED_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
    b'<rect width="40" height="20" fill="#ff0000"/></svg>\n'
)
BLUE_SVG = RED_SVG.replace(b"#ff0000", b"#0000ff")


# --------------------------------------------------------------------------
# Repository helpers
# --------------------------------------------------------------------------


def _write_links(folder: Path, source: str, links: list[tuple[str, str, str, str]]) -> None:
    """Write `links/<source>.xml` by hand: (target_doc, target_id, type, ack)."""
    rows = "".join(
        f'  <link target-doc="{d}" target-id="{t}" type="{ty}" ack="{ack}"/>\n'
        for d, t, ty, ack in links
    )
    (folder / "links").mkdir(exist_ok=True)
    (folder / "links" / f"{source}.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<links source="{source}">\n{rows}</links>\n',
        encoding="utf-8",
        newline="\n",
    )


def _set_label(folder: Path, attr_name: str, label: str) -> None:
    """Give a manifest attribute a label, editing the XML directly."""
    path = folder / f"{folder.name}.xml"
    tree = etree.parse(str(path))
    (el,) = tree.xpath(f"/document/attributes/attribute[@name='{attr_name}']")
    el.set("label", label)
    tree.write(str(path), xml_declaration=True, encoding="UTF-8")


def _object_tree(folder: Path, obj_id: str):
    path = folder / "objects" / f"{obj_id}.xml"
    return path, etree.parse(str(path))


def _unset_attr(folder: Path, obj_id: str, name: str) -> None:
    """Remove an attribute from an object's file, so it is not set."""
    path, tree = _object_tree(folder, obj_id)
    for el in tree.xpath(f"/object/attr[@name='{name}']"):
        el.getparent().remove(el)
    tree.write(str(path), xml_declaration=True, encoding="UTF-8")


def _set_empty_attr(folder: Path, obj_id: str, name: str) -> None:
    """Give an object's file an empty value for an attribute: set, but empty."""
    path, tree = _object_tree(folder, obj_id)
    root = tree.getroot()
    assert not root.xpath(f"attr[@name='{name}']"), "setup: attribute already set"
    el = etree.Element("attr", name=name)
    el.text = ""
    root.insert(list(root).index(root.find("description")), el)
    tree.write(str(path), xml_declaration=True, encoding="UTF-8")


def _disk_description(folder: Path, obj_id: str) -> str:
    _, tree = _object_tree(folder, obj_id)
    return tree.findtext("description") or ""


def _outline(folder: Path) -> list[tuple[str, str | None]]:
    """The outline as (object ID, parent ID) in reading order, from the manifest."""
    tree = etree.parse(str(folder / f"{folder.name}.xml"))
    out: list[tuple[str, str | None]] = []

    def walk(el, parent):
        for ref in el.findall("ref"):
            out.append((ref.get("id"), parent))
            walk(ref, ref.get("id"))

    walk(tree.find("outline"), None)
    return out


def _std_repo(root: Path) -> Path:
    """The standard repository's content with fixed signatures (hashes repeat).

    seed: SPEC-0 (Heading) over SPEC-1 "The VCU shall stop.", SPEC-2
    "Requirement 2.", SPEC-3 "Requirement 3.". edits: SPEC-1 "The VCU must
    stop.", SPEC-2 rationale "Because.", SPEC-4 created, SPEC-3 deleted.
    """
    pygit2.init_repository(str(root))
    folder = editor.create_document(root, "SPEC", title="Spec")
    assert editor.create_object(folder, None, "Top heading") == "SPEC-0"
    editor.set_attribute(folder, "SPEC-0", "type", "Heading")
    for n, text in ((1, "The VCU shall stop."), (2, "Requirement 2."), (3, "Requirement 3.")):
        assert editor.create_object(folder, "SPEC-0", text) == f"SPEC-{n}"
    _commit(root, "seed", 1)
    editor.set_description(folder, "SPEC-1", "The VCU must stop.")
    editor.set_attribute(folder, "SPEC-2", "rationale", "Because.")
    assert editor.create_object(folder, "SPEC-0", "Requirement 4.") == "SPEC-4"
    editor.delete_object(folder, "SPEC-3", force=True)
    _commit(root, "edits", 2)
    return folder


def _outline_repo(root: Path) -> tuple[Path, str]:
    """SPEC-4 (top), SPEC-0 (top) over SPEC-3, SPEC-1, SPEC-2, SPEC-5 under SPEC-2.

    Outline order differs from ID order. Returns (folder, base commit).
    """
    pygit2.init_repository(str(root))
    folder = editor.create_document(root, "SPEC", title="Spec")
    for i in range(6):
        assert editor.create_object(folder, None, f"Object {i}, first text.") == f"SPEC-{i}"
    editor.move_object(folder, "SPEC-3", parent_id="SPEC-0")
    editor.move_object(folder, "SPEC-1", parent_id="SPEC-0", after="SPEC-3")
    editor.move_object(folder, "SPEC-2", parent_id="SPEC-0", after="SPEC-1")
    editor.move_object(folder, "SPEC-5", parent_id="SPEC-2")
    editor.move_object(folder, "SPEC-4", to_top=True, before="SPEC-0")
    assert _outline(folder) == [
        ("SPEC-4", None),
        ("SPEC-0", None),
        ("SPEC-3", "SPEC-0"),
        ("SPEC-1", "SPEC-0"),
        ("SPEC-2", "SPEC-0"),
        ("SPEC-5", "SPEC-2"),
    ], "setup: outline not built as intended"
    return folder, _commit(root, "base")


# --------------------------------------------------------------------------
# Dialog helpers
# --------------------------------------------------------------------------


def _service(win) -> DocumentService:
    return win.controller.require_service()


def _compare(qtbot, dlg) -> None:
    """Press the Compare button and wait until the comparison is done."""
    qtbot.mouseClick(dlg.compare_button, Qt.MouseButton.LeftButton)
    _wait(qtbot, dlg)


def _type(combo, text: str) -> None:
    combo.setEditText(text)


def _entries(combo) -> list[tuple[str, object]]:
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def _row_map(dlg) -> dict[str, tuple[str, str]]:
    return {obj_id: (change, text) for obj_id, change, text in _rows(dlg)}


def _ids(dlg) -> list[str]:
    return [r[0] for r in _rows(dlg)]


def _visible_texts(dlg) -> list[str]:
    return [lbl.text() for lbl in dlg.findChildren(QLabel) if lbl.isVisibleTo(dlg) and lbl.text()]


def _says_no_object_changed(dlg) -> bool:
    return any("no object changed" in t.lower() for t in _visible_texts(dlg))


def _asks_for_version(dlg) -> bool:
    return any("choose" in t.lower() and "version" in t.lower() for t in _visible_texts(dlg))


def _error_text(dlg) -> str:
    lbl = dlg.message_label
    return lbl.text() if lbl.isVisibleTo(dlg) else ""


_COUNT_RE = re.compile(r"(\d+)\s+(added|deleted|modified|moved|link reviewed)\b", re.I)


def _counts(text: str) -> dict[str, int]:
    """The non-zero counts a text gives, as {'added': 1, ...}."""
    found = {k.lower(): int(n) for n, k in _COUNT_RE.findall(text)}
    return {k: v for k, v in found.items() if v}


def _shown(widget) -> bool:
    return shiboken6.isValid(widget) and widget.isVisible()


def _drain() -> None:
    """Let background comparisons finish and deliver what they posted."""
    QThreadPool.globalInstance().waitForDone(30000)
    for _ in range(3):
        QApplication.sendPostedEvents()
        QApplication.processEvents()


def _action(win, text: str) -> QAction:
    for act in win.findChildren(QAction):
        if act.text().replace("&", "") == text:
            return act
    raise AssertionError(f"no action {text!r} in the Document Editor")


def _open_via_action(qtbot, win) -> CompareDialog:
    """Trigger the Document Editor's Compare… action; return the dialog shown."""
    _action(win, "Compare…").trigger()
    qtbot.waitUntil(lambda: any(d.isVisible() for d in win.findChildren(CompareDialog)))
    (dlg,) = [d for d in win.findChildren(CompareDialog) if d.isVisible()]
    _wait(qtbot, dlg)
    return dlg


class _Details(HTMLParser):
    """The details pane's HTML as outermost tables: rows of cells (text, images)."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._open: list[list[list[dict]]] = []
        self.feed(html)
        for table in self.tables:
            for row in table:
                for cell in row:
                    cell["text"] = " ".join(cell["text"].split())

    def _cells(self) -> list[dict]:
        return [t[-1][-1] for t in self._open if t and t[-1]]

    def _data(self, data: str) -> None:
        for cell in self._cells():
            cell["text"] += data

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            table: list[list[dict]] = []
            if not self._open:
                self.tables.append(table)
            self._open.append(table)
        elif tag == "tr" and self._open:
            self._open[-1].append([])
        elif tag in ("td", "th") and self._open:
            if not self._open[-1]:
                self._open[-1].append([])
            self._open[-1][-1].append({"text": "", "imgs": []})
        elif tag == "img":
            for cell in self._cells():
                cell["imgs"].append(dict(attrs).get("src", ""))
        elif tag == "br":
            self._data(" ")

    def handle_endtag(self, tag):
        if tag == "table" and self._open:
            self._open.pop()
        elif tag == "p":
            self._data(" ")

    def handle_data(self, data):
        self._data(data)


def _two_sided_rows(dlg) -> list[tuple[str, dict, dict]]:
    """(field label, From cell, To cell) for each row of the From/To table."""
    out = []
    for table in _Details(dlg.details.toHtml()).tables:
        if not table:
            continue
        head = [c["text"].lower() for c in table[0]]
        i_from = next((i for i, h in enumerate(head) if h.startswith("from")), None)
        i_to = next((i for i, h in enumerate(head) if h.startswith("to")), None)
        if i_from is None or i_to is None:
            continue
        for row in table[1:]:
            if len(row) > max(i_from, i_to):
                out.append((row[0]["text"], row[i_from], row[i_to]))
    return out


def _sides(dlg, label: str) -> tuple[dict, dict]:
    rows = [(f, t) for lbl, f, t in _two_sided_rows(dlg) if lbl == label]
    assert rows, (
        f"no {label!r} row under From/To in the details; rows: "
        f"{[r[0] for r in _two_sided_rows(dlg)]}\n{dlg.details.toPlainText()}"
    )
    return rows[0]


def _one_sided_rows(dlg) -> list[tuple[str, dict]]:
    """(label, value cell) for each two-cell row of the details' outer tables."""
    out = []
    for table in _Details(dlg.details.toHtml()).tables:
        for row in table:
            if len(row) == 2:
                out.append((row[0]["text"], row[1]))
    return out


def _image_rgb(dlg, src: str) -> tuple[int, int, int] | None:
    res = dlg.details.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(src))
    if res is None:
        return None
    img = res.toImage() if hasattr(res, "toImage") else res
    if img.isNull():
        return None
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return c.red(), c.green(), c.blue()


def _is_red(rgb) -> bool:
    return rgb is not None and rgb[0] > 180 and rgb[1] < 90 and rgb[2] < 90


def _is_blue(rgb) -> bool:
    return rgb is not None and rgb[2] > 180 and rgb[0] < 90 and rgb[1] < 90


def _id_re(obj_id: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(obj_id)}(?!\d)")


def _link_status(dlg, target_id: str, link_type: str) -> set[str]:
    """How the details show a link: 'added', 'removed' (by word or by side)."""
    found: set[str] = set()
    pat = _id_re(target_id)
    for line in dlg.details.toPlainText().splitlines():
        if pat.search(line) and link_type in line:
            low = line.lower()
            if "added" in low:
                found.add("added")
            if "removed" in low:
                found.add("removed")
    for _, f, t in _two_sided_rows(dlg):
        in_from = bool(pat.search(f["text"])) and link_type in f["text"]
        in_to = bool(pat.search(t["text"])) and link_type in t["text"]
        if in_to and not in_from:
            found.add("added")
        if in_from and not in_to:
            found.add("removed")
    return found


class _Hold:
    """Wrap a DocumentService's compare_document so chosen calls wait for `release`."""

    def __init__(self, service, monkeypatch, hold_if) -> None:
        self.release = threading.Event()
        self.entered: list[tuple[str, str]] = []
        self.returned: list[tuple[str, str]] = []
        real = service.compare_document
        sig = inspect.signature(real)

        def compare_document(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            key = (bound.arguments["ref_a"], bound.arguments["ref_b"])
            self.entered.append(key)
            try:
                if hold_if(*key) and not self.release.wait(30):
                    raise RuntimeError("held comparison was never released")
                return real(*args, **kwargs)
            finally:
                self.returned.append(key)

        monkeypatch.setattr(service, "compare_document", compare_document)

    def froms_entered(self) -> list[str]:
        return [a for a, _ in self.entered]

    def froms_returned(self) -> list[str]:
        return [a for a, _ in self.returned]


@pytest.fixture
def holds(monkeypatch):
    made: list[_Hold] = []

    def make(service, hold_if=lambda a, b: False) -> _Hold:
        h = _Hold(service, monkeypatch, hold_if)
        made.append(h)
        return h

    yield make
    for h in made:
        h.release.set()
    QThreadPool.globalInstance().waitForDone(30000)


class _ShownResults(QObject):
    """Record each distinct (summary, rows) the dialog shows, before every event."""

    def __init__(self, dlg) -> None:
        super().__init__()
        self._dlg = dlg
        self._sampling = False
        self.states: list[tuple[str, tuple]] = []
        QApplication.instance().installEventFilter(self)
        self._sample()

    def _sample(self) -> None:
        if self._sampling or not shiboken6.isValid(self._dlg):
            return
        self._sampling = True
        try:
            state = (self._dlg.summary_label.text(), tuple(_rows(self._dlg)))
        except RuntimeError:
            return
        finally:
            self._sampling = False
        if not self.states or self.states[-1] != state:
            self.states.append(state)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        self._sample()
        return False

    def stop(self) -> None:
        QApplication.instance().removeEventFilter(self)
        self._sample()

    def rows_seen(self) -> list[tuple]:
        return [rows for _, rows in self.states]


# ==========================================================================
# MELLON-VER-SRS-5: the dialog closes with its Document Editor
# ==========================================================================


def test_srs5_dialog_closes_when_editor_closes(qtbot, tmp_path):
    """MELLON-VER-SRS-5: closing the Document Editor closes its finished Compare dialog."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    win.show()
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    assert _shown(dlg), "setup: the Compare dialog should be open"
    win.close()
    qtbot.waitUntil(lambda: not _shown(dlg), timeout=5000)
    still = [w for w in QApplication.topLevelWidgets() if isinstance(w, CompareDialog) and w.isVisible()]
    assert not still, "a Compare dialog is still a visible top-level window after its editor closed"


def test_srs5_dialog_closes_when_editor_closes_mid_comparison(qtbot, tmp_path, holds):
    """MELLON-VER-SRS-5: closing the editor while the comparison runs closes the dialog;
    when the comparison then finishes, no window appears and nothing raises."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    win.show()
    hold = holds(_service(win), lambda a, b: True)
    with qtbot.captureExceptions() as exceptions:
        dlg = win.open_compare()
        qtbot.waitUntil(lambda: bool(hold.entered), timeout=5000)
        before = {id(w) for w in QApplication.topLevelWidgets() if w.isVisible()}
        win.close()
        qtbot.waitUntil(lambda: not _shown(dlg), timeout=5000)
        hold.release.set()
        qtbot.waitUntil(lambda: bool(hold.returned), timeout=10000)
        _drain()
        appeared = [
            type(w).__name__ + ": " + w.windowTitle()
            for w in QApplication.topLevelWidgets()
            if w.isVisible() and id(w) not in before
        ]
    assert not _shown(dlg), "the Compare dialog came back after its comparison finished"
    assert not appeared, f"a window appeared after the comparison finished: {appeared}"
    assert not exceptions, f"an exception was raised: {exceptions}"


# ==========================================================================
# MELLON-VER-SRS-4: History tab's Compare with latest…
# ==========================================================================


def _history_repo(root: Path) -> tuple[str, str, str]:
    """SPEC-2 changed in three commits A (seed), B (edits), C (newest = HEAD)."""
    folder = _build(root)
    a = _oid(root, "HEAD~1")
    b = _oid(root, "HEAD")
    editor.set_description(folder, "SPEC-2", "Requirement 2, changed in C.")
    c = _commit(root, "change SPEC-2 again")
    return a, b, c


def _history_compare(qtbot, win, commit: str) -> CompareDialog:
    """Select SPEC-2, pick `commit` in the History tab and press Compare with latest…."""
    win.show()
    win.controller.select_object("SPEC", "SPEC-2")
    hist = win.findChild(HistoryTab)
    (tabs,) = [t for t in win.right_pane.findChildren(QTabWidget) if t.indexOf(hist) >= 0]
    tabs.setCurrentWidget(hist)
    table = hist.findChild(QTableWidget)
    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    hash_col = HistoryTab.COLUMNS.index("Hash")
    (row,) = [r for r in range(table.rowCount()) if commit.startswith(table.item(r, hash_col).text())]
    table.selectRow(row)
    (button,) = [b for b in hist.findChildren(QPushButton) if b.text() == "Compare with latest…"]
    qtbot.waitUntil(button.isEnabled, timeout=2000)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: any(d.isVisible() for d in win.findChildren(CompareDialog)), timeout=5000)
    (dlg,) = [d for d in win.findChildren(CompareDialog) if d.isVisible()]
    _wait(qtbot, dlg)
    return dlg


def test_srs4_history_compare_with_latest_sets_from_and_to(qtbot, tmp_path):
    """MELLON-VER-SRS-4: Compare with latest… on commit B opens From = B, To = latest commit."""
    root = tmp_path / "r"
    _, b, c = _history_repo(root)
    win = _window(qtbot, root)
    dlg = _history_compare(qtbot, win, b)
    assert _commit_of(root, dlg.ref(dlg.from_combo)) == b, (
        f"From should be commit B {b[:7]}, is {dlg.ref(dlg.from_combo)!r}"
    )
    to = dlg.ref(dlg.to_combo)
    assert to != WORKING_COPY and _commit_of(root, to) == c, (
        f"To should be the latest commit {c[:7]}, is {to!r}"
    )


def test_srs4_history_compare_with_latest_shows_changes_since_commit(qtbot, tmp_path):
    """MELLON-VER-SRS-4: comparing B with the latest commit lists SPEC-2, changed in C."""
    root = tmp_path / "r"
    _, b, _ = _history_repo(root)
    win = _window(qtbot, root)
    dlg = _history_compare(qtbot, win, b)
    assert _rows(dlg) == [("SPEC-2", "Modified", "Requirement 2, changed in C.")], (
        f"B -> latest should list only SPEC-2 Modified; rows: {_rows(dlg)}"
    )


def test_srs4_history_compare_latest_commit_says_no_object_changed(qtbot, tmp_path):
    """MELLON-VER-SRS-4 (and -34): Compare with latest… on C, the latest commit: From = C,
    and the dialog says no object changed."""
    root = tmp_path / "r"
    _, _, c = _history_repo(root)
    win = _window(qtbot, root)
    dlg = _history_compare(qtbot, win, c)
    assert _commit_of(root, dlg.ref(dlg.from_combo)) == c, "From should be commit C"
    assert _rows(dlg) == [], f"C -> latest should list no rows: {_rows(dlg)}"
    assert _says_no_object_changed(dlg), f"the dialog should say no object changed: {_visible_texts(dlg)}"


def test_srs4_history_button_text(qtbot, tmp_path):
    """MELLON-VER-SRS-4: the History tab's action is named 'Compare with latest…'."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    hist = win.findChild(HistoryTab)
    texts = [b.text() for b in hist.findChildren(QPushButton)]
    assert "Compare with latest…" in texts, f"no 'Compare with latest…' button; buttons: {texts}"


# ==========================================================================
# MELLON-VER-SRS-7: versions offered
# ==========================================================================


@pytest.mark.parametrize("side", ["from_combo", "to_combo"])
def test_srs7_offers_each_remote_tracking_branch(qtbot, tmp_path, side):
    """MELLON-VER-SRS-7: in a clone, From and To each offer each remote-tracking branch."""
    src = tmp_path / "src"
    _build(src)
    repo = pygit2.Repository(str(src))
    repo.branches.local.create("feature", repo.revparse_single("HEAD~1").peel(pygit2.Commit))
    clone_root = tmp_path / "clone"
    clone = pygit2.clone_repository(str(src), str(clone_root))
    remote = [n for n in clone.branches.remote if not n.endswith("/HEAD")]
    assert sorted(remote) == ["origin/feature", "origin/master"], f"setup: remote branches {remote}"
    win = _window(qtbot, clone_root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    entries = _entries(getattr(dlg, side))
    for name in remote:
        target = str(clone.branches.remote[name].peel(pygit2.Commit).id)
        match = [
            (t, d) for t, d in entries
            if (name in t or name in str(d)) and _commit_of(clone_root, str(d)) == target
        ]
        assert match, f"{side} doesn't offer remote-tracking branch {name}; entries: {entries}"


@pytest.mark.parametrize("side", ["from_combo", "to_combo"])
def test_srs7_offers_each_issue_including_off_branch(qtbot, tmp_path, side):
    """MELLON-VER-SRS-7: issues 1, 2, 2.10 on the line of history and 3 on another branch
    are all offered, in From and in To."""
    root = tmp_path / "r"
    folder = _build(root)  # seed tagged issue 1
    repo = pygit2.Repository(str(root))
    git_ops.create_issue_tag(repo, "SPEC", "2", repo.revparse_single("HEAD").id)
    editor.set_description(folder, "SPEC-2", "Requirement 2, issue 2.10.")
    _commit(root, "third")
    git_ops.create_issue_tag(repo, "SPEC", "2.10", repo.revparse_single("HEAD").id)
    side_commit = _side_commit(root, "HEAD~2", "side branch work")
    git_ops.create_issue_tag(repo, "SPEC", "3", pygit2.Oid(hex=side_commit))
    tagged = {n: _oid(root, f"SPEC-issue-{n}") for n in ("1", "2", "2.10", "3")}
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    entries = _entries(getattr(dlg, side))
    for issue, commit in tagged.items():
        named = re.compile(rf"\bIssue {re.escape(issue)}(?![.\d])")
        match = [
            (t, d) for t, d in entries
            if (d == f"SPEC-issue-{issue}" or named.search(t)) and _commit_of(root, str(d)) == commit
        ]
        assert match, f"{side} doesn't offer issue {issue}; entries: {entries}"


# ==========================================================================
# MELLON-VER-SRS-8: the working copy as a version
# ==========================================================================


def _working_copy_compare(qtbot, tmp_path):
    root = tmp_path / "r"
    folder = _build(root)
    editor.set_description(folder, "SPEC-1", "Uncommitted text of SPEC-1.")
    new_id = editor.create_object(folder, "SPEC-0", "Brand new object.")
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    _choose(dlg, dlg.from_combo, WORKING_COPY)
    _choose(dlg, dlg.to_combo, LATEST)
    _compare(qtbot, dlg)
    return dlg, new_id


@pytest.mark.parametrize("side", ["from_combo", "to_combo"])
def test_srs8_working_copy_offered(qtbot, tmp_path, side):
    """MELLON-VER-SRS-8: the working copy is a choice for From and for To."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    datas = [d for _, d in _entries(getattr(dlg, side))]
    assert WORKING_COPY in datas, f"{side} doesn't offer the working copy: {datas}"


def test_srs8_working_copy_to_latest_rows(qtbot, tmp_path):
    """MELLON-VER-SRS-8: From = working copy, To = latest: the edited SPEC-1 is Modified and
    the uncommitted new object is Deleted (it exists at From only)."""
    dlg, new_id = _working_copy_compare(qtbot, tmp_path)
    rows = _row_map(dlg)
    assert rows.get("SPEC-1", ("",))[0] == "Modified", f"SPEC-1 should be Modified: {rows}"
    assert rows.get(new_id) == ("Deleted", "Brand new object."), f"{new_id} should be Deleted: {rows}"
    assert set(rows) == {"SPEC-1", new_id}, f"only SPEC-1 and {new_id} changed: {rows}"


def test_srs8_working_copy_text_is_the_from_value(qtbot, tmp_path):
    """MELLON-VER-SRS-8 (with -44): SPEC-1's description: uncommitted text under From,
    committed text under To."""
    dlg, _ = _working_copy_compare(qtbot, tmp_path)
    _select(dlg, "SPEC-1")
    f, t = _sides(dlg, "Description")
    assert f["text"] == "Uncommitted text of SPEC-1.", f"From should hold the working copy text: {f}"
    assert t["text"] == "The VCU must stop.", f"To should hold the committed text: {t}"


# ==========================================================================
# MELLON-VER-SRS-9 / -10: typed versions
# ==========================================================================


STD_DICT = {k: v for k, v in STD_ROWS.items()}


def _typed_from(qtbot, win, text: str):
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    _type(dlg.from_combo, text)
    _choose(dlg, dlg.to_combo, LATEST)
    _compare(qtbot, dlg)
    return dlg


@pytest.mark.parametrize("how", ["full-hash", "4-hex-prefix", "annotated-tag", "lightweight-tag", "branch"])
def test_srs9_typed_name_resolves(qtbot, tmp_path, how):
    """MELLON-VER-SRS-9: a typed full hash, unique 4-hex prefix, tag or branch naming seed
    compares seed with the latest commit."""
    root = tmp_path / "r"
    _std_repo(root)
    repo = pygit2.Repository(str(root))
    seed = _oid(root, "HEAD~1")
    if how == "full-hash":
        text = seed
    elif how == "4-hex-prefix":
        text = seed[:4]
        sharing = [str(o) for o in repo if str(o).startswith(text)]
        assert sharing == [seed], f"setup: prefix {text} isn't unique: {sharing}"
    elif how == "annotated-tag":
        text = git_ops.create_tag(repo, "review-1", pygit2.Oid(hex=seed), "review 1")
    elif how == "lightweight-tag":
        repo.references.create("refs/tags/review-lw", pygit2.Oid(hex=seed))
        text = "review-lw"
    else:
        repo.branches.local.create("feature", repo.revparse_single(seed).peel(pygit2.Commit))
        text = "feature"
    win = _window(qtbot, root)
    dlg = _typed_from(qtbot, win, text)
    assert _error_text(dlg) == "", f"typing {text!r} gave an error: {_error_text(dlg)!r}"
    assert _row_map(dlg) == STD_DICT, f"typing {text!r} as From: rows {_rows(dlg)}"


def test_srs9_typed_remote_tracking_branch_resolves(qtbot, tmp_path):
    """MELLON-VER-SRS-9: in a clone, typing the remote-tracking branch origin/feature (on seed)
    as From compares seed with the latest commit."""
    src = tmp_path / "src"
    _std_repo(src)
    repo = pygit2.Repository(str(src))
    repo.branches.local.create("feature", repo.revparse_single("HEAD~1").peel(pygit2.Commit))
    clone_root = tmp_path / "clone"
    pygit2.clone_repository(str(src), str(clone_root))
    win = _window(qtbot, clone_root)
    dlg = _typed_from(qtbot, win, "origin/feature")
    assert _error_text(dlg) == "", f"typing origin/feature gave an error: {_error_text(dlg)!r}"
    assert _row_map(dlg) == STD_DICT, f"origin/feature as From: rows {_rows(dlg)}"


def test_srs9_three_hex_prefix_is_refused(qtbot, tmp_path):
    """MELLON-VER-SRS-9 (boundary, with -10, -11): 3 hex digits are below the 4 git accepts:
    an error names them and the previous result stays."""
    root = tmp_path / "r"
    _std_repo(root)
    seed = _oid(root, "HEAD~1")
    win = _window(qtbot, root)
    dlg = _typed_from(qtbot, win, seed)
    rows_before, summary_before = _rows(dlg), dlg.summary_label.text()
    assert _row_map(dlg) == STD_DICT, "setup: the full-hash comparison should succeed"
    _type(dlg.from_combo, seed[:3])
    _compare(qtbot, dlg)
    assert seed[:3] in _error_text(dlg), f"error should name {seed[:3]!r}: {_error_text(dlg)!r}"
    assert _rows(dlg) == rows_before, "the previous rows should stay after a refused 3-hex prefix"
    assert dlg.summary_label.text() == summary_before, "the previous versions/counts should stay"


@pytest.mark.parametrize("kind", ["commit", "blob"])
def test_srs10_ambiguous_prefix_error_names_it(qtbot, tmp_path, kind):
    """MELLON-VER-SRS-10 (with -11): a 4-hex prefix shared by two objects is refused with an
    error naming it, and the previous result stays."""
    root = tmp_path / "r"
    _build(root)
    prefix = _ambiguous_prefix(root, kind)
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    rows_before, summary_before = _rows(dlg), dlg.summary_label.text()
    assert rows_before, "setup: the default comparison should show rows"
    _type(dlg.from_combo, prefix)
    _compare(qtbot, dlg)
    assert prefix in _error_text(dlg), f"error should name {prefix!r}: {_error_text(dlg)!r}"
    assert _rows(dlg) == rows_before, f"the ambiguous prefix {prefix} was compared as something"
    assert dlg.summary_label.text() == summary_before, "the previous versions/counts should stay"


def test_srs10_unknown_name_error_names_it(qtbot, tmp_path):
    """MELLON-VER-SRS-10: 'no-such-version' typed as To: the error names it."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    rows_before = _rows(dlg)
    _type(dlg.to_combo, "no-such-version")
    _compare(qtbot, dlg)
    assert "no-such-version" in _error_text(dlg), f"error should name the version: {_error_text(dlg)!r}"
    assert _rows(dlg) == rows_before, "the previous rows should stay"


# ==========================================================================
# MELLON-VER-SRS-11: a failed comparison keeps the previous result
# ==========================================================================


def test_srs11_failed_comparison_keeps_previous_result(qtbot, tmp_path):
    """MELLON-VER-SRS-11: From = To = a commit where SPEC doesn't exist fails; the rows,
    counts and versions shown stay those of the previous result."""
    root = tmp_path / "r"
    pygit2.init_repository(str(root))
    other = editor.create_document(root, "OTHER", title="Other")
    editor.create_object(other, None, "Other object.")
    first = _commit(root, "first: OTHER only")
    spec = editor.create_document(root, "SPEC", title="Spec")
    editor.create_object(spec, None, "Spec object zero.")
    editor.create_object(spec, None, "Spec object one.")
    second = _commit(root, "second: SPEC appears")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=first, to_ref=second)
    _wait(qtbot, dlg)
    assert _row_map(dlg) == {
        "SPEC-0": ("Added", "Spec object zero."),
        "SPEC-1": ("Added", "Spec object one."),
    }, f"setup: SPEC's objects should be Added: {_rows(dlg)}"
    rows_before, summary_before = _rows(dlg), dlg.summary_label.text()
    _type(dlg.from_combo, first)
    _type(dlg.to_combo, first)
    _compare(qtbot, dlg)
    assert _error_text(dlg), "an error should be shown when SPEC exists at neither version"
    assert _rows(dlg) == rows_before, f"the previous rows should stay: {_rows(dlg)}"
    assert dlg.summary_label.text() == summary_before, (
        f"the previous versions and counts should stay: {dlg.summary_label.text()!r}"
    )


# ==========================================================================
# MELLON-VER-SRS-14 / -15 / -16: unset versions
# ==========================================================================


def test_srs14_only_issue_is_latest_commit(qtbot, tmp_path):
    """MELLON-VER-SRS-14: the only issue is on the latest commit, unchanged since: From unset,
    To = latest commit, and the dialog asks for a version."""
    root = tmp_path / "r"
    _build(root, issue=False)
    repo = pygit2.Repository(str(root))
    git_ops.create_issue_tag(repo, "SPEC", "1", repo.revparse_single("HEAD").id)
    win = _window(qtbot, root)
    dlg = _open_via_action(qtbot, win)
    assert dlg.ref(dlg.from_combo) is None, f"From should be unset: {dlg.ref(dlg.from_combo)!r}"
    to = dlg.ref(dlg.to_combo)
    assert to != WORKING_COPY and _commit_of(root, to) == _oid(root, "HEAD"), f"To should be latest: {to!r}"
    assert _asks_for_version(dlg), f"the dialog should ask for a version: {_visible_texts(dlg)}"
    assert _rows(dlg) == [], "no rows while From is unset"


def test_srs14_only_issue_off_the_line_of_history_dialog(qtbot, tmp_path):
    """MELLON-VER-SRS-14: the only issue is on a branch commit that isn't an ancestor of the
    latest commit: From unset, To = latest commit."""
    root = tmp_path / "r"
    _build(root, issue=False)
    side = _side_commit(root, "HEAD~1", "side branch work")
    git_ops.create_issue_tag(pygit2.Repository(str(root)), "SPEC", "1", pygit2.Oid(hex=side))
    win = _window(qtbot, root)
    dlg = _open_via_action(qtbot, win)
    assert dlg.ref(dlg.from_combo) is None, f"From should be unset: {dlg.ref(dlg.from_combo)!r}"
    to = dlg.ref(dlg.to_combo)
    assert to != WORKING_COPY and _commit_of(root, to) == _oid(root, "HEAD"), f"To should be latest: {to!r}"


def _uncommitted_repo(root: Path) -> None:
    pygit2.init_repository(str(root))
    folder = editor.create_document(root, "SPEC", title="Spec")
    editor.create_object(folder, None, "First object.")
    editor.create_object(folder, None, "Second object.")


def test_srs15_no_commit_opens_with_working_copy_as_to(qtbot, tmp_path):
    """MELLON-VER-SRS-15: no commit yet: From unset, To = working copy, the dialog asks for a
    version and shows no rows."""
    root = tmp_path / "r"
    _uncommitted_repo(root)
    win = _window(qtbot, root)
    dlg = _open_via_action(qtbot, win)
    assert dlg.ref(dlg.from_combo) is None, f"From should be unset: {dlg.ref(dlg.from_combo)!r}"
    assert dlg.ref(dlg.to_combo) == WORKING_COPY, f"To should be the working copy: {dlg.ref(dlg.to_combo)!r}"
    assert _asks_for_version(dlg), f"the dialog should ask for a version: {_visible_texts(dlg)}"
    assert _rows(dlg) == [], f"no rows expected: {_rows(dlg)}"


def test_srs15_no_commit_working_copy_against_itself(qtbot, tmp_path):
    """MELLON-VER-SRS-15 (with -34): choosing the working copy as From too: no object changed."""
    root = tmp_path / "r"
    _uncommitted_repo(root)
    win = _window(qtbot, root)
    dlg = _open_via_action(qtbot, win)
    _choose(dlg, dlg.from_combo, WORKING_COPY)
    _compare(qtbot, dlg)
    assert _rows(dlg) == [], f"no rows expected: {_rows(dlg)}"
    assert _says_no_object_changed(dlg), f"should say no object changed: {_visible_texts(dlg)}"


def _assert_asks_instead_of_comparing(dlg, hold) -> None:
    assert hold.entered == [], f"a comparison started with a version unset: {hold.entered}"
    assert not dlg.is_busy(), "the dialog is busy with a version unset"
    assert _rows(dlg) == [], f"rows shown with a version unset: {_rows(dlg)}"
    assert not _says_no_object_changed(dlg), "'no object changed' shown with a version unset"
    assert _asks_for_version(dlg), f"the dialog should ask for a version: {_visible_texts(dlg)}"


def test_srs16_from_unset_compare_asks_for_version(qtbot, tmp_path, holds):
    """MELLON-VER-SRS-16: From unset (no issue): pressing Compare starts no comparison and the
    request to choose a version stays."""
    root = tmp_path / "r"
    _build(root, issue=False)
    win = _window(qtbot, root)
    hold = holds(_service(win))
    dlg = win.open_compare()
    QApplication.processEvents()
    assert dlg.ref(dlg.from_combo) is None, "setup: From should be unset"
    qtbot.mouseClick(dlg.compare_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    _assert_asks_instead_of_comparing(dlg, hold)


@pytest.mark.parametrize("to_text", ["", "   "], ids=["empty", "whitespace"])
def test_srs16_to_cleared_compare_asks_for_version(qtbot, tmp_path, holds, to_text):
    """MELLON-VER-SRS-16: From chosen, To's text cleared (or only whitespace, which is Unset):
    pressing Compare starts no comparison and the dialog asks for a version."""
    root = tmp_path / "r"
    _build(root, issue=False)
    seed = _oid(root, "HEAD~1")
    win = _window(qtbot, root)
    hold = holds(_service(win))
    dlg = win.open_compare()
    QApplication.processEvents()
    _type(dlg.to_combo, to_text)
    _choose(dlg, dlg.from_combo, seed)
    qtbot.mouseClick(dlg.compare_button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    _assert_asks_instead_of_comparing(dlg, hold)


# ==========================================================================
# MELLON-VER-SRS-17: swap
# ==========================================================================


def _swap_setup(qtbot, tmp_path):
    root = tmp_path / "r"
    _build(root)
    short = _oid(root, "HEAD~1")[:7]
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    _type(dlg.from_combo, short)
    _choose(dlg, dlg.to_combo, WORKING_COPY)
    _compare(qtbot, dlg)
    assert _row_map(dlg) == STD_DICT, f"setup: seed -> working copy rows {_rows(dlg)}"
    qtbot.mouseClick(dlg.swap_button, Qt.MouseButton.LeftButton)
    _wait(qtbot, dlg)
    return root, dlg, short


def test_srs17_swap_exchanges_from_and_to(qtbot, tmp_path):
    """MELLON-VER-SRS-17: swap: From = working copy, To = the typed short hash."""
    root, dlg, short = _swap_setup(qtbot, tmp_path)
    assert dlg.ref(dlg.from_combo) == WORKING_COPY, f"From after swap: {dlg.ref(dlg.from_combo)!r}"
    to = dlg.ref(dlg.to_combo)
    assert _commit_of(root, to) == _oid(root, "HEAD~1"), f"To after swap should name {short}: {to!r}"


def test_srs17_swapped_versions_compare_reversed(qtbot, tmp_path):
    """MELLON-VER-SRS-17: after swap and Compare, an object Added before is Deleted and the
    Deleted one is Added."""
    _, dlg, _ = _swap_setup(qtbot, tmp_path)
    _compare(qtbot, dlg)
    rows = _row_map(dlg)
    assert rows.get("SPEC-4", ("",))[0] == "Deleted", f"SPEC-4 was Added, should be Deleted: {rows}"
    assert rows.get("SPEC-3", ("",))[0] == "Added", f"SPEC-3 was Deleted, should be Added: {rows}"


# ==========================================================================
# MELLON-VER-SRS-31 / -32 / -33: rows and counts
# ==========================================================================


def _modified_and_moved_repo(root: Path) -> None:
    folder = _build(root)
    editor.set_description(folder, "SPEC-2", "Requirement 2, revised.")
    editor.move_object(folder, "SPEC-2", to_top=True)
    _commit(root, "edit and move SPEC-2")


def test_srs31_modified_and_moved_is_one_row(qtbot, tmp_path):
    """MELLON-VER-SRS-31: SPEC-2 edited and moved to the top level: one row showing both
    Modified and Moved."""
    root = tmp_path / "r"
    _modified_and_moved_repo(root)
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=_oid(root, "HEAD~1"), to_ref=LATEST)
    _wait(qtbot, dlg)
    rows = _rows(dlg)
    assert [r[0] for r in rows] == ["SPEC-2"], f"exactly one row, SPEC-2, expected: {rows}"
    change = rows[0][1]
    assert "Modified" in change and "Moved" in change, f"the row should show Modified and Moved: {change!r}"


def test_srs31_recompare_with_later_row_selected_raises_nothing(qtbot, tmp_path):
    """MELLON-VER-SRS-31 (with -44): with the fourth row selected, comparing versions that
    give one row shows that row and raises no exception."""
    root = tmp_path / "r"
    folder = _build(root)
    edits = _oid(root, "HEAD")
    editor.set_description(folder, "SPEC-2", "Requirement 2, revised.")
    _commit(root, "change SPEC-2")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref="SPEC-issue-1", to_ref=LATEST)
    _wait(qtbot, dlg)
    assert len(_rows(dlg)) == 4, f"setup: four rows expected: {_rows(dlg)}"
    _select(dlg, _ids(dlg)[3])
    with qtbot.captureExceptions() as exceptions:
        _choose(dlg, dlg.from_combo, edits)
        _compare(qtbot, dlg)
        _drain()
    assert not exceptions, f"re-comparing raised: {[str(e[1]) for e in exceptions]}"
    assert _rows(dlg) == [("SPEC-2", "Modified", "Requirement 2, revised.")], f"rows: {_rows(dlg)}"


def test_srs32_row_text_is_first_line(qtbot, tmp_path):
    """MELLON-VER-SRS-32: a modified object's row shows the first line of its description at
    To; a deleted object's row shows the first line at From."""
    root = tmp_path / "r"
    folder = _build(root)
    editor.set_description(folder, "SPEC-1", "Old first line.\nOld second line.\nOld third line.")
    gone = editor.create_object(folder, "SPEC-0", "Gone first line.\nGone second line.")
    before = _commit(root, "multi-line texts")
    editor.set_description(folder, "SPEC-1", "New first line.\nNew second line.\nNew third line.")
    editor.delete_object(folder, gone, force=True)
    _commit(root, "change and delete")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _row_map(dlg) == {
        "SPEC-1": ("Modified", "New first line."),
        gone: ("Deleted", "Gone first line."),
    }, f"rows: {_rows(dlg)}"


def test_srs33_counts_include_hidden_rows(qtbot, tmp_path):
    """MELLON-VER-SRS-33: with Modified unticked, the counts still read 1 added, 1 deleted,
    2 modified."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    dlg.filter_boxes[Change.MODIFIED].click()
    qtbot.waitUntil(lambda: "SPEC-1" not in _ids(dlg), timeout=2000)
    assert _counts(dlg.summary_label.text()) == {"added": 1, "deleted": 1, "modified": 2}, (
        f"counts with Modified hidden: {dlg.summary_label.text()!r}"
    )


def test_srs33_modified_and_moved_counts_once_each(qtbot, tmp_path):
    """MELLON-VER-SRS-33: an object Modified and Moved counts as 1 modified and 1 moved."""
    root = tmp_path / "r"
    _modified_and_moved_repo(root)
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=_oid(root, "HEAD~1"), to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _counts(dlg.summary_label.text()) == {"modified": 1, "moved": 1}, (
        f"counts: {dlg.summary_label.text()!r}"
    )


# ==========================================================================
# MELLON-VER-SRS-35 / -36: row order
# ==========================================================================


def test_srs35_rows_follow_outline_at_to(qtbot, tmp_path):
    """MELLON-VER-SRS-35: all six objects changed: rows read as the outline at To, each object
    before its sub-objects, not in ID order."""
    root = tmp_path / "r"
    folder, base = _outline_repo(root)
    for i in range(6):
        editor.set_description(folder, f"SPEC-{i}", f"Object {i}, second text.")
    _commit(root, "change all six")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=base, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _ids(dlg) == ["SPEC-4", "SPEC-0", "SPEC-3", "SPEC-1", "SPEC-2", "SPEC-5"], f"rows: {_rows(dlg)}"


def _outline_change(qtbot, tmp_path, deleted: list[str], modified: list[str]) -> list[str]:
    root = tmp_path / "r"
    folder, base = _outline_repo(root)
    for obj_id in modified:
        editor.set_description(folder, obj_id, f"{obj_id} changed.")
    for obj_id in deleted:
        editor.delete_object(folder, obj_id, force=True)
    _commit(root, "delete and modify")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=base, to_ref=LATEST)
    _wait(qtbot, dlg)
    kinds = {i: c for i, c, _ in _rows(dlg)}
    assert all(kinds.get(i) == "Deleted" for i in deleted), f"setup: deleted rows {kinds}"
    assert all(kinds.get(i) == "Modified" for i in modified), f"setup: modified rows {kinds}"
    return _ids(dlg)


def test_srs36_deleted_first_object_comes_first(qtbot, tmp_path):
    """MELLON-VER-SRS-36: SPEC-4, first at From with nothing before it, is deleted: its row
    comes first, before the modified SPEC-0."""
    ids = _outline_change(qtbot, tmp_path, deleted=["SPEC-4"], modified=["SPEC-0"])
    assert ids == ["SPEC-4", "SPEC-0"], f"rows: {ids}"


def test_srs36_adjacent_deleted_keep_from_order(qtbot, tmp_path):
    """MELLON-VER-SRS-36: SPEC-3 and SPEC-1 (adjacent, in that order at From) deleted, SPEC-2
    modified: rows SPEC-3, SPEC-1, SPEC-2 (From order, not ID order, after SPEC-0's place).
    Assumption: "after X" puts the deleted object directly after X, ahead of the objects that
    follow X at To, so the deleted object shows where it was."""
    ids = _outline_change(qtbot, tmp_path, deleted=["SPEC-3", "SPEC-1"], modified=["SPEC-2"])
    assert ids == ["SPEC-3", "SPEC-1", "SPEC-2"], f"rows: {ids}"


def test_srs36_deleted_after_unchanged_object(qtbot, tmp_path):
    """MELLON-VER-SRS-36: SPEC-1, after the unchanged SPEC-3, deleted; SPEC-4 and SPEC-2
    modified: rows SPEC-4, SPEC-1, SPEC-2. Same assumption on "after X" as above."""
    ids = _outline_change(qtbot, tmp_path, deleted=["SPEC-1"], modified=["SPEC-4", "SPEC-2"])
    assert ids == ["SPEC-4", "SPEC-1", "SPEC-2"], f"rows: {ids}"


# ==========================================================================
# MELLON-VER-SRS-37: hiding rows by change
# ==========================================================================


def _filter_repo(qtbot, tmp_path):
    """SPEC-2 Modified and Moved, SPEC-4 Link reviewed."""
    root = tmp_path / "r"
    folder = _build(root)
    _write_links(folder, "SPEC-4", [("SPEC", "SPEC-1", "refines", "abc1234")])
    before = _commit(root, "link SPEC-4")
    editor.set_description(folder, "SPEC-2", "Requirement 2, revised.")
    editor.move_object(folder, "SPEC-2", to_top=True)
    _write_links(folder, "SPEC-4", [("SPEC", "SPEC-1", "refines", "def5678")])
    _commit(root, "edit, move, review")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    kinds = {c.obj_id: set(c.kinds) for c in dlg.visible_changes()}
    assert kinds == {
        "SPEC-2": {Change.MODIFIED, Change.MOVED},
        "SPEC-4": {Change.LINK_REVIEWED},
    }, f"setup: changes {kinds}"
    return dlg


@pytest.mark.parametrize(
    "untick, expected",
    [
        ([Change.MODIFIED], ["SPEC-2", "SPEC-4"]),
        ([Change.MOVED], ["SPEC-2", "SPEC-4"]),
        ([Change.MODIFIED, Change.MOVED], ["SPEC-4"]),
        ([Change.LINK_REVIEWED], ["SPEC-2"]),
    ],
    ids=["modified", "moved", "modified+moved", "link-reviewed"],
)
def test_srs37_row_hidden_only_when_each_change_hidden(qtbot, tmp_path, untick, expected):
    """MELLON-VER-SRS-37: a row is hidden only when each of its changes is unticked."""
    dlg = _filter_repo(qtbot, tmp_path)
    for change in untick:
        dlg.filter_boxes[change].click()
    QApplication.processEvents()
    assert sorted(_ids(dlg)) == sorted(expected), f"unticked {untick}: rows {_rows(dlg)}"


# ==========================================================================
# MELLON-VER-SRS-40: only the latest From and To's result is shown
# ==========================================================================


ISSUE1_ROWS = [
    ("SPEC-1", "Modified", "The VCU must stop."),
    ("SPEC-2", "Modified", "Requirement 2, third."),
    ("SPEC-3", "Deleted", "Requirement 3."),
    ("SPEC-4", "Added", "Requirement 4."),
]


def _pick(qtbot, combo, ref) -> list:
    """Choose the entry whose data is `ref` as a user does with the arrow keys on the closed
    combo: each key press selects the next entry and emits `activated`. Returns the data of
    each entry stepped onto."""
    targets = [i for i in range(combo.count()) if combo.itemData(i) == ref]
    assert targets, f"setup: no entry {ref!r} in {_entries(combo)}"
    target, stepped = targets[0], []
    for _ in range(combo.count()):
        here = combo.currentIndex()
        if here == target:
            break
        qtbot.keyClick(combo, Qt.Key.Key_Down if here < target else Qt.Key.Key_Up)
        assert combo.currentIndex() != here, "setup: the arrow key didn't move the combo"
        stepped.append(combo.itemData(combo.currentIndex()))
    assert combo.currentIndex() == target, "setup: couldn't reach the entry with the arrow keys"
    return stepped


def _settle(qtbot, dlg, hold, first: str) -> None:
    """Release the held comparison and wait until it and any queued one are done."""
    hold.release.set()
    qtbot.waitUntil(lambda: first in hold.froms_returned(), timeout=10000)
    for _ in range(3):
        _drain()
        _wait(qtbot, dlg)


def _third_commit_repo(root: Path) -> tuple[str, str]:
    """The standard repository plus a third commit changing SPEC-2. Returns (seed, A = edits)."""
    folder = _build(root)
    seed, a = _oid(root, "HEAD~1"), _oid(root, "HEAD")
    editor.set_description(folder, "SPEC-2", "Requirement 2, third.")
    _commit(root, "third")
    return seed, a


A_ROWS = (("SPEC-2", "Modified", "Requirement 2, third."),)


def test_srs40_earlier_result_never_shown_after_from_is_picked(qtbot, tmp_path, holds):
    """MELLON-VER-SRS-40: while From = A is compared (held), the user picks Issue 1 as From with
    the arrow keys; then A's comparison finishes. A's result is never shown, and the rows,
    counts and versions shown are Issue 1's.

    The dialog disables Compare while it compares and runs the picked From afterwards, so A
    can't be made to finish after Issue 1; A is held until the pick is made instead."""
    root = tmp_path / "r"
    seed, a = _third_commit_repo(root)
    win = _window(qtbot, root)
    hold = holds(_service(win), lambda ref_a, ref_b: ref_a == a)
    dlg = win.open_compare(from_ref=a, to_ref=LATEST)
    shown = _ShownResults(dlg)
    try:
        qtbot.waitUntil(lambda: a in hold.froms_entered(), timeout=5000)
        _pick(qtbot, dlg.from_combo, "SPEC-issue-1")
        _settle(qtbot, dlg, hold, a)
    finally:
        shown.stop()
    assert A_ROWS not in shown.rows_seen(), f"A's result was shown at some point: {shown.states}"
    assert _rows(dlg) == ISSUE1_ROWS, f"final rows should be Issue 1's: {_rows(dlg)}"
    assert _counts(dlg.summary_label.text()) == {"added": 1, "deleted": 1, "modified": 2}, (
        f"final counts should be Issue 1's: {dlg.summary_label.text()!r}"
    )
    assert _commit_of(root, dlg.diff.ref_a) == seed, f"result's From should be Issue 1: {dlg.diff.ref_a!r}"
    assert a[:7] not in dlg.summary_label.text(), f"A is named in the summary: {dlg.summary_label.text()!r}"


def test_srs40_typed_from_while_comparing_never_shows_earlier_result(qtbot, tmp_path, holds):
    """MELLON-VER-SRS-40: while From = A is compared (held), the user types Issue 1's tag into
    From; then A's comparison finishes. A's result is never shown.

    Typing a version into From changes From, before Enter or Compare (definitions.md, "From
    or To changes", decided 2026-11-07)."""
    root = tmp_path / "r"
    _, a = _third_commit_repo(root)
    win = _window(qtbot, root)
    hold = holds(_service(win), lambda ref_a, ref_b: ref_a == a)
    dlg = win.open_compare(from_ref=a, to_ref=LATEST)
    shown = _ShownResults(dlg)
    try:
        qtbot.waitUntil(lambda: a in hold.froms_entered(), timeout=5000)
        _type(dlg.from_combo, "SPEC-issue-1")
        _settle(qtbot, dlg, hold, a)
    finally:
        shown.stop()
    assert A_ROWS not in shown.rows_seen(), (
        f"A's result was shown though From reads {dlg.from_combo.currentText()!r}: {shown.states}"
    )


def test_srs40_quick_succession_shows_only_last(qtbot, tmp_path, holds):
    """MELLON-VER-SRS-40: five comparisons with different From in quick succession (seed, then
    four arrow-key picks while seed's is held): only the last one's result is ever shown."""
    root = tmp_path / "r"
    folder = _build(root)
    froms = [_oid(root, "HEAD~1"), _oid(root, "HEAD")]
    for obj_id in ("SPEC-1", "SPEC-2", "SPEC-4", "SPEC-0"):
        editor.set_description(folder, obj_id, f"{obj_id}, changed once more.")
        froms.append(_commit(root, f"change {obj_id}"))
    froms = froms[:5]  # seed, edits, c3, c4, c5; To = c6, the latest commit
    # Worked out by hand: the objects whose files differ between each earlier From and c6.
    earlier = [
        {"SPEC-0", "SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"},
        {"SPEC-0", "SPEC-1", "SPEC-2", "SPEC-4"},
        {"SPEC-0", "SPEC-2", "SPEC-4"},
        {"SPEC-0", "SPEC-4"},
    ]
    win = _window(qtbot, root)
    hold = holds(_service(win), lambda ref_a, ref_b: ref_a == froms[0])
    dlg = win.open_compare(from_ref=froms[0], to_ref=LATEST)
    shown = _ShownResults(dlg)
    try:
        qtbot.waitUntil(lambda: froms[0] in hold.froms_entered(), timeout=5000)
        stepped = _pick(qtbot, dlg.from_combo, froms[4])
        assert stepped == froms[1:], f"setup: the picks should step through {froms[1:]}: {stepped}"
        _settle(qtbot, dlg, hold, froms[0])
    finally:
        shown.stop()
    seen_sets = [{r[0] for r in rows} for rows in shown.rows_seen()]
    leaked = [s for s in seen_sets if s in earlier]
    assert not leaked, f"an earlier From's result was shown: {leaked}"
    assert _ids(dlg) == ["SPEC-0"], f"final rows should be c5 -> latest (SPEC-0 only): {_rows(dlg)}"


# ==========================================================================
# MELLON-VER-SRS-41: the result names its versions
# ==========================================================================


def _named_versions_ok(text: str, typed: str, full: str) -> bool:
    hexes = re.findall(r"\b[0-9a-f]{7,40}\b", text)
    names_commit = typed in text or any(full.startswith(h) for h in hexes)
    return names_commit and "working copy" in text.lower()


def _typed_to_working_copy(qtbot, tmp_path):
    root = tmp_path / "r"
    _build(root, issue=False)
    seed = _oid(root, "HEAD~1")
    win = _window(qtbot, root)
    dlg = win.open_compare()
    QApplication.processEvents()
    _type(dlg.from_combo, seed[:7])
    _choose(dlg, dlg.to_combo, WORKING_COPY)
    _compare(qtbot, dlg)
    assert _row_map(dlg) == STD_DICT, f"setup: rows {_rows(dlg)}"
    return dlg, seed


def test_srs41_result_names_its_versions(qtbot, tmp_path):
    """MELLON-VER-SRS-41: the shown result names the typed version (or its commit) and the
    working copy."""
    dlg, seed = _typed_to_working_copy(qtbot, tmp_path)
    texts = " | ".join(_visible_texts(dlg))
    assert _named_versions_ok(dlg.summary_label.text(), seed[:7], seed), (
        f"the result should name {seed[:7]} and the working copy: {texts!r}"
    )


def test_srs41_copied_list_names_its_versions(qtbot, tmp_path):
    """MELLON-VER-SRS-41 (with -61): the first line Copy list puts on the clipboard names the
    typed version (or its commit) and the working copy."""
    dlg, seed = _typed_to_working_copy(qtbot, tmp_path)
    QApplication.clipboard().clear()
    qtbot.mouseClick(dlg.copy_button, Qt.MouseButton.LeftButton)
    first = (QApplication.clipboard().text().splitlines() or [""])[0]
    assert _named_versions_ok(first, seed[:7], seed), f"first copied line: {first!r}"


# ==========================================================================
# MELLON-VER-SRS-44: a Modified object's fields at From and at To
# ==========================================================================


def _status_change_dialog(qtbot, tmp_path):
    root = tmp_path / "r"
    folder = _build(root)
    editor.add_attribute(folder, "status", "enum", False, options=("Draft", "Approved"))
    editor.set_attribute(folder, "SPEC-1", "status", "Draft")
    before = _commit(root, "status Draft")
    editor.set_attribute(folder, "SPEC-1", "status", "Approved")
    editor.set_description(folder, "SPEC-1", "The VCU must stop at once.")
    editor.add_link(folder, "SPEC-1", "SPEC", "SPEC-2", "refines")
    _commit(root, "approve SPEC-1")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _ids(dlg) == ["SPEC-1"], f"setup: only SPEC-1 changed: {_rows(dlg)}"
    _select(dlg, "SPEC-1")
    return dlg


def test_srs44_attribute_values_under_their_versions(qtbot, tmp_path):
    """MELLON-VER-SRS-44: status Draft under From, Approved under To."""
    dlg = _status_change_dialog(qtbot, tmp_path)
    f, t = _sides(dlg, "status")
    assert (f["text"], t["text"]) == ("Draft", "Approved"), f"status From/To: {f['text']!r} / {t['text']!r}"


def test_srs44_descriptions_under_their_versions(qtbot, tmp_path):
    """MELLON-VER-SRS-44: each description under its own version."""
    dlg = _status_change_dialog(qtbot, tmp_path)
    f, t = _sides(dlg, "Description")
    assert f["text"] == "The VCU must stop.", f"From description: {f['text']!r}"
    assert t["text"] == "The VCU must stop at once.", f"To description: {t['text']!r}"


def test_srs44_added_link_shown(qtbot, tmp_path):
    """MELLON-VER-SRS-44 (with -46): the link added in the same commit is shown as added."""
    dlg = _status_change_dialog(qtbot, tmp_path)
    assert _link_status(dlg, "SPEC-2", "refines") == {"added"}, dlg.details.toPlainText()


# ==========================================================================
# MELLON-VER-SRS-45: attribute labels
# ==========================================================================


def _label_dialog(qtbot, tmp_path, prepare, change):
    root = tmp_path / "r"
    folder = _build(root)
    prepare(folder)
    before = _commit(root, "prepare")
    change(folder)
    _commit(root, "change")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    _select(dlg, "SPEC-1")
    return dlg


def _field_labels(dlg) -> list[str]:
    return [r[0] for r in _two_sided_rows(dlg)]


def _labelled_req_type(qtbot, tmp_path):
    def prepare(folder):
        editor.add_attribute(folder, "req-type", "text", False, label="Requirement Type")

    def change(folder):
        editor.set_attribute(folder, "SPEC-1", "req-type", "Functional")
        editor.set_attribute(folder, "SPEC-1", "rationale", "Because it must.")

    return _label_dialog(qtbot, tmp_path, prepare, change)


def test_srs45_attribute_shown_by_label(qtbot, tmp_path):
    """MELLON-VER-SRS-45: 'req-type' labelled 'Requirement Type' is shown as 'Requirement Type'."""
    dlg = _labelled_req_type(qtbot, tmp_path)
    labels = _field_labels(dlg)
    assert "Requirement Type" in labels, f"rows: {labels}"
    assert "req-type" not in dlg.details.toPlainText(), f"the name shows: {dlg.details.toPlainText()!r}"


def test_srs45_unlabelled_attribute_shown_by_name(qtbot, tmp_path):
    """MELLON-VER-SRS-45: 'rationale', which has no label, is shown as 'rationale'."""
    dlg = _labelled_req_type(qtbot, tmp_path)
    assert "rationale" in _field_labels(dlg), f"rows: {_field_labels(dlg)}"


def test_srs45_label_at_to_wins(qtbot, tmp_path):
    """MELLON-VER-SRS-45: the label at To is used when From's label differs."""
    def prepare(folder):
        editor.add_attribute(folder, "req-type", "text", False, label="Old Type Label")
        editor.set_attribute(folder, "SPEC-1", "req-type", "Functional")

    def change(folder):
        _set_label(folder, "req-type", "Requirement Type")
        editor.set_attribute(folder, "SPEC-1", "req-type", "Safety")

    dlg = _label_dialog(qtbot, tmp_path, prepare, change)
    labels = _field_labels(dlg)
    assert "Requirement Type" in labels and "Old Type Label" not in labels, f"rows: {labels}"


def test_srs45_label_at_from_when_only_from_defines(qtbot, tmp_path):
    """MELLON-VER-SRS-45: an attribute only From defines is shown by From's label."""
    def prepare(folder):
        editor.add_attribute(folder, "legacy", "text", False, label="Legacy Field")
        editor.set_attribute(folder, "SPEC-1", "legacy", "old value")

    def change(folder):
        editor.delete_attribute(folder, "legacy")

    dlg = _label_dialog(qtbot, tmp_path, prepare, change)
    f, t = _sides(dlg, "Legacy Field")
    assert (f["text"], t["text"]) == ("old value", "(not set)"), f"Legacy Field: {f['text']!r} / {t['text']!r}"


def test_srs45_unlabelled_type_attribute_shown_by_built_in_label(qtbot, tmp_path):
    """MELLON-VER-SRS-45: 'type' has no label in SPEC's manifest, so it is shown by its
    built-in label, 'Type' (definitions.md, Label, decided 2026-11-07)."""
    def prepare(folder):
        # Something to commit, away from SPEC-1.
        editor.set_description(folder, "SPEC-2", "Requirement 2, prepared.")

    def change(folder):
        editor.set_attribute(folder, "SPEC-1", "type", "Note")

    dlg = _label_dialog(qtbot, tmp_path, prepare, change)
    labels = _field_labels(dlg)
    assert "Type" in labels and "type" not in labels, f"rows: {labels}"


# ==========================================================================
# MELLON-VER-SRS-46: links added and removed
# ==========================================================================


def test_srs46_links_added_and_removed_shown(qtbot, tmp_path):
    """MELLON-VER-SRS-46: SPEC-1's link to SPEC-2 (refines) shown as added, its link to SPEC-4
    (refines) as removed."""
    root = tmp_path / "r"
    before, after = _relinked_repo(root)
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=after)
    _wait(qtbot, dlg)
    _select(dlg, "SPEC-1")
    assert _link_status(dlg, "SPEC-2", "refines") == {"added"}, dlg.details.toPlainText()
    assert _link_status(dlg, "SPEC-4", "refines") == {"removed"}, dlg.details.toPlainText()


# ==========================================================================
# MELLON-VER-SRS-49: "(not set)"
# ==========================================================================


def test_srs49_unset_at_to_shows_not_set(qtbot, tmp_path):
    """MELLON-VER-SRS-49: SPEC-2's rationale set at From, not set at To: 'Because.' under From,
    '(not set)' under To."""
    root = tmp_path / "r"
    folder = _build(root)
    before = _oid(root, "HEAD")
    _unset_attr(folder, "SPEC-2", "rationale")
    _commit(root, "unset SPEC-2 rationale")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    _select(dlg, "SPEC-2")
    f, t = _sides(dlg, "rationale")
    assert (f["text"], t["text"]) == ("Because.", "(not set)"), f"rationale: {f['text']!r} / {t['text']!r}"


def test_srs49_empty_value_is_not_shown_as_not_set(qtbot, tmp_path):
    """MELLON-VER-SRS-49: SPEC-1's rationale not set at From, set to empty text at To:
    '(not set)' under From, and under To something other than '(not set)'. "Set" includes an
    empty value (definitions.md, Set, not set); the requirement fixes only the unset side's
    wording, and its rationale asks that "an empty value and an unset one must not look
    alike", so how the empty value itself reads is left open."""
    root = tmp_path / "r"
    folder = _build(root)
    before = _oid(root, "HEAD")
    _set_empty_attr(folder, "SPEC-1", "rationale")
    _commit(root, "empty rationale on SPEC-1")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert "SPEC-1" in _ids(dlg), f"unset -> empty is a change of a field: rows {_rows(dlg)}"
    _select(dlg, "SPEC-1")
    f, t = _sides(dlg, "rationale")
    assert f["text"] == "(not set)", f"From should read (not set): {f['text']!r}"
    assert t["text"] != "(not set)", f"To's empty value looks like an unset one: {t['text']!r}"


# ==========================================================================
# MELLON-VER-SRS-50: an Added or Deleted object's fields
# ==========================================================================


def _added_deleted_dialog(qtbot, tmp_path, which: str):
    """D (deleted) and A (added), each with type, status, rationale, a link, a table and a
    figure. Labels are given in the manifest so the expected names are unambiguous."""
    root = tmp_path / "r"
    folder = _build(root)
    editor.add_attr_option(folder, "type", "Requirement")
    editor.add_attribute(folder, "status", "enum", False, options=("Draft", "Approved"), label="Status")
    _set_label(folder, "type", "Type")
    _set_label(folder, "rationale", "Rationale")
    red, blue = tmp_path / "red.svg", tmp_path / "blue.svg"
    red.write_bytes(RED_SVG)
    blue.write_bytes(BLUE_SVG)

    d = editor.create_object(folder, "SPEC-0", "Deleted thing.")
    editor.move_object(folder, d, parent_id="SPEC-0", after="SPEC-1")
    for name, value in (("type", "Requirement"), ("status", "Draft"), ("rationale", "Why D.")):
        editor.set_attribute(folder, d, name, value)
    editor.add_link(folder, d, "SPEC", "SPEC-1", "refines")
    editor.set_table(folder, d, Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("Speed"), TableCell("10 km/h"))),),
    ))
    editor.set_figure(folder, d, red)
    before = _commit(root, "create D")

    a = editor.create_object(folder, "SPEC-0", "Added thing.")
    for name, value in (("type", "Requirement"), ("status", "Approved"), ("rationale", "Why A.")):
        editor.set_attribute(folder, a, name, value)
    editor.add_link(folder, a, "SPEC", "SPEC-2", "refines")
    editor.set_table(folder, a, Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("Mass"), TableCell("5 kg"))),),
    ))
    editor.set_figure(folder, a, blue)
    editor.delete_object(folder, d, force=True)
    _commit(root, "create A, delete D")
    assert ("SPEC-4", "SPEC-0") in _outline(folder) and _outline(folder)[-1] == (a, "SPEC-0"), (
        "setup: A should be SPEC-0's last sub-object, after SPEC-4"
    )

    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    rows = _row_map(dlg)
    assert rows.get(d, ("",))[0] == "Deleted" and rows.get(a, ("",))[0] == "Added", f"setup: rows {rows}"
    obj = d if which == "deleted" else a
    _select(dlg, obj)
    return dlg


EXPECT_50 = {
    "deleted": {
        "attrs": {"Type": "Requirement", "Status": "Draft", "Rationale": "Why D."},
        "description": "Deleted thing.",
        "link": "SPEC-1",
        "cells": ("Speed", "10 km/h"),
        "colour": _is_red,
        "position": ("SPEC-0", "SPEC-1"),
    },
    "added": {
        "attrs": {"Type": "Requirement", "Status": "Approved", "Rationale": "Why A."},
        "description": "Added thing.",
        "link": "SPEC-2",
        "cells": ("Mass", "5 kg"),
        "colour": _is_blue,
        "position": ("SPEC-0", "SPEC-4"),
    },
}


@pytest.mark.parametrize("which", ["added", "deleted"])
def test_srs50_attributes_by_label(qtbot, tmp_path, which):
    """MELLON-VER-SRS-50: each attribute by its label with its value, at the version where the
    object exists (To for the added object, From for the deleted one)."""
    dlg = _added_deleted_dialog(qtbot, tmp_path, which)
    rows = {label: cell["text"] for label, cell in _one_sided_rows(dlg)}
    for label, value in EXPECT_50[which]["attrs"].items():
        assert rows.get(label) == value, f"{label} should read {value!r}: {rows}"


@pytest.mark.parametrize("which", ["added", "deleted"])
def test_srs50_description(qtbot, tmp_path, which):
    """MELLON-VER-SRS-50: the description at the version where the object exists."""
    dlg = _added_deleted_dialog(qtbot, tmp_path, which)
    values = [cell["text"] for _, cell in _one_sided_rows(dlg)]
    assert EXPECT_50[which]["description"] in values, f"description missing: {values}"


@pytest.mark.parametrize("which", ["added", "deleted"])
def test_srs50_link(qtbot, tmp_path, which):
    """MELLON-VER-SRS-50: the outgoing link (target and type) at the version where it exists."""
    dlg = _added_deleted_dialog(qtbot, tmp_path, which)
    pat = _id_re(EXPECT_50[which]["link"])
    values = [cell["text"] for _, cell in _one_sided_rows(dlg)]
    assert any(pat.search(v) and "refines" in v for v in values), f"link missing: {values}"


@pytest.mark.parametrize("which", ["added", "deleted"])
def test_srs50_table(qtbot, tmp_path, which):
    """MELLON-VER-SRS-50: the table's cells at the version where the object exists."""
    dlg = _added_deleted_dialog(qtbot, tmp_path, which)
    first, second = EXPECT_50[which]["cells"]
    values = [cell["text"] for _, cell in _one_sided_rows(dlg)]
    assert any(first in v and second in v for v in values), f"table cells missing: {values}"


@pytest.mark.parametrize("which", ["added", "deleted"])
def test_srs50_figure(qtbot, tmp_path, which):
    """MELLON-VER-SRS-50: the figure at the version where the object exists (red for the
    deleted object's figure, blue for the added one's)."""
    dlg = _added_deleted_dialog(qtbot, tmp_path, which)
    imgs = [src for _, cell in _one_sided_rows(dlg) for src in cell["imgs"]]
    assert len(imgs) == 1, f"one figure image expected: {imgs}"
    rgb = _image_rgb(dlg, imgs[0])
    assert EXPECT_50[which]["colour"](rgb), f"the figure shown isn't this version's: colour {rgb}"


@pytest.mark.parametrize("which", ["added", "deleted"])
def test_srs50_outline_place(qtbot, tmp_path, which):
    """MELLON-VER-SRS-50 (gap list): the place in the outline at the version where the object
    exists. Assumption: the place is shown though the Field definition doesn't list it."""
    dlg = _added_deleted_dialog(qtbot, tmp_path, which)
    parent, after = EXPECT_50[which]["position"]
    values = [cell["text"] for _, cell in _one_sided_rows(dlg)]
    assert any(
        _id_re(parent).search(v) and _id_re(after).search(v) and "refines" not in v for v in values
    ), f"place (under {parent}, after {after}) missing: {values}"


# ==========================================================================
# MELLON-VER-SRS-51: table and figure at From and at To
# ==========================================================================


def _table_or_figure_dialog(qtbot, tmp_path, prepare, change):
    root = tmp_path / "r"
    folder = _build(root)
    prepare(folder, tmp_path)
    before = _commit(root, "prepare")
    change(folder, tmp_path)
    _commit(root, "change")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _row_map(dlg).get("SPEC-1", ("",))[0] == "Modified", f"setup: rows {_rows(dlg)}"
    _select(dlg, "SPEC-1")
    return dlg


def _speed_table(value: str) -> Table:
    return Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("Speed"), TableCell(value))),),
    )


def _figure_row(dlg) -> tuple[dict, dict]:
    rows = [(f, t) for _, f, t in _two_sided_rows(dlg) if f["imgs"] or t["imgs"]]
    assert rows, f"no row with a figure under From/To: {dlg.details.toPlainText()!r}"
    return rows[0]


def test_srs51_table_cell_change_under_each_version(qtbot, tmp_path):
    """MELLON-VER-SRS-51: a cell '10 km/h' changed to '20 km/h': each value under its version."""
    dlg = _table_or_figure_dialog(
        qtbot, tmp_path,
        lambda folder, _: editor.set_table(folder, "SPEC-1", _speed_table("10 km/h")),
        lambda folder, _: editor.set_table(folder, "SPEC-1", _speed_table("20 km/h")),
    )
    rows = [(f, t) for _, f, t in _two_sided_rows(dlg) if "km/h" in f["text"] + t["text"]]
    assert rows, f"no table row under From/To: {dlg.details.toPlainText()!r}"
    f, t = rows[0]
    assert "10 km/h" in f["text"] and "20 km/h" not in f["text"], f"From table: {f['text']!r}"
    assert "20 km/h" in t["text"] and "10 km/h" not in t["text"], f"To table: {t['text']!r}"


def _put_figure(folder: Path, tmp_path: Path, svg: bytes, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(svg)
    editor.set_figure(folder, "SPEC-1", path)


def test_srs51_replaced_figure_at_both_versions(qtbot, tmp_path):
    """MELLON-VER-SRS-51: a figure replaced by a different SVG: the From figure (red) under
    From, the To figure (blue) under To."""
    dlg = _table_or_figure_dialog(
        qtbot, tmp_path,
        lambda folder, tp: _put_figure(folder, tp, RED_SVG, "red.svg"),
        lambda folder, tp: _put_figure(folder, tp, BLUE_SVG, "blue.svg"),
    )
    f, t = _figure_row(dlg)
    assert len(f["imgs"]) == 1 and len(t["imgs"]) == 1, f"one image per side expected: {f} / {t}"
    assert _is_red(_image_rgb(dlg, f["imgs"][0])), "the From figure should be the red one"
    assert _is_blue(_image_rgb(dlg, t["imgs"][0])), "the To figure should be the blue one"


def test_srs51_removed_figure_says_none_at_to(qtbot, tmp_path):
    """MELLON-VER-SRS-51: a figure removed: the image under From, and To says there is none
    (wording assumed to contain "no figure", as the gap list gives it)."""
    dlg = _table_or_figure_dialog(
        qtbot, tmp_path,
        lambda folder, tp: _put_figure(folder, tp, RED_SVG, "red.svg"),
        lambda folder, _: editor.clear_figure(folder, "SPEC-1"),
    )
    f, t = _figure_row(dlg)
    assert len(f["imgs"]) == 1 and _is_red(_image_rgb(dlg, f["imgs"][0])), f"From figure: {f}"
    assert t["imgs"] == [], f"no image expected under To: {t}"
    assert "no figure" in t["text"].lower(), f"To should say there is no figure: {t['text']!r}"


# ==========================================================================
# MELLON-VER-SRS-52: a Moved object's place
# ==========================================================================


def _place_repo(root: Path) -> tuple[Path, str]:
    """SPEC-0 (top) over SPEC-1, SPEC-2, SPEC-3; SPEC-4 (top) with no sub-object."""
    pygit2.init_repository(str(root))
    folder = editor.create_document(root, "SPEC", title="Spec")
    for i in range(5):
        assert editor.create_object(folder, None, f"Object {i}.") == f"SPEC-{i}"
    editor.move_object(folder, "SPEC-1", parent_id="SPEC-0")
    editor.move_object(folder, "SPEC-2", parent_id="SPEC-0", after="SPEC-1")
    editor.move_object(folder, "SPEC-3", parent_id="SPEC-0", after="SPEC-2")
    assert _outline(folder) == [
        ("SPEC-0", None), ("SPEC-1", "SPEC-0"), ("SPEC-2", "SPEC-0"), ("SPEC-3", "SPEC-0"), ("SPEC-4", None),
    ], "setup: outline not built as intended"
    return folder, _commit(root, "base")


def _place_cells(qtbot, root: Path, base: str, obj_id: str) -> tuple[str, str]:
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=base, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _rows(dlg) and _rows(dlg)[0][:2] == (obj_id, "Moved") and len(_rows(dlg)) == 1, (
        f"setup: only {obj_id} Moved expected: {_rows(dlg)}"
    )
    _select(dlg, obj_id)
    rows = _two_sided_rows(dlg)
    assert rows, f"no From/To row for the place: {dlg.details.toPlainText()!r}"
    return rows[0][1]["text"], rows[0][2]["text"]


def _ids_in(text: str) -> set[str]:
    return set(re.findall(r"SPEC-\d+", text))


def test_srs52_moved_to_another_parent(qtbot, tmp_path):
    """MELLON-VER-SRS-52: SPEC-3 moved from under SPEC-0 (after SPEC-2) to SPEC-4's first
    sub-object: From names SPEC-0 and SPEC-2; To names SPEC-4 only (first, nothing before)."""
    root = tmp_path / "r"
    folder, base = _place_repo(root)
    editor.move_object(folder, "SPEC-3", parent_id="SPEC-4")
    assert ("SPEC-3", "SPEC-4") in _outline(folder), "setup: SPEC-3 not under SPEC-4"
    _commit(root, "move SPEC-3")
    f, t = _place_cells(qtbot, root, base, "SPEC-3")
    assert _ids_in(f) == {"SPEC-0", "SPEC-2"}, f"From place should be under SPEC-0 after SPEC-2: {f!r}"
    assert _ids_in(t) == {"SPEC-4"}, f"To place should be first under SPEC-4: {t!r}"


def test_srs52_moved_to_top_level(qtbot, tmp_path):
    """MELLON-VER-SRS-52: SPEC-2 moved to the top level after SPEC-4: From names SPEC-0 and
    SPEC-1; To names SPEC-4 and not the old parent SPEC-0."""
    root = tmp_path / "r"
    folder, base = _place_repo(root)
    editor.move_object(folder, "SPEC-2", to_top=True, after="SPEC-4")
    assert _outline(folder)[-1] == ("SPEC-2", None), f"setup: outline {_outline(folder)}"
    _commit(root, "move SPEC-2 to top")
    f, t = _place_cells(qtbot, root, base, "SPEC-2")
    assert _ids_in(f) == {"SPEC-0", "SPEC-1"}, f"From place should be under SPEC-0 after SPEC-1: {f!r}"
    assert _ids_in(t) == {"SPEC-4"}, f"To place should be top level after SPEC-4: {t!r}"


# ==========================================================================
# MELLON-VER-SRS-53: a Link reviewed object's acknowledged commits
# ==========================================================================


def test_srs53_link_reviewed_shows_ack_at_from_and_to(qtbot, tmp_path):
    """MELLON-VER-SRS-53: SPEC-4's link to SPEC-1 (refines) re-acknowledged abc1234 -> def5678:
    the details show the target, the type, abc1234 at From and def5678 at To."""
    root = tmp_path / "r"
    folder = _build(root)
    _write_links(folder, "SPEC-4", [("SPEC", "SPEC-1", "refines", "abc1234")])
    before = _commit(root, "link")
    _write_links(folder, "SPEC-4", [("SPEC", "SPEC-1", "refines", "def5678")])
    _commit(root, "review link")
    win = _window(qtbot, root)
    dlg = win.open_compare(from_ref=before, to_ref=LATEST)
    _wait(qtbot, dlg)
    assert _rows(dlg) == [("SPEC-4", "Link reviewed", "Requirement 4.")], f"setup: rows {_rows(dlg)}"
    _select(dlg, "SPEC-4")
    two = [(f, t) for _, f, t in _two_sided_rows(dlg) if "abc1234" in f["text"] + t["text"]]
    if two:  # shown under From and To columns
        f, t = two[0]
        assert "abc1234" in f["text"] and "def5678" in t["text"], f"acks: {f['text']!r} / {t['text']!r}"
        return
    pat = _id_re("SPEC-1")
    lines = [l for l in dlg.details.toPlainText().splitlines() if pat.search(l) and "refines" in l]
    assert lines, f"no line for the link to SPEC-1 (refines): {dlg.details.toPlainText()!r}"
    line = lines[0]
    assert "abc1234" in line and "def5678" in line, f"both acknowledged commits expected: {line!r}"
    assert line.index("abc1234") < line.index("def5678"), f"From's commit should come first: {line!r}"


# ==========================================================================
# MELLON-VER-SRS-54: double-click selects in the Document Editor
# ==========================================================================


def test_srs54_double_click_selects_in_editor(qtbot, tmp_path):
    """MELLON-VER-SRS-54: a real mouse double-click on SPEC-1's row selects SPEC-1 in the
    Document Editor."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    win.show()
    win.controller.select_object("SPEC", "SPEC-2")
    assert win.controller.current_obj_id() == "SPEC-2", "setup: SPEC-2 should be selected"
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    qtbot.waitUntil(dlg.isVisible, timeout=2000)
    items = [dlg.list.topLevelItem(i) for i in range(dlg.list.topLevelItemCount())]
    (item,) = [
        it for it in items if any(it.text(c) == "SPEC-1" for c in range(dlg.list.columnCount()))
    ]
    rect = dlg.list.visualItemRect(item)
    assert rect.isValid(), "setup: SPEC-1's row has no rectangle"
    viewport, left, at = dlg.list.viewport(), Qt.MouseButton.LeftButton, rect.center()
    # What a mouse sends for a double-click: press, release, double-click, release.
    # (QTest.mouseDClick alone sends only the double-click event.)
    qtbot.mousePress(viewport, left, pos=at)
    qtbot.mouseRelease(viewport, left, pos=at)
    qtbot.mouseDClick(viewport, left, pos=at)
    qtbot.mouseRelease(viewport, left, pos=at)
    qtbot.waitUntil(lambda: win.controller.current_obj_id() == "SPEC-1", timeout=3000)


# ==========================================================================
# MELLON-VER-SRS-58: responsive while comparing
# ==========================================================================


def test_srs58_editor_and_dialog_respond_while_comparing(qtbot, tmp_path, holds):
    """MELLON-VER-SRS-58: while a comparison is held, a description edit in the Document Editor
    reaches the grid and the disk, and unticking Modified hides rows, before the release."""
    root = tmp_path / "r"
    folder = _build(root)
    seed = _oid(root, "HEAD~1")
    win = _window(qtbot, root)
    win.show()
    read_only = _action(win, "Read-only")
    if read_only.isChecked():
        read_only.trigger()
    assert not read_only.isChecked(), "setup: the editor should leave read-only mode"
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    assert _row_map(dlg) == STD_DICT, f"setup: rows {_rows(dlg)}"
    hold = holds(_service(win), lambda ref_a, ref_b: ref_a == seed)
    _choose(dlg, dlg.from_combo, seed)
    qtbot.mouseClick(dlg.compare_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: seed in hold.froms_entered(), timeout=5000)

    model = win.grid_pane.table.model()
    headers = [model.headerData(c, Qt.Orientation.Horizontal) for c in range(model.columnCount())]
    id_col, desc_col = headers.index("ID"), headers.index("Description")
    (row,) = [r for r in range(model.rowCount()) if model.index(r, id_col).data() == "SPEC-1"]
    text = "Edited while comparing."
    model.setData(model.index(row, desc_col), text, Qt.ItemDataRole.EditRole)
    qtbot.waitUntil(lambda: _disk_description(folder, "SPEC-1") == text, timeout=5000)
    qtbot.waitUntil(lambda: text in str(model.index(row, desc_col).data()), timeout=5000)

    dlg.filter_boxes[Change.MODIFIED].click()
    qtbot.waitUntil(lambda: _ids(dlg) == ["SPEC-3", "SPEC-4"] or sorted(_ids(dlg)) == ["SPEC-3", "SPEC-4"],
                    timeout=5000)

    assert seed not in hold.froms_returned(), "the comparison was released before the checks"
    assert dlg.is_busy(), "the dialog should still be comparing"
    hold.release.set()
    qtbot.waitUntil(lambda: not dlg.is_busy(), timeout=10000)


# ==========================================================================
# MELLON-VER-SRS-61: Copy list
# ==========================================================================


def _copy(qtbot, dlg) -> list[str]:
    QApplication.clipboard().clear()
    qtbot.mouseClick(dlg.copy_button, Qt.MouseButton.LeftButton)
    return QApplication.clipboard().text().splitlines()


def test_srs61_copy_button_puts_rows_on_clipboard(qtbot, tmp_path):
    """MELLON-VER-SRS-61: the Copy list button puts each shown row (ID, change, text) on the
    clipboard as plain text."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    lines = _copy(qtbot, dlg)
    for obj_id, (change, text) in STD_ROWS.items():
        pat = _id_re(obj_id)
        assert any(pat.search(l) and change in l and text in l for l in lines), (
            f"no copied line for {obj_id} {change} {text!r}: {lines}"
        )


def test_srs61_copy_leaves_out_hidden_rows(qtbot, tmp_path):
    """MELLON-VER-SRS-61: with Added unticked, the copied list leaves SPEC-4 (Added) out."""
    root = tmp_path / "r"
    _build(root)
    win = _window(qtbot, root)
    dlg = win.open_compare()
    _wait(qtbot, dlg)
    dlg.filter_boxes[Change.ADDED].click()
    qtbot.waitUntil(lambda: "SPEC-4" not in _ids(dlg), timeout=2000)
    lines = _copy(qtbot, dlg)
    pat = _id_re("SPEC-4")
    assert not any(pat.search(l) for l in lines), f"hidden SPEC-4 was copied: {lines}"
    assert any(_id_re("SPEC-1").search(l) for l in lines), f"shown SPEC-1 missing: {lines}"


# ==========================================================================
# V12: end to end on the req-test fixture (MELLON-VER-SRS-31, -32, -44)
# ==========================================================================


# Worked out by hand from `git diff VCU3-SRS-issue-1 HEAD -- VCU3-SRS`: the outline
# is the same at both commits; 21 objects changed their `type` and gained
# `req-type` (some links were re-acknowledged too, which is part of Modified);
# no object was added or deleted. Rows in outline order, texts as at HEAD.
V12_ROWS = [
    ("VCU3-SRS-1", "The VCU shall generate a FAULT_OVERTEMP signal within 200 ms of the coolant temperature exceeding 106 degC."),
    ("VCU3-SRS-2", "The VCU shall sample the coolant temperature at a rate of at least 20 Hz."),
    ("VCU3-SRS-11", "The VCU shall apply a 3 degC hysteresis band when clearing the FAULT_OVERTEMP signal."),
    ("VCU3-SRS-12", "The VCU shall latch FAULT_OVERTEMP until an explicit reset command is received via CAN."),
    ("VCU3-SRS-13", "The VCU shall report a sensor-fault code (FAULT_OVERTEMP_SENSOR) if the coolant temperature reading is out of range (<-40 degC or >150 degC)."),
    ("VCU3-SRS-10", "The VCU software shall be written in C and conform to MISRA C:2012."),
    ("VCU3-SRS-15", "The VCU shall monitor the 12 V supply rail continuously and declare under-voltage at <9 V for longer than 50 ms."),
    ("VCU3-SRS-16", "The VCU shall request a controlled shutdown if the 12 V supply falls below 7 V."),
    ("VCU3-SRS-17", "The VCU shall perform a power-on self-test of RAM, ROM, and all analog inputs within 500 ms of boot."),
    ("VCU3-SRS-18", "The VCU shall enter a low-power sleep state within 25 s of ignition-off."),
    ("VCU3-SRS-20", "The VCU shall refresh the hardware watchdog from the main loop at intervals not exceeding 50 ms."),
    ("VCU3-SRS-21", "The VCU shall not refresh the watchdog from an interrupt context."),
    ("VCU3-SRS-23", "The VCU shall configure the CAN controller for 500 kbit/s, sample point 87.5%."),
    ("VCU3-SRS-24", "The VCU shall broadcast a status message with ID 0x7E0 every 100 ms when the bus is active."),
    ("VCU3-SRS-25", "The VCU shall implement ISO-TP (ISO 15765-2) for diagnostic sessions."),
    ("VCU3-SRS-26", "The VCU shall re-enter the error-active state within 200 ms of recovering from bus-off."),
    ("VCU3-SRS-27", "The VCU shall log CAN error frames to the event log with timestamp and peer node id."),
    ("VCU3-SRS-29", "The VCU shall illuminate the warning lamp within 200 ms of any fault transitioning to active state."),
    ("VCU3-SRS-30", "The VCU shall blink the warning lamp at 2 Hz during boot self-test."),
    ("VCU3-SRS-32", "All VCU source files shall pass static analysis (Cppcheck, clang-tidy) with zero warnings at the project-defined severity threshold."),
    ("VCU3-SRS-33", "The VCU software shall not use dynamic memory allocation (malloc / new) in any post-init code path."),
]


@pytest.fixture
def vcu3_srs_dialog(qtbot, tmp_path, clean_worktree):
    _ensure_req_test_fixture()
    root = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, root)
    clean_worktree(root)
    win = DocumentEditorWindow("VCU3-SRS", DocumentService(root, cache=True))
    qtbot.addWidget(win)
    dlg = win.open_compare(from_ref="VCU3-SRS-issue-1", to_ref=LATEST)
    _wait(qtbot, dlg)
    return dlg


def test_v12_rows_from_issue1_to_latest(vcu3_srs_dialog):
    """V12 (MELLON-VER-SRS-31, -32, -35): VCU3-SRS from Issue 1 to the latest commit: one
    Modified row per changed object, in outline order, with its first line at To."""
    expected = [(obj_id, "Modified", text) for obj_id, text in V12_ROWS]
    assert _rows(vcu3_srs_dialog) == expected, f"rows: {_rows(vcu3_srs_dialog)}"


def test_v12_counts(vcu3_srs_dialog):
    """V12 (MELLON-VER-SRS-33): 21 modified, nothing else."""
    text = vcu3_srs_dialog.summary_label.text()
    assert _counts(text) == {"modified": 21}, f"counts: {text!r}"


@pytest.mark.parametrize(
    "obj_id, type_from, req_type_to",
    [("VCU3-SRS-1", "Functional", "Safety"), ("VCU3-SRS-10", "Design Constraint", "Design Constraint")],
)
def test_v12_details_spot_check(vcu3_srs_dialog, obj_id, type_from, req_type_to):
    """V12 (MELLON-VER-SRS-44, -45, -49): type changed to Requirement and req-type added, each
    under its version and named by its label at To ('Type', 'Requirement Type')."""
    dlg = vcu3_srs_dialog
    _select(dlg, obj_id)
    f, t = _sides(dlg, "Type")
    assert (f["text"], t["text"]) == (type_from, "Requirement"), f"Type: {f['text']!r} / {t['text']!r}"
    f, t = _sides(dlg, "Requirement Type")
    assert (f["text"], t["text"]) == ("(not set)", req_type_to), (
        f"Requirement Type: {f['text']!r} / {t['text']!r}"
    )


# --------------------------------------------------------------------------
# MELLON-VER-SRS-47: word marking
# --------------------------------------------------------------------------


class _TextOf(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def test_ver47_dialog_shows_lt_and_amp_literally(qapp):
    """MELLON-VER-SRS-47 (dialog side): the marked-up description 'a < b & c' -> 'a < d & c'
    reads, as text, exactly 'a < b & c' at From and 'a < d & c' at To, so '<' and '&'
    are shown literally. (The theme tokens are stand-ins: every token is one colour.)"""
    from mellon.gui.dialogs.compare_dialog import words_html

    tokens = defaultdict(lambda: "#808080")
    shown = []
    for side in ("a", "b"):
        parser = _TextOf()
        parser.feed(words_html("a < b & c", "a < d & c", side, tokens))
        parser.close()
        shown.append("".join(parser.parts).split())
    assert shown == [["a", "<", "b", "&", "c"], ["a", "<", "d", "&", "c"]], f"dialog text read {shown}"


# --- written blind from MELLON-VER-SRS (round 2) -----------------------------
#
# MELLON-VER-SRS-9, -22, -56, -67 and -73 through the GUI, offscreen. Written
# from the requirements, docs/definitions.md and the public signatures alone,
# without the code under test or the bodies of other tests; every expected
# value comes from a requirement or a definition. Repositories are built in
# tmp_path with fixed commit signatures; no real clock, no network.


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def _object_file(root: Path, obj_id: str) -> Path:
    return root / DOC / "objects" / f"{obj_id}.xml"


def _r2_disk_description(root: Path, obj_id: str) -> str:
    """The description an object's file holds in the working copy."""
    tree = etree.parse(str(_object_file(root, obj_id)))
    node = tree.getroot().find("description")
    return "" if node is None or node.text is None else node.text


@pytest.fixture
def rev_repo(rev_template, tmp_path):
    src, info = rev_template
    dst = tmp_path / "repo"
    shutil.copytree(src, dst)
    return dst, info


def _added(info: dict, after: range) -> dict[str, str]:
    return {info["ids"][j]: "Added" for j in after}


# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------


def _open_editor(qtbot, root: Path, *, editable: bool = False) -> DocumentEditorWindow:
    win = DocumentEditorWindow(DOC, DocumentService(root, cache=True))
    qtbot.addWidget(win)
    win.show()
    QApplication.processEvents()
    if editable:
        banner = win.findChild(ReadOnlyBanner)
        button = next(b for b in banner.findChildren(QAbstractButton) if b.text() == "Enable editing")
        button.click()
        qtbot.waitUntil(lambda: not banner.isVisible(), timeout=5000)
    return win


def _r2_settle(qtbot, dlg) -> None:
    qtbot.waitUntil(lambda: not dlg.is_busy(), timeout=20_000)
    QApplication.processEvents()


def _open_compare(qtbot, win, from_ref=None, to_ref=None):
    dlg = win.open_compare(from_ref, to_ref)
    _r2_settle(qtbot, dlg)
    return dlg


def _press_compare(qtbot, dlg) -> None:
    """Press Compare without moving the keyboard focus, as a shortcut does."""
    dlg.compare_button.click()
    _r2_settle(qtbot, dlg)


def _r2_rows(dlg) -> dict[str, tuple[str, str]]:
    """The dialog's rows as {object ID: (change, text)}."""
    tree = dlg.list
    head = {tree.headerItem().text(c): c for c in range(tree.columnCount())}
    out: dict[str, tuple[str, str]] = {}

    def walk(item) -> None:
        if not item.isHidden():
            out[item.text(head["ID"])] = (item.text(head["Change"]), item.text(head["Text"]))
        for i in range(item.childCount()):
            walk(item.child(i))

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    return out


def _changes(dlg) -> dict[str, str]:
    return {obj_id: change for obj_id, (change, _) in _r2_rows(dlg).items()}


def _result_text(dlg) -> str:
    """What the dialog shows besides its rows, with no row selected: the title and
    each visible label, line edit and text pane, apart from the version pickers
    and the selected object's details."""
    excluded = (dlg.from_combo, dlg.to_combo, dlg.details)
    parts = [dlg.windowTitle()]
    for w in dlg.findChildren(QWidget):
        if not w.isVisible() or any(x is w or x.isAncestorOf(w) for x in excluded):
            continue
        if isinstance(w, QLabel):
            parts.append(w.text())
        elif isinstance(w, QLineEdit):
            parts.append(w.text())
        elif isinstance(w, QTextEdit):
            parts.append(w.toPlainText())
    return html.unescape(re.sub(r"<[^>]+>", " ", "\n".join(parts)))


def _grid_view(win) -> QAbstractItemView:
    grid = win.findChild(GridPane)
    return next(v for v in grid.findChildren(QAbstractItemView) if not isinstance(v, QHeaderView))


def _type_without_leaving(qtbot, win, obj_id: str, text: str):
    """Open `obj_id`'s description cell in the grid and type `text` at its end,
    leaving the cell editor open: a pending edit. Returns (view, editor widget)."""
    view = _grid_view(win)
    model = view.model()
    cols = {model.headerData(c, Qt.Horizontal, Qt.DisplayRole): c for c in range(model.columnCount())}
    row = next(r for r in range(model.rowCount()) if model.data(model.index(r, cols["ID"])) == obj_id)
    index = model.index(row, cols["Description"])
    view.setCurrentIndex(index)
    view.edit(index)
    qtbot.waitUntil(lambda: view.state() == QAbstractItemView.State.EditingState, timeout=5000)
    widget = view.indexWidget(index) or QApplication.focusWidget()
    QTest.keyClick(widget, Qt.Key_End, Qt.ControlModifier)
    QTest.keyClicks(widget, text)
    QApplication.processEvents()
    return view, widget


def _editor_text(widget) -> str | None:
    """The text in a cell editor, or None once the editor is gone."""
    if widget is None or not shiboken6.isValid(widget):
        return None
    if hasattr(widget, "toPlainText"):
        return widget.toPlainText()
    return widget.text()


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-9: typed text that git's revision syntax resolves to one commit
# ---------------------------------------------------------------------------


# (case, typed text or a function of the repository info, the k of the commit Ck it names)
TYPED_FROM: list[tuple[str, object, object]] = [
    ("full hash", lambda i: i["commits"][2], 2),
    ("4-digit unique prefix", lambda i: i["prefix"][0], lambda i: i["prefix"][1]),
    ("annotated tag", "v1", 1),
    ("lightweight tag", "rel-2", 2),
    ("local branch", "feature", 2),
    ("local branch, full ref name", "refs/heads/feature", 2),
    ("remote-tracking branch", "origin/review", 3),
    ("remote-tracking branch, full ref name", "refs/remotes/origin/review", 3),
    ("HEAD~1", "HEAD~1", 4),
    ("HEAD^", "HEAD^", 4),
    ("HEAD^^", "HEAD^^", 3),
    ("HEAD~5, the first commit", "HEAD~5", 0),
    ("@~2", "@~2", 3),
    ("<branch>~2", "main~2", 3),
    ("<branch>~2 on another branch", "feature~2", 0),
    ("<tag>^{commit}", "v1^{commit}", 1),
    ("<tag>^{}", "v1^{}", 1),
    ("<tag>~1", "rel-2~1", 1),
    ("HEAD^{/message}", "HEAD^{/object 3}", 3),
    (":/message", ":/object 1", 1),
]


def _resolve(value, info):
    return value(info) if callable(value) else value


def _from_latest(qtbot, root: Path, info: dict):
    """The dialog comparing C5 with the latest commit: no rows. Each typed version
    below names an older commit, so rows can only come from the typed text."""
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win, info["commits"][5], diff.LATEST)
    assert _changes(dlg) == {}, f"precondition: C5 to the latest commit lists {_r2_rows(dlg)}"
    return dlg


@pytest.mark.parametrize("case, typed, k", TYPED_FROM, ids=[c[0] for c in TYPED_FROM])
def test_9_typed_from_compares_the_commit_it_names(qtbot, rev_repo, case, typed, k):
    """MELLON-VER-SRS-9: text typed as From that git's revision syntax resolves to
    one commit Ck is compared as Ck. To is the latest commit (C5), so the rows
    must be SPEC-(k+1)..SPEC-5, each Added, and nothing else. Each expected
    commit was checked with `git rev-parse`."""
    root, info = rev_repo
    text, k = _resolve(typed, info), _resolve(k, info)
    dlg = _from_latest(qtbot, root, info)
    dlg.from_combo.setEditText(text)
    dlg.to_combo.setCurrentIndex(dlg.to_combo.findData(diff.LATEST))
    _press_compare(qtbot, dlg)
    expected = _added(info, range(k + 1, 6))
    shown = _changes(dlg)
    assert shown == expected, (
        f"From typed as {text!r} ({case}) names C{k}; comparing it with the latest commit should "
        f"list {expected}, the dialog lists {shown}. Message: {dlg.message_label.text()!r}"
    )


TYPED_TO: list[tuple[str, object, object]] = [
    ("HEAD~2", "HEAD~2", 3),
    ("local branch", "feature", 2),
    ("remote-tracking branch", "origin/review", 3),
    ("<tag>^{commit}", "v1^{commit}", 1),
    ("4-digit unique prefix", lambda i: i["prefix"][0], lambda i: i["prefix"][1]),
]


@pytest.mark.parametrize("case, typed, k", TYPED_TO, ids=[c[0] for c in TYPED_TO])
def test_9_typed_to_compares_the_commit_it_names(qtbot, rev_repo, case, typed, k):
    """MELLON-VER-SRS-9, the To side: From is C0's full hash, To is typed text that
    resolves to Ck, so the rows must be SPEC-1..SPEC-k, each Added."""
    root, info = rev_repo
    text, k = _resolve(typed, info), _resolve(k, info)
    dlg = _from_latest(qtbot, root, info)
    dlg.from_combo.setEditText(info["commits"][0])
    dlg.to_combo.setEditText(text)
    _press_compare(qtbot, dlg)
    expected = _added(info, range(1, k + 1))
    shown = _changes(dlg)
    assert shown == expected, (
        f"To typed as {text!r} ({case}) names C{k}; From C0 to it should list {expected}, "
        f"the dialog lists {shown}. Message: {dlg.message_label.text()!r}"
    )


NOT_A_COMMIT = [
    ("no such name", "no-such-branch"),
    ("past the first commit", "HEAD~6"),
    ("a tree", "HEAD^{tree}"),
    ("the tree of a commit", "HEAD:"),
    ("a file at a commit", "HEAD:SPEC/SPEC.xml"),
]


def _bad_after_good(qtbot, rev_repo, bad: str):
    """Compare From C2 (typed full hash) with the latest commit, then type `bad` as
    From and press Compare. Returns (dialog, rows of the good result)."""
    root, info = rev_repo
    dlg = _from_latest(qtbot, root, info)
    dlg.from_combo.setEditText(info["commits"][2])
    _press_compare(qtbot, dlg)
    good = _r2_rows(dlg)
    assert _changes(dlg) == _added(info, range(3, 6)), "precondition: the first comparison failed"
    dlg.from_combo.setEditText(bad)
    _press_compare(qtbot, dlg)
    return dlg, good


@pytest.mark.parametrize("case, bad", NOT_A_COMMIT, ids=[c[0] for c in NOT_A_COMMIT])
def test_9_text_naming_no_commit_is_reported_by_name(qtbot, rev_repo, case, bad):
    """MELLON-VER-SRS-9's boundary, with -10: text that resolves to no commit
    (nothing, or a tree or file rather than a commit) isn't accepted; the dialog
    shows an error message naming the typed text."""
    dlg, _ = _bad_after_good(qtbot, rev_repo, bad)
    shown = _result_text(dlg)
    assert bad in shown, f"typed {bad!r} ({case}) names no commit; no message names it. The dialog shows: {shown!r}"


@pytest.mark.parametrize("case, bad", NOT_A_COMMIT, ids=[c[0] for c in NOT_A_COMMIT])
def test_9_text_naming_no_commit_is_not_compared(qtbot, rev_repo, case, bad):
    """MELLON-VER-SRS-9's boundary, with -10 and -11: text that resolves to no
    commit isn't compared as something else, so the previous rows stay."""
    dlg, good = _bad_after_good(qtbot, rev_repo, bad)
    assert _r2_rows(dlg) == good, (
        f"typed {bad!r} ({case}) names no commit, yet the rows changed from {good} to {_r2_rows(dlg)}"
    )


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-22: differences gone in the canonical form are no change
# ---------------------------------------------------------------------------


def test_22_compare_dialog_lists_no_row_for_canonical_only_rewrite(qtbot, canon_repo):
    """MELLON-VER-SRS-22 through the Compare dialog: every document file except the
    figure rewritten in all the ways at once; the dialog lists no row."""
    root, info = canon_repo
    _rewrite(root, "**/*.xml", _everything)
    c2 = _r2_commit(root, "rewrite everything")
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win, info["c1"], c2)
    assert dlg.diff is not None, f"precondition: no result. Message: {dlg.message_label.text()!r}"
    assert _r2_rows(dlg) == {}, f"a canonical-only rewrite is no change, the dialog lists {_r2_rows(dlg)}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-56: comparing changes nothing but by saving pending edits
# ---------------------------------------------------------------------------


PENDING = " Typed, not saved."


def _scenario_two_commits(qtbot, win, info):
    dlg = _open_compare(qtbot, win, "side", diff.LATEST)
    dlg.from_combo.setEditText("v1^{commit}")
    _press_compare(qtbot, dlg)


def _scenario_working_copy_pending(qtbot, win, info):
    dlg = _open_compare(qtbot, win, diff.LATEST, WORKING_COPY)
    _type_without_leaving(qtbot, win, info["ids"][2], PENDING)
    _press_compare(qtbot, dlg)


def _scenario_two_commits_pending(qtbot, win, info):
    dlg = _open_compare(qtbot, win, "side", diff.LATEST)
    _type_without_leaving(qtbot, win, info["ids"][2], PENDING)
    _press_compare(qtbot, dlg)


SCENARIOS = [
    ("two commits", _scenario_two_commits, False),
    ("working copy, pending edit", _scenario_working_copy_pending, True),
    ("two commits, pending edit", _scenario_two_commits_pending, True),
]


@pytest.mark.parametrize("part", PARTS)
@pytest.mark.parametrize("case, run, pending", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_56_compare_dialog_changes_only_what_a_pending_edit_saves(qtbot, tmp_path, case, run, pending, part):
    """MELLON-VER-SRS-56 through the Compare dialog, in a working copy that differs
    from the index, which differs from HEAD. Comparing changes nothing in `part`,
    except that saving a pending edit may write the edited object's file. Assumed:
    a description edit saves only the object's own file."""
    root = tmp_path / "repo"
    info = _build_dirty_repo(root)
    win = _open_editor(qtbot, root, editable=True)
    before = _snapshot(root)
    run(qtbot, win, info)
    after = _snapshot(root)
    allowed = {f"{DOC}/objects/{info['ids'][2]}.xml"} if pending and part == "working copy" else set()
    extra = _changed(before, after, part) - allowed
    assert not extra, f"comparing ({case}) changed {sorted(extra)} in the {part}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-67: each result reports the commit each version resolved to,
# "by at least the first 7 digits of its hash" (settled with the person on
# 2026-11-07). The tests accept any run of 7 to 40 hex digits that begins the
# right commit's hash. They don't tie a hash to its side, but each side's
# commit differs, so the other side's hash can't stand in for it.
#
# MELLON-VER-SRS-87: the MCP server's compare tool returns the full hash.
# ---------------------------------------------------------------------------


def _typed_result(qtbot, root: Path, from_text: str, to_text: str | None):
    """Open the Compare dialog on its defaults, then type From and To (None picks
    the working copy for To) and press Compare."""
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win)
    dlg.from_combo.setEditText(from_text)
    if to_text is None:
        dlg.to_combo.setCurrentIndex(dlg.to_combo.findData(WORKING_COPY))
    else:
        dlg.to_combo.setEditText(to_text)
    _press_compare(qtbot, dlg)
    return dlg


# (case, From typed, To typed, k of From's commit, k of To's commit)
TYPED_PAIRS = [
    ("branch to HEAD~1", "feature", "HEAD~1", 2, 4),
    ("annotated tag to remote-tracking branch", "v1", "origin/review", 1, 3),
]


@pytest.mark.parametrize("side", ["From", "To"])
@pytest.mark.parametrize("case, from_text, to_text, ka, kb", TYPED_PAIRS, ids=[p[0] for p in TYPED_PAIRS])
def test_67_dialog_shows_hash_of_typed_version(qtbot, rev_repo, case, from_text, to_text, ka, kb, side):
    """MELLON-VER-SRS-67: the dialog's result, with no row selected, reports at
    least the first 7 digits of the hash of the commit each typed version
    resolved to. The names typed don't hold the hash. An annotated tag resolves
    to its commit, so the commit's hash is wanted, not the tag object's. The
    dialog first showed its default result, for other commits, so a stale report
    fails too."""
    root, info = rev_repo
    dlg = _typed_result(qtbot, root, from_text, to_text)
    assert _changes(dlg) == _added(info, range(ka + 1, kb + 1)), (
        f"precondition: comparing {from_text!r} with {to_text!r} failed: {dlg.message_label.text()!r}"
    )
    k = ka if side == "From" else kb
    full = info["commits"][k]
    shown = _result_text(dlg)
    assert _shows_hash_of(shown, full), (
        f"{side} {from_text if side == 'From' else to_text!r} resolved to {full}; the result doesn't "
        f"report 7 or more of its first digits. It shows: {shown!r}"
    )


@pytest.mark.parametrize("side, k", [("From", 1), ("To", 5)])
def test_67_dialog_shows_hash_of_default_versions(qtbot, rev_repo, side, k):
    """MELLON-VER-SRS-67 for versions the dialog chose itself: opened by the
    Compare… path with no versions given, From is the current issue (C1) and To
    the latest commit (C5), per -12. The result reports at least the first 7
    digits of each one's hash."""
    root, info = rev_repo
    win = _open_editor(qtbot, root)
    dlg = _open_compare(qtbot, win)
    assert _changes(dlg) == _added(info, range(2, 6)), (
        f"precondition: the default comparison isn't issue 1 to the latest commit: {_r2_rows(dlg)}"
    )
    full = info["commits"][k]
    shown = _result_text(dlg)
    assert _shows_hash_of(shown, full), (
        f"{side} resolved to {full}; the result doesn't report 7 or more of its first digits. It shows: {shown!r}"
    )


def test_67_dialog_shows_hash_of_from_when_to_is_working_copy(qtbot, rev_repo):
    """MELLON-VER-SRS-67: From typed as HEAD~1 (C4), To the working copy. The
    result reports at least the first 7 digits of C4's hash."""
    root, info = rev_repo
    dlg = _typed_result(qtbot, root, "HEAD~1", None)
    # SPEC-5 exists at To (the working copy, as C5) and not at From (C4): Added.
    assert _changes(dlg) == {info["ids"][5]: "Added"}, f"precondition: unexpected rows {_r2_rows(dlg)}"
    shown = _result_text(dlg)
    full = info["commits"][4]
    assert _shows_hash_of(shown, full), (
        f"From resolved to {full}; the result doesn't report 7 or more of its first digits. It shows: {shown!r}"
    )


def test_67_dialog_says_to_was_the_working_copy(qtbot, rev_repo):
    """MELLON-VER-SRS-67: with To the working copy, the result says so. Weak: the
    words "working copy" can come from naming the versions (-41) alone."""
    root, info = rev_repo
    dlg = _typed_result(qtbot, root, "HEAD~1", None)
    shown = _result_text(dlg)
    assert "working copy" in shown.lower(), f"To was the working copy; the result doesn't say so. It shows: {shown!r}"


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-73: pending edits are saved before comparing with the working copy
# ---------------------------------------------------------------------------


def _build_pending_repo(root: Path) -> dict:
    """SPEC-0..SPEC-2 ("Object k.") at "seed" (C1); "revise" (C2 = HEAD) changes
    SPEC-1 only. The working copy is clean."""
    _init(root)
    folder = editor.create_document(root, DOC, title="Pending")
    ids = [editor.create_object(folder, None, f"Object {k}.") for k in range(3)]
    c1 = _r2_commit(root, "seed")
    editor.set_description(folder, ids[1], "Object 1, revised.")
    c2 = _r2_commit(root, "revise")
    return {"ids": ids, "c1": c1, "c2": c2}


def _pending_compare(qtbot, tmp_path, from_ref, to_ref):
    """Open the dialog comparing `from_ref` with `to_ref`, type into SPEC-2's
    description cell without leaving it, then press Compare."""
    root = tmp_path / "repo"
    info = _build_pending_repo(root)
    win = _open_editor(qtbot, root, editable=True)
    dlg = _open_compare(qtbot, win, from_ref(info), to_ref(info))
    target = info["ids"][2]
    view, widget = _type_without_leaving(qtbot, win, target, PENDING)
    assert view.state() == QAbstractItemView.State.EditingState, "precondition: the cell editor closed"
    assert _r2_disk_description(root, target) == "Object 2.", "precondition: the typed text was saved before Compare"
    _press_compare(qtbot, dlg)
    return root, info, dlg, widget


TYPED_LINE = "Object 2." + PENDING


@pytest.mark.parametrize(
    "side, from_ref, to_ref",
    [("To", lambda i: diff.LATEST, lambda i: WORKING_COPY), ("From", lambda i: WORKING_COPY, lambda i: diff.LATEST)],
    ids=["working copy as To", "working copy as From"],
)
def test_73_pending_edit_is_part_of_working_copy_comparison(qtbot, tmp_path, side, from_ref, to_ref):
    """MELLON-VER-SRS-73: the working copy equals the latest commit except for text
    typed in a cell that is still open. Comparing the two must list that object,
    and only it, as Modified, with the working copy on either side."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, from_ref, to_ref)
    expected = {info["ids"][2]: "Modified"}
    assert _changes(dlg) == expected, (
        f"working copy as {side}: the pending edit wasn't compared; expected {expected}, "
        f"the dialog lists {_r2_rows(dlg)}"
    )


def test_73_row_shows_the_typed_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73 with -32: the row of the edited object shows the first line
    of its description at To, the working copy, which is the typed text."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, lambda i: diff.LATEST, lambda i: WORKING_COPY)
    row = _r2_rows(dlg).get(info["ids"][2])
    assert row is not None and row[1] == TYPED_LINE, f"expected the row to show {TYPED_LINE!r}, got {row}"


def test_73_file_holds_the_typed_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73: comparing with the working copy first saves the pending
    edit, so afterwards the object's file holds the typed text."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, lambda i: diff.LATEST, lambda i: WORKING_COPY)
    on_disk = _r2_disk_description(root, info["ids"][2])
    assert on_disk == TYPED_LINE, f"expected the file to hold {TYPED_LINE!r}, it holds {on_disk!r}"


def test_73_two_commit_comparison_leaves_out_pending_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73 doesn't apply when neither version is the working copy.
    Assumed: such a comparison is of the two commits alone (-19 to -21), whether or
    not the pending edit is saved (-56 allows saving it). Seed to the latest commit
    changed SPEC-1 only, so SPEC-1 is the only row."""
    root, info, dlg, _ = _pending_compare(qtbot, tmp_path, lambda i: i["c1"], lambda i: diff.LATEST)
    expected = {info["ids"][1]: "Modified"}
    assert _changes(dlg) == expected, f"expected exactly {expected}, the dialog lists {_r2_rows(dlg)}"


def test_73_two_commit_comparison_keeps_pending_text(qtbot, tmp_path):
    """MELLON-VER-SRS-73, two commits. Assumed: -73 requires no save here, and -56
    allows one, so either the typed text is still in the open cell editor or the
    file holds it. Losing it is neither."""
    root, info, dlg, widget = _pending_compare(qtbot, tmp_path, lambda i: i["c1"], lambda i: diff.LATEST)
    in_editor = _editor_text(widget)
    on_disk = _r2_disk_description(root, info["ids"][2])
    assert in_editor == TYPED_LINE or on_disk == TYPED_LINE, (
        f"the typed text is gone: editor holds {in_editor!r}, the file holds {on_disk!r}"
    )
