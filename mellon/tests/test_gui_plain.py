"""Tests for src/mellon/gui/plain.py, and that the GUI shows repository and git
text as plain text (MELLON-GUI-SRS-122, MELLON-VER-SRS-380).

Text another person commits (attribute values and labels, link types, filter
names, branch names) could hold markup. Qt guesses rich text from a string's
first line, so such markup would hide or fake words, and an <img> tag makes Qt
open the file it names: with a network path, Windows sends the user's
credentials to that host.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pygit2
import pytest
from PySide6.QtCore import QModelIndex, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QTextDocument
from PySide6.QtGui import Qt as QtGuiNamespace
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QMessageBox,
    QWidget,
)

from mellon import set_attribute, set_description
from mellon.gui import plain

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()
SRS = "VCU3-SRS"
OBJ = "VCU3-SRS-1"

needs_fixture = pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(), reason="req-test fixture not available"
)


def _rendered(rich: str) -> str:
    """What a rich-text sink shows for ``rich``, spaces normalised."""
    doc = QTextDocument()
    doc.setHtml(rich)
    return doc.toPlainText().replace("\xa0", " ")


def _png(path: Path, w: int = 400, h: int = 300) -> Path:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    assert img.save(str(path))
    return path


def _img_tag(png: Path) -> str:
    return f'<img src="{QUrl.fromLocalFile(str(png)).toString()}">'


# --- the helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["R&D", "a < b and c > d", "x &lt; y", "<b>bold</b>", '<img src="x.png"> tail', "line\n<b>2</b>"],
)
def test_gui_srs_122_text_shows_as_written_in_a_sink_that_guesses(qapp, value) -> None:
    out = plain.text(value)
    if QtGuiNamespace.mightBeRichText(out):
        assert _rendered(out) == value
    else:
        assert out == value


def test_gui_srs_122_text_leaves_ordinary_text_alone(qapp) -> None:
    for value in ("Delete VCU3-SRS-1?", "R&D", "a < b", "issue-<n>.", ""):
        assert plain.text(value) == value


def test_gui_srs_122_label_and_form_label_are_plain(qapp) -> None:
    lab = plain.label("<b>x</b>")
    assert lab.textFormat() == Qt.TextFormat.PlainText
    assert lab.text() == "<b>x</b>"
    field = QWidget()
    row = plain.form_label("<i>Name</i>:", field)
    assert row.textFormat() == Qt.TextFormat.PlainText
    assert row.buddy() is field


def test_gui_srs_122_esc_escapes_for_rich_labels(qapp) -> None:
    assert plain.esc('<img src="a">&') == "&lt;img src=&quot;a&quot;&gt;&amp;"
    lab = QLabel(f"<b>{plain.esc('<i>x</i>')}</b>")
    lab.setTextFormat(Qt.TextFormat.RichText)
    assert _rendered(lab.text()) == "<i>x</i>"


def test_gui_srs_122_make_plain_covers_a_boxs_informative_text(qapp) -> None:
    box = plain.make_plain(QMessageBox())
    box.setText("<b>a</b>")
    box.setInformativeText("<b>b</b>")
    formats = {lab.objectName(): lab.textFormat() for lab in box.findChildren(QLabel)}
    assert formats["qt_msgbox_label"] == Qt.TextFormat.PlainText
    assert formats["qt_msgbox_informativelabel"] == Qt.TextFormat.PlainText


@pytest.mark.parametrize("kind", ["warning", "critical", "information", "question"])
def test_gui_srs_122_message_boxes_show_text_as_written(qapp, monkeypatch, kind) -> None:
    seen: list = []
    monkeypatch.setattr(
        QMessageBox, kind, staticmethod(lambda *a, **k: seen.append(a) or QMessageBox.StandardButton.Ok)
    )
    getattr(plain, kind)(None, "Title", '<img src="x.png"> failed', QMessageBox.StandardButton.Ok)
    (args,) = seen
    assert args[1] == "Title"
    assert _rendered(args[2]) == '<img src="x.png"> failed'
    assert args[3] == QMessageBox.StandardButton.Ok


def test_gui_srs_122_input_dialog_prompt_shows_as_written(qapp, monkeypatch) -> None:
    from PySide6.QtWidgets import QInputDialog

    seen: list = []
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: seen.append(a) or ("", False)))
    plain.get_text(None, "Add option", "New option for <b>x</b>:")
    assert _rendered(seen[0][2]) == "New option for <b>x</b>:"


# --- the Inspector (CV-GUI-04) -------------------------------------------------


@pytest.fixture
def repo_copy(tmp_path: Path, clean_worktree) -> Path:
    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)
    return dst


@needs_fixture
def test_gui_srs_122_inspector_value_with_image_tag_opens_no_file(qtbot, tmp_path, repo_copy) -> None:
    """An attribute value naming an image shows as written in the read-only
    Inspector, and the image isn't loaded (it would be ~300 px tall)."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    value = _img_tag(_png(tmp_path / "probe.png"))
    set_attribute(repo_copy / SRS, OBJ, "rationale", value)
    w = DocumentEditorWindow(SRS, DocumentService(repo_copy, cache=True))
    qtbot.addWidget(w)
    w.show()
    w.controller.select_object(SRS, OBJ)
    labels = [lab for lab in w.inspector.findChildren(QLabel) if lab.text() == value]
    assert labels, "the Inspector shows no label with the value"
    for lab in labels:
        assert lab.textFormat() == Qt.TextFormat.PlainText
        assert lab.sizeHint().height() < 100, "the label loaded the image the value names"


@needs_fixture
def test_gui_srs_122_inspector_link_row_shows_the_link_type_as_written(qtbot, repo_copy) -> None:
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    links = repo_copy / SRS / "links" / f"{OBJ}.xml"
    text = links.read_text(encoding="utf-8")
    links.write_text(text.replace('type="satisfies"', 'type="&lt;b&gt;MKT&lt;/b&gt;"'), encoding="utf-8")
    w = DocumentEditorWindow(SRS, DocumentService(repo_copy, cache=True))
    qtbot.addWidget(w)
    w.show()
    w.controller.select_object(SRS, OBJ)
    rows = [lab for lab in w.inspector.findChildren(QLabel) if "MKT" in lab.text()]
    assert rows, "the Inspector shows no outgoing link row"
    for lab in rows:
        shown = " ".join(_rendered(lab.text()).split())
        assert shown.startswith("<b>MKT</b> → VCU3-SyRS/VCU3-SyRS-42"), shown


# --- branch names in the status bar (MELLON-VER-SRS-380) ----------------------

# "&lt;" is a name git and every file system accept, and a rich-text sink would
# show it as "<".
BRANCH = "pr&lt;MKB&gt;1"


def _checkout_branch(repo: Path, name: str) -> None:
    r = pygit2.Repository(str(repo))
    r.branches.local.create(name, r.head.peel(pygit2.Commit))
    r.set_head(f"refs/heads/{name}")


@needs_fixture
def test_ver_srs_380_editor_status_bar_shows_the_branch_name_as_written(qtbot, repo_copy) -> None:
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    _checkout_branch(repo_copy, BRANCH)
    w = DocumentEditorWindow(SRS, DocumentService(repo_copy, cache=True))
    qtbot.addWidget(w)
    w._refresh_git_cell()
    assert w._git_cell.text().startswith(f"{BRANCH} @ ")
    assert w._git_cell.textFormat() == Qt.TextFormat.PlainText


@needs_fixture
def test_ver_srs_380_explorer_status_bar_shows_the_branch_name_as_written(qtbot, repo_copy) -> None:
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    _checkout_branch(repo_copy, BRANCH)
    pe = ProjectExplorerWindow()
    qtbot.addWidget(pe)
    assert pe.open_repo(repo_copy)
    assert pe._git_cell.text().startswith(f"{BRANCH} @ ")
    assert pe._git_cell.textFormat() == Qt.TextFormat.PlainText


# --- every window and dialog over a repository full of markup -------------------

# Raw markup that must never reach a sink that renders it.
_RAW = ("<b>MK", "<img", "&lt;MKB")


def _shows_as_written(text: str, fmt: Qt.TextFormat) -> bool:
    if fmt == Qt.TextFormat.PlainText:
        return True
    rich = fmt == Qt.TextFormat.RichText or QtGuiNamespace.mightBeRichText(text)
    return not rich or not any(raw in text for raw in _RAW)


def _model_tooltips(model, parent=QModelIndex(), depth=0):
    if model is None or depth > 6:
        return
    try:
        cols = model.columnCount(parent)
    except TypeError:  # a list model keeps columnCount private
        cols = 1
    for row in range(model.rowCount(parent)):
        for col in range(cols):
            idx = model.index(row, col, parent)
            tip = model.data(idx, Qt.ItemDataRole.ToolTipRole)
            if isinstance(tip, str) and tip:
                yield idx, tip
        if cols > 0:
            yield from _model_tooltips(model, model.index(row, 0, parent), depth + 1)


_MARKERS = ("MKV", "MKL", "MKT", "MKF", "MKB", "mk.png")


def _misread(where: str, seen: set[str]) -> list[str]:
    """Every visible label, tooltip and item tooltip that would render the
    markup instead of showing it. Adds the markers it saw in labels to ``seen``."""
    bad = []
    for top in QApplication.topLevelWidgets():
        if not top.isVisible():
            continue
        for w in [top, *top.findChildren(QWidget)]:
            if isinstance(w, QLabel) and w.isVisible():
                seen.update(m for m in _MARKERS if m in w.text())
                if not _shows_as_written(w.text(), w.textFormat()):
                    bad.append(f"{where}: {type(top).__name__} label {w.text()!r}")
            tip = w.toolTip()
            if tip and not _shows_as_written(tip, Qt.TextFormat.AutoText):
                bad.append(f"{where}: {type(top).__name__} {type(w).__name__} tooltip {tip!r}")
            if isinstance(w, QAbstractItemView) and w.isVisible():
                for _idx, tip in _model_tooltips(w.model()):
                    if not _shows_as_written(tip, Qt.TextFormat.AutoText):
                        bad.append(f"{where}: {type(top).__name__} item tooltip {tip!r}")
    return bad


@needs_fixture
def test_gui_srs_122_no_window_renders_markup_from_the_repository(gui_gui, repo_copy, tmp_path) -> None:
    from mellon.gui.dialogs.commit_dialog import CommitDialog
    from mellon.gui.dialogs.document_properties import DocumentPropertiesDialog
    from mellon.gui.dialogs.object_at_commit import ObjectAtCommitDialog
    from mellon.gui.dialogs.trace_view import TraceView
    from tests.gui_support import _choose_filter, _git_commit_all, _make_filter

    srs = repo_copy / SRS
    img = _img_tag(_png(tmp_path / "mk.png"))
    set_attribute(srs, OBJ, "rationale", f"{img} <b>MKV</b>")
    set_description(srs, OBJ, "<b>MKD</b> description")
    manifest = srs / f"{SRS}.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            'label="Rationale"', 'label="&lt;b&gt;MKL&lt;/b&gt;"'
        ),
        encoding="utf-8",
    )
    links = srs / "links" / f"{OBJ}.xml"
    links.write_text(
        links.read_text(encoding="utf-8").replace('type="satisfies"', 'type="&lt;b&gt;MKT&lt;/b&gt;"'),
        encoding="utf-8",
    )
    _make_filter(repo_copy, "<b>MKF</b>", "MKV")
    _git_commit_all(repo_copy, "markup everywhere")
    _checkout_branch(repo_copy, BRANCH)

    pe = gui_gui.explorer(repo_copy)
    ed = gui_gui.open_doc(pe, SRS)
    ed.controller.select_object(SRS, OBJ)
    gui_gui.app.processEvents()
    seen: set[str] = set()
    problems = _misread("read-only editor", seen)

    _choose_filter(gui_gui, ed, "<b>MKF</b>")
    problems += _misread("filter active", seen)

    gui_gui.enable_editing(ed)
    ed.controller.select_object(SRS, OBJ)
    gui_gui.app.processEvents()
    problems += _misread("editing", seen)

    dialogs = [
        DocumentPropertiesDialog(ed.controller, SRS, ed),
        TraceView(ed.controller, SRS, OBJ, ed),
        ObjectAtCommitDialog(ed.controller.object_at_commit(SRS, OBJ, "HEAD"), ed),
    ]
    set_attribute(srs, OBJ, "status", "Reviewed")
    dialogs.append(CommitDialog(ed.controller.prepare_document_commit(SRS), ed))
    for dlg in dialogs:
        dlg.show()
        gui_gui.app.processEvents()
        problems += _misread(type(dlg).__name__, seen)
        dlg.close()

    assert not problems, "\n".join(problems)
    # The sweep looked at labels showing each kind of text.
    assert seen == set(_MARKERS), sorted(set(_MARKERS) - seen)
