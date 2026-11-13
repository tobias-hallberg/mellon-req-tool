"""Read-only smoke test for the PySide6 GUI (slice 0 + 1).

Runs against the committed `req-test/` fixture using Qt's offscreen platform
so pytest doesn't pop windows during CI. Tests drive the controller's public
API directly + verify views rebind as expected, rather than simulating mouse
clicks — faster and less fragile than event-level tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# pytest-qt respects this env var. Set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()


pytestmark = pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(),
    reason="req-test fixture not available",
)


@pytest.fixture
def window(qtbot, tmp_path, clean_worktree):
    import shutil

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
    return w


# --- shape -----------------------------------------------------------------


def test_main_window_has_three_panes(window) -> None:
    assert window.outline_pane is not None
    assert window.grid_pane is not None
    assert window.inspector is not None


def test_project_tree_lists_three_docs(window) -> None:
    from mellon.gui.models.project_tree import DOC_ID_ROLE, ProjectTreeModel

    model = ProjectTreeModel(window.controller)
    ids = [
        model.data(model.index(row, 0), DOC_ID_ROLE)
        for row in range(model.rowCount())
    ]
    assert set(ids) == {"VCU3-SRS", "VCU3-STS", "VCU3-SyRS"}


def test_default_selection_populates_grid(window) -> None:
    grid_model = window.grid_pane._model
    # load_all returns docs keyed in discovery order; first doc gets auto-selected.
    doc_id = window.controller.current_doc_id()
    expected_rows = len(window.controller.document(doc_id).iter_outline_ids())
    assert grid_model.rowCount() == expected_rows
    assert grid_model.rowCount() > 0


# --- selecting a doc switches outline + grid -------------------------------


def test_selecting_vcu3_srs_shows_27_rows(window) -> None:
    window.controller.select_document("VCU3-SRS")
    assert window.grid_pane._model.rowCount() == 27
    # Outline model should also rebuild.
    outline_model = window.outline_pane._outline_model
    assert outline_model.rowCount() > 0


def test_selecting_vcu3_sts_shows_12_rows(window) -> None:
    window.controller.select_document("VCU3-STS")
    assert window.grid_pane._model.rowCount() == 12


# --- inspector -------------------------------------------------------------


def test_inspector_populates_on_object_select(window) -> None:
    from PySide6.QtWidgets import QLabel

    window.controller.select_object("VCU3-SRS", "VCU3-SRS-1")
    desc = window.inspector._description.toPlainText()
    assert "FAULT_OVERTEMP" in desc
    # Outgoing rows are compound widgets (label + optional Ack button) added
    # via setItemWidget — check the label text, not the QListWidgetItem's.
    out = window.inspector._outgoing
    labels = []
    for i in range(out.count()):
        w = out.itemWidget(out.item(i))
        if w is None:
            labels.append(out.item(i).text())
            continue
        for lbl in w.findChildren(QLabel):
            labels.append(lbl.text())
    assert any("VCU3-SyRS-42" in label for label in labels)
    # Breadcrumb shows the outline ancestry.
    bc = window.inspector._breadcrumb.text()
    assert "VCU3-SRS-1" in bc or "VCU3-SRS-0" in bc


def test_inspector_shows_empty_state_before_any_selection(qtbot) -> None:
    from mellon.gui.controller import RepoController
    from mellon.gui.panels.inspector import Inspector

    # Test the inspector widget directly without a full window — it shows the
    # empty-state label when no object is selected (no service attached).
    controller = RepoController()
    inspector = Inspector(controller)
    qtbot.addWidget(inspector)
    assert not inspector._empty.isHidden()
    assert inspector._description.isHidden()


# --- schema-driven columns ------------------------------------------------


def test_grid_columns_match_document_schema(window) -> None:
    """Per-document attribute schema drives the grid columns.

    Each document's grid columns are exactly the system columns followed by
    the user attributes declared in its manifest, in declaration order.
    No user-attribute column appears unless the manifest declares it — the
    old hard-coded `RequirementType` regression is impossible by construction.
    """
    model = window.grid_pane._model

    for doc_id in ("VCU3-STS", "VCU3-SRS", "VCU3-SyRS"):
        window.controller.select_document(doc_id)
        doc = window.controller.document(doc_id)
        cols = list(model.columns)
        # System columns are fixed and always first.
        assert cols[:4] == ["state", "id", "heading_or_text", "type"]
        # The rest is the manifest's attribute names in declaration order,
        # minus `type` (already a system column).
        expected = [a.name for a in doc.attributes if a.name != "type"]
        assert cols[4:] == expected, (
            f"{doc_id}: expected user cols {expected}, got {cols[4:]}"
        )
        # Any attribute the manifest does not declare must NOT be a column.
        # `RequirementType` was the original hard-coded regression — keep
        # asserting it stays absent unless a manifest re-introduces it.
        if doc.attribute("RequirementType") is None:
            assert model.column_index("RequirementType") == -1


# --- grid filter -----------------------------------------------------------


def test_filter_narrows_grid_by_id(window) -> None:
    window.controller.select_document("VCU3-SRS")
    window.grid_pane.filter_box.setText("SRS-10")
    proxy = window.grid_pane._proxy
    visible = proxy.rowCount()
    assert visible >= 1
    id_col = window.grid_pane._model.column_index("id")
    for row in range(visible):
        id_cell = str(proxy.index(row, id_col).data())
        assert "SRS-10" in id_cell


def test_filter_by_description_text(window) -> None:
    window.controller.select_document("VCU3-SRS")
    window.grid_pane.filter_box.setText("watchdog")
    proxy = window.grid_pane._proxy
    # Several rows should match (watchdog heading + two watchdog reqs).
    assert proxy.rowCount() >= 2


# --- cross-pane sync -------------------------------------------------------


def test_outline_click_updates_inspector(window) -> None:
    # Simulate selecting a different object via the controller (the path all
    # panel clicks go through).
    window.controller.select_object("VCU3-SRS", "VCU3-SRS-10")
    assert window.controller.current_obj_id() == "VCU3-SRS-10"
    assert "MISRA" in window.inspector._description.toPlainText()


def test_inspector_incoming_links_cross_doc(window) -> None:
    # VCU3-SyRS-42 is linked IN to by four SRS objects in the fixture.
    window.controller.select_object("VCU3-SyRS", "VCU3-SyRS-42")
    inc = window.inspector._incoming
    labels = [inc.item(i).text() for i in range(inc.count())]
    # Each incoming entry is "satisfies  ←  VCU3-SRS/VCU3-SRS-<n>".
    srs_hits = [l for l in labels if "VCU3-SRS/" in l]
    assert len(srs_hits) >= 3


# --- opening a bad path ------------------------------------------------------


def test_open_repo_nonexistent_path_fails_cleanly(qtbot, tmp_path) -> None:
    from mellon.gui.controller import RepoController

    controller = RepoController()
    failures: list[tuple[str, str]] = []
    controller.repo_load_failed.connect(lambda root, msg: failures.append((root, msg)))
    # Must not raise (FileNotFoundError used to escape uncaught).
    assert controller.open_repo(tmp_path / "does-not-exist") is False
    assert len(failures) == 1
    assert "cannot read repo root" in failures[0][1]


def _write_doc(root: Path, doc_id: str = "DOC") -> None:
    folder = root / doc_id
    (folder / "objects").mkdir(parents=True)
    (folder / f"{doc_id}.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<document id="{doc_id}">\n'
        "  <metadata><title>D</title></metadata>\n  <outline/>\n</document>\n",
        encoding="utf-8",
    )


def _open_failures(root: Path) -> tuple[bool, list[str]]:
    from mellon.gui.controller import RepoController

    controller = RepoController()
    failures: list[str] = []
    controller.repo_load_failed.connect(lambda _root, msg: failures.append(msg))
    ok = controller.open_repo(root)
    assert controller.service is None or ok
    return ok, failures


def test_open_repo_refuses_folder_outside_git(qtbot, tmp_path) -> None:
    """Without git there is no commit, history or suspect detection: refuse
    and say how to get a clone, instead of opening a half-working repo."""
    import pygit2

    _write_doc(tmp_path)
    ok, failures = _open_failures(tmp_path)
    assert ok is False
    assert len(failures) == 1
    if pygit2.discover_repository(str(tmp_path)) is not None:
        # The temp folder sits inside some git work tree on this machine
        # (e.g. a dotfiles repo in the home folder): refused as a subfolder.
        assert "not the root of its git work tree" in failures[0]
        return
    assert "not inside a git repository" in failures[0]
    assert "git init" in failures[0]
    assert "clone" in failures[0]


def test_open_repo_refuses_subfolder_of_git_clone(qtbot, tmp_path) -> None:
    """The likeliest mistake: opening a document folder inside the clone.
    Point at the clone's root; `git init` there would nest a second repo."""
    import pygit2

    pygit2.init_repository(str(tmp_path))
    _write_doc(tmp_path / "reqs")
    ok, failures = _open_failures(tmp_path / "reqs")
    assert ok is False
    assert "not the root of its git work tree" in failures[0]
    assert f"Open {tmp_path.resolve()} instead." in failures[0]
    assert "git init" not in failures[0]


def test_open_repo_refuses_shallow_clone_with_only_its_remedy(qtbot, tmp_path) -> None:
    """The clone root was opened, so "open that folder instead" (or clone,
    or `git init`) would be wrong advice; the fix is to unshallow."""
    import pygit2

    from tests.test_suspects import _commit_all

    _write_doc(tmp_path)
    repo = pygit2.init_repository(str(tmp_path))
    _commit_all(repo, "seed")
    (Path(repo.path) / "shallow").write_bytes(f"{repo.head.target}\n".encode())
    ok, failures = _open_failures(tmp_path)
    assert ok is False
    assert "git fetch --unshallow" in failures[0]
    assert "instead" not in failures[0]
    assert "git init" not in failures[0]


def test_explorer_explains_refused_repo_in_a_message_box(
    qtbot, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QMessageBox

    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    texts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda _parent, _title, text, *a, **k: texts.append(text) or 0),
    )
    import pygit2

    pygit2.init_repository(str(tmp_path))
    _write_doc(tmp_path / "reqs")
    w = ProjectExplorerWindow()
    qtbot.addWidget(w)

    assert w.open_repo(tmp_path / "reqs") is False
    assert len(texts) == 1 and f"Open {tmp_path.resolve()} instead." in texts[0]
    assert w._project_model.rowCount() == 0


# --- written blind from MELLON-CHECK-SRS ---
# Imports for this block (the helpers below are prefixed _blind_).
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from mellon.loader import load_document
from mellon.validator import validate
from tests.conftest import _ensure_req_test_fixture


_BLIND_REQ_TEST = Path(__file__).resolve().parents[2] / "req-test"


def _blind_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _blind_snapshot(root: Path, *, include_git: bool) -> dict[str, tuple[str, int]]:
    """Each file's sha256 and modified time (ns), keyed by its path relative to `root`."""
    out: dict[str, tuple[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_git and Path(dirpath) == Path(root) and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            p = Path(dirpath, name)
            out[p.relative_to(root).as_posix()] = (
                hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def _blind_assert_unchanged(before, after, what: str) -> None:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), (
        f"{what} changed files: added={added} removed={removed} changed={changed}")


def _blind_req_test_copy(dest: Path, clean_worktree) -> Path:
    _ensure_req_test_fixture()
    shutil.copytree(_BLIND_REQ_TEST, dest, dirs_exist_ok=True)
    clean_worktree(dest)
    return dest


def _blind_dirty_req_test(root: Path) -> None:
    """Uncommitted change in VCU3-SRS: an edited description, an object deleted with its
    outline entry, an untracked file and an ignored file in the document folder."""
    srs = root / "VCU3-SRS"
    obj = srs / "objects" / "VCU3-SRS-1.xml"
    text = obj.read_text(encoding="utf-8")
    new = re.sub(r"<description>(.*?)</description>",
                 r"<description>\1 Edited, not committed.</description>", text, flags=re.S)
    assert new != text
    _blind_write(obj, new)
    (srs / "objects" / "VCU3-SRS-33.xml").unlink()
    manifest = srs / "VCU3-SRS.xml"
    mtext = manifest.read_text(encoding="utf-8")
    assert '<ref id="VCU3-SRS-33"/>' in mtext
    _blind_write(manifest, mtext.replace('      <ref id="VCU3-SRS-33"/>\n', ""))
    _blind_write(srs / "notes.txt", "untracked note\n")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n*.ignored\n")
    _blind_write(srs / "scratch.ignored", "ignored file\n")


def _blind_action(win, text: str):
    from PySide6.QtGui import QAction

    acts = [a for a in win.findChildren(QAction) if a.text().replace("&", "") == text]
    assert len(acts) == 1, f"expected one {text!r} action, found {len(acts)}"
    return acts[0]


def _blind_set_read_only(win, on: bool) -> None:
    act = _blind_action(win, "Read-only")
    if act.isChecked() != on:
        act.trigger()
    assert act.isChecked() == on, f"read-only mode should be {on}"


def _blind_widget_text(w) -> str:
    from PySide6.QtWidgets import (QAbstractItemView, QLabel, QMessageBox, QPlainTextEdit,
                                   QTextEdit)

    if isinstance(w, QMessageBox):
        return "\n".join([w.text(), w.informativeText(), w.detailedText()])
    parts = [c.text() for c in w.findChildren(QLabel)]
    parts += [c.toPlainText() for c in w.findChildren(QTextEdit)]
    parts += [c.toPlainText() for c in w.findChildren(QPlainTextEdit)]
    for view in w.findChildren(QAbstractItemView):
        model = view.model()
        if model is None:
            continue
        for r in range(model.rowCount()):
            for k in range(model.columnCount()):
                data = model.index(r, k).data()
                if data is not None:
                    parts.append(str(data))
    return "\n".join(parts)


def _blind_trigger_validate(qtbot, win, *, expect_window=True) -> list[tuple[str, str]]:
    """Choose the Validate action; return (title, text) of each window it shows. Without
    expect_window, wait for the status bar to change instead (a clean document shows no
    window) and close any window that does show."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog

    act = _blind_action(win, "Validate")
    assert act.isEnabled(), "the Validate action is disabled"
    before = list(QApplication.topLevelWidgets())
    status_before = win.statusBar().currentMessage()
    shown: list[tuple[str, str]] = []
    state = {"tries": 0, "stop": False}

    def grab():
        if state["stop"]:
            return
        found = []
        modal = QApplication.activeModalWidget()
        if modal is not None:
            found.append(modal)
        for w in QApplication.topLevelWidgets():
            if (w.isVisible() and isinstance(w, QDialog) and w not in before
                    and w not in found):
                found.append(w)
        if found:
            for w in found:
                shown.append((w.windowTitle(), _blind_widget_text(w)))
                w.close()
            return
        state["tries"] += 1
        if state["tries"] < 200:
            QTimer.singleShot(25, grab)

    QTimer.singleShot(0, grab)
    act.trigger()
    try:
        if expect_window:
            qtbot.waitUntil(lambda: bool(shown) or state["tries"] >= 200, timeout=15000)
        else:
            qtbot.waitUntil(
                lambda: bool(shown) or win.statusBar().currentMessage() != status_before,
                timeout=15000)
    finally:
        state["stop"] = True
    return shown


_BLIND_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "editing"])
def test_check_srs_79_validate_action_shows_each_validation_error(qtbot, tmp_path,
                                                                  clean_worktree, read_only):
    """MELLON-CHECK-SRS-79: "The Document Editor shall have a Validate action that shows each
    validation error of the open document." Condition: VCU3-SRS with three validation
    errors made by hand (a status that isn't an option, an object file deleted while its
    outline entry stays, an attribute the manifest doesn't define), in read-only mode and
    with read-only mode off."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service import DocumentService

    root = _blind_req_test_copy(tmp_path / "rt", clean_worktree)
    objs = root / "VCU3-SRS" / "objects"
    p10 = objs / "VCU3-SRS-10.xml"
    _blind_write(p10, p10.read_text(encoding="utf-8").replace(
        '<attr name="status">Draft</attr>', '<attr name="status">Bogus</attr>'))
    (objs / "VCU3-SRS-33.xml").unlink()
    p32 = objs / "VCU3-SRS-32.xml"
    _blind_write(p32, p32.read_text(encoding="utf-8").replace(
        "<description>", '<attr name="colour">red</attr>\n  <description>'))

    win = DocumentEditorWindow("VCU3-SRS", DocumentService(root))
    qtbot.addWidget(win)
    win.show()
    _blind_set_read_only(win, read_only)
    shown = _blind_trigger_validate(qtbot, win)
    assert len(shown) == 1, f"expected one result window, got {shown}"
    codes = sorted(_BLIND_CODE.findall(shown[0][1]))
    assert codes == ["ATTR_BAD_ENUM", "ATTR_UNKNOWN", "OUTLINE_MISSING_OBJECT"], shown[0]


@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "editing"])
def test_check_srs_9_validate_action_changes_no_working_copy_file(qtbot, tmp_path,
                                                                  clean_worktree, read_only):
    """MELLON-CHECK-SRS-9: "While Mellon validates a document, Mellon shall not change a file
    in the working copy." Condition: the Validate action of a Document Editor on VCU3-SRS
    with an uncommitted change (edited description, deleted object, untracked and ignored
    files), in read-only mode and with read-only mode off."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service import DocumentService

    root = _blind_req_test_copy(tmp_path / "rt", clean_worktree)
    _blind_dirty_req_test(root)
    before = _blind_snapshot(root, include_git=False)
    win = DocumentEditorWindow("VCU3-SRS", DocumentService(root))
    qtbot.addWidget(win)
    win.show()
    _blind_set_read_only(win, read_only)
    _blind_trigger_validate(qtbot, win, expect_window=False)
    _blind_assert_unchanged(before, _blind_snapshot(root, include_git=False),
                            "the Validate action")


# MELLON-STORE-SRS blind tests
from tests.store_support import (  # noqa: E402
    CONFLICTED_DOC_1,
    git_commit_all,
    git_init,
    make_doc,
    write,
)


def test_store_srs_9_project_explorer_refuses_repo_git_cant_open(gui, unopenable_repo):
    """MELLON-STORE-SRS-9: if git can't open the repository, Mellon shall refuse
    the folder. With core.repositoryformatversion 99, the Project Explorer
    refuses it, lists no document, and says git can't open the repository."""
    pe, ok = gui.explorer(unopenable_repo)
    assert not ok, "open_repo accepted a repository git can't open"
    assert gui.tree_rows(pe) == [], "documents were listed for a refused repository"
    shown = " ".join(t for _, _, t in gui.messages).lower()
    assert shown, "nothing was shown for the refusal"
    assert "git" in shown and "open" in shown, f"message doesn't say git can't open it: {shown!r}"


def test_store_srs_13_project_explorer_opens_root_without_documents(gui, code_only_repo):
    """MELLON-STORE-SRS-13: Mellon shall accept a repository root that holds no
    document. The Project Explorer opens a committed repository holding only a
    src folder and lists no document."""
    pe, ok = gui.explorer(code_only_repo)
    assert ok, f"refused a root without documents: {gui.messages}"
    assert gui.tree_rows(pe) == [], gui.tree_rows(pe)


def test_store_srs_14_project_explorer_accepts_root_without_commit(gui, uncommitted_repo):
    """MELLON-STORE-SRS-14: Mellon shall accept a repository root whose repository
    has no commit. The Project Explorer opens it and lists the document created
    before the first commit."""
    pe, ok = gui.explorer(uncommitted_repo)
    assert ok, f"refused a repository without a commit: {gui.messages}"
    texts = [r["text"] for r in gui.tree_rows(pe)]
    assert any(t.startswith("DOC") for t in texts), texts


def test_store_srs_15_project_explorer_accepts_detached_head(gui, detached_repo):
    """MELLON-STORE-SRS-15: Mellon shall accept a repository root whose HEAD is
    detached. The Project Explorer opens it and shows the document as at the
    checked-out first commit."""
    pe, ok = gui.explorer(detached_repo)
    assert ok, f"refused a detached HEAD: {gui.messages}"
    texts = [r["text"] for r in gui.tree_rows(pe)]
    assert any("First title" in t for t in texts), texts
    assert not any("Second title" in t for t in texts), texts


def test_store_srs_34_warning_icon_only_on_document_with_load_error(gui, tmp_path, git_env):
    """MELLON-STORE-SRS-34: the Project Explorer shall show a warning icon on a
    document with a load error. With conflict markers in one document's object
    file, that entry has an icon, and the other entries show no icon or a
    different one."""
    from PySide6.QtGui import QIcon

    root = git_init(git_env, tmp_path / "icons")
    for d in ("BAD", "GOOD", "OTHER"):
        make_doc(root, d, n=1)
    write(root / "BAD" / "objects" / "BAD-1.xml", CONFLICTED_DOC_1.replace("DOC-1", "BAD-1"))
    git_commit_all(git_env, root, "seed")
    pe, ok = gui.explorer(root)
    assert ok
    rows = {r["text"].split()[0]: r for r in gui.tree_rows(pe)}
    assert set(rows) == {"BAD", "GOOD", "OTHER"}, rows
    bad = rows["BAD"]["icon"]
    assert isinstance(bad, QIcon) and not bad.isNull(), "no icon on the document with a load error"
    bad_img = bad.pixmap(16, 16).toImage()
    for name in ("GOOD", "OTHER"):
        icon = rows[name]["icon"]
        if isinstance(icon, QIcon) and not icon.isNull():
            assert icon.pixmap(16, 16).toImage() != bad_img, f"{name} shows the warning icon"


# MELLON-VER-SRS blind tests
from tests.ver_support import (  # noqa: E402
    SVG_RED,
    _action,
    _activate,
    _commit,
    _git,
    _head,
    _make_repo,
    _obj,
    _open_editor,
    _open_explorer,
    _rev,
    _status_labels,
    _write,
)


# ==========================================================================
# Uncommitted changes
# ==========================================================================


def test_ver_srs_184_side_files_dont_count_as_uncommitted_changes(tmp_path, qtbot):
    """MELLON-VER-SRS-184: Mellon leaves each editor side file out of the
    count of uncommitted changes, in the Project Explorer's and the Document
    Editor's status bars. Control: a real change does change the count."""
    root = _make_repo(tmp_path / "repo")
    pe = _open_explorer(qtbot, root)
    w = _open_editor(qtbot, root)
    clean_pe, clean_w = _status_labels(pe), _status_labels(w)

    def refresh():
        _action(pe, "Refresh").trigger()
        _activate(qtbot, pe)
        _action(w, "Reload from disk").trigger()
        _activate(qtbot, w)

    _write(root / "CHILD/figures/.$CHILD-1.svg.bkp", SVG_RED)
    _write(root / "CHILD/~$CHILD.xml", "lock file")
    refresh()

    assert _status_labels(pe) == clean_pe, "the Project Explorer counts a side file"
    assert _status_labels(w) == clean_w, "the Document Editor counts a side file"
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "A real change."))
    refresh()
    assert _status_labels(pe) != clean_pe and _status_labels(w) != clean_w, "control: a real change isn't counted"


def test_ver_srs_375_project_explorer_status_bar_shows_branch_and_latest_commit(tmp_path, qtbot):
    """MELLON-VER-SRS-375: The Project Explorer shows in the status bar the
    current branch's name and the latest commit's hash, and the new hash after
    a git client commits and the window is activated."""
    root = _make_repo(tmp_path / "repo")
    _git(root, "checkout", "-q", "-b", "feature-x")
    pe = _open_explorer(qtbot, root)
    first = " ".join(_status_labels(pe))
    assert "feature-x" in first and _head(root)[:7] in first, f"status bar: {first}"

    _write(root / "PARENT/objects/PARENT-2.xml", _obj("PARENT-2", "Committed in a git client."))
    new = _commit(root, "git client commit", tick=5)
    _activate(qtbot, pe)

    shown = " ".join(_status_labels(pe))
    assert new[:7] in shown and "feature-x" in shown, f"status bar after the commit: {shown}"


def test_ver_srs_377_document_editor_status_bar_shows_detached_head(tmp_path, qtbot):
    """MELLON-VER-SRS-377: While HEAD is detached, the Document Editor's status
    bar shows that HEAD is detached (MELLON-VER-SRS-376: with the commit's hash)."""
    root = _make_repo(tmp_path / "repo")
    _write(root / "PARENT/objects/PARENT-2.xml", _obj("PARENT-2", "Second commit."))
    _commit(root, "second", tick=2)
    first = _rev(root, "HEAD~1")
    _git(root, "checkout", "-q", first)
    assert _git(root, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0

    w = _open_editor(qtbot, root, writable=False)
    shown = " ".join([w.statusBar().currentMessage(), *_status_labels(w)])

    assert "detached" in shown.lower(), f"the status bar doesn't say HEAD is detached: {shown}"
    assert first[:7] in shown, f"the status bar doesn't show the commit: {shown}"


# MELLON-GUI-SRS blind tests
import pygit2  # noqa: E402
from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QComboBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
)

from tests.gui_support import (  # noqa: E402
    DOC_IDS,
    SRS,
    STS,
    SYRS,
    _RELEASE_NUMBER,
    _choose_filter,
    _copy_fixture,
    _description,
    _external_edit,
    _fake_tectonic,
    _head as gui_head,
    _make_filter,
    _manifest,
    _obj_file,
    _plain,
    _shown,
    _snapshot,
    _snapshot_diff,
    _start_export,
    _write as gui_write,
)


# --------------------------------------------------------------------------- Project Explorer


def test_gui_srs_4_open_repository_asks_for_a_folder(gui_gui, repo):
    """MELLON-GUI-SRS-4: Open Repository… asks the user for a folder (a folder chooser, not a file chooser)
    and opens the folder as the repository."""
    pe = gui_gui.explorer()
    gui_gui.answers["getExistingDirectory"] = str(repo)
    gui_gui.action(pe, "Open Repository").trigger()
    gui_gui.app.processEvents()
    assert [e[0] for e in gui_gui.log if e[1] == "QFileDialog"] == ["getExistingDirectory"]
    assert sorted(gui_gui.listed_docs(pe)) == sorted(DOC_IDS)


def test_gui_srs_4_cancelled_chooser_keeps_open_repository(gui_gui, repo):
    """MELLON-GUI-SRS-4: cancelling the folder chooser keeps the repository that was open."""
    pe = gui_gui.explorer(repo)
    gui_gui.answers["getExistingDirectory"] = ""
    gui_gui.action(pe, "Open Repository").trigger()
    gui_gui.app.processEvents()
    assert gui_gui.opened("getExistingDirectory"), "no folder chooser was shown"
    assert Path(pe.controller.root) == repo
    assert sorted(gui_gui.listed_docs(pe)) == sorted(DOC_IDS)


def _switch_repo_while_modal(gui_gui, pe, other: Path, dialog_name: str, outcome: dict):
    """Handler that, with the dialog really open, opens another repository from the Project Explorer."""

    def handler(d):
        def switch():
            outcome["switched"] = True
            gui_gui.answers["getExistingDirectory"] = str(other)
            gui_gui.action(pe, "Open Repository").trigger()

        def watchdog():
            if _shown(d):
                outcome["still_open"] = True
                d.reject()

        QTimer.singleShot(50, switch)
        QTimer.singleShot(4000, watchdog)
        return gui_gui.real_dialog_exec(d)

    gui_gui.handlers[dialog_name] = handler


@pytest.mark.parametrize("dialog", ["DocumentPropertiesDialog", "CommitDialog"])
def test_gui_srs_6_opening_other_repository_closes_editors_and_their_dialog(gui_gui, repo, tmp_path, dialog):
    """MELLON-GUI-SRS-6: opening another repository closes each Document Editor of the previous one,
    also while the Document Editor shows a modal dialog, which closes with it and changes neither repository."""
    other = _copy_fixture(tmp_path / "other")
    _external_edit(_obj_file(repo, SRS, "VCU3-SRS-2"), "at least 20 Hz", "at least 25 Hz")  # something to commit
    heads = (gui_head(repo), gui_head(other))
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=True)
    outcome = {}
    _switch_repo_while_modal(gui_gui, pe, other, dialog, outcome)
    # Unexpected errors raised after the window closed are reported to the lead, not judged here.
    with gui_gui.bot.captureExceptions() as errors:
        gui_gui.action(ed, "Properties" if dialog == "DocumentPropertiesDialog" else "Commit").trigger()
        gui_gui.bot.wait(200)
    outcome["errors"] = [str(e[1]) for e in errors]
    assert gui_gui.opened(dialog), f"the {dialog} didn't open; dialogs: {[e[:2] for e in gui_gui.log]}"
    assert outcome.get("switched"), "the repository switch never ran"
    assert Path(pe.controller.root) == other
    assert not _shown(ed), "the Document Editor of the previous repository stayed open"
    assert gui_gui.open_list(pe) == []
    assert not outcome.get("still_open"), "the modal dialog stayed open after its Document Editor closed"
    assert (gui_head(repo), gui_head(other)) == heads


@pytest.mark.parametrize("how", ["double_click", "enter"])
def test_gui_srs_36_opening_document_opens_editor(gui_gui, repo, how):
    """MELLON-GUI-SRS-36: opening a document that loads opens a Document Editor for it, listed as open."""
    pe = gui_gui.explorer(repo)
    tree = gui_gui.doc_tree(pe)
    idx = gui_gui.doc_index(pe, STS)
    if how == "double_click":
        gui_gui.dclick(tree, idx)
    else:
        tree.setFocus()
        tree.setCurrentIndex(idx)
        gui_gui.key(pe, Qt.Key.Key_Return)
    gui_gui.bot.waitUntil(lambda: len(gui_gui.editors(STS)) == 1, timeout=5000)
    assert [t.split(" ")[0] for t in gui_gui.open_list(pe)] == [STS]


def test_gui_srs_37_opening_open_document_activates_its_editor(gui_gui, repo):
    """MELLON-GUI-SRS-37: opening a document that already has a Document Editor activates that one
    instead of opening a second."""
    pe = gui_gui.explorer(repo)
    first = gui_gui.open_doc(pe, SRS)
    gui_gui.activate(pe)
    gui_gui.dclick(gui_gui.doc_tree(pe), gui_gui.doc_index(pe, SRS))
    gui_gui.bot.wait(200)
    assert gui_gui.editors(SRS) == [first], "a second Document Editor opened"
    assert [t.split(" ")[0] for t in gui_gui.open_list(pe)] == [SRS]
    gui_gui.bot.waitUntil(lambda: first.isActiveWindow(), timeout=3000)


def test_gui_srs_38_explorer_lists_open_editors(gui_gui, repo):
    """MELLON-GUI-SRS-38: the Project Explorer lists the open Document Editors: DOC and PARENT, then PARENT only."""
    pe = gui_gui.explorer(repo)
    doc = gui_gui.open_doc(pe, SRS)
    gui_gui.open_doc(pe, SYRS)
    assert sorted(t.split(" ")[0] for t in gui_gui.open_list(pe)) == sorted([SRS, SYRS])
    doc.close()
    gui_gui.app.processEvents()
    assert [t.split(" ")[0] for t in gui_gui.open_list(pe)] == [SYRS]


# --------------------------------------------------------------------------- status bar


def _hex_tokens(text: str) -> list[str]:
    return re.findall(r"(?<![0-9a-fA-F])[0-9a-f]{4,40}(?![0-9a-fA-F])", text)


@pytest.mark.parametrize("window", ["explorer", "editor"])
def test_gui_srs_115_status_bar_shows_branch_and_short_hash(gui_gui, repo, window):
    """MELLON-GUI-SRS-115: the status bar shows the current branch and an abbreviated hash of the latest commit."""
    pe = gui_gui.explorer(repo)
    win = pe if window == "explorer" else gui_gui.open_doc(pe, SRS)
    text = gui_gui.status_texts(win)
    branch = pygit2.Repository(str(repo)).head.shorthand
    assert branch in text, text
    full = gui_head(repo)
    prefixes = [t for t in _hex_tokens(text) if full.startswith(t) and len(t) < len(full)]
    assert prefixes, f"no abbreviated hash of {full} in {text!r}"


def _uncommitted_counts(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"(\d+)\s*(?:dirty|document file|uncommitted)", text)]


@pytest.mark.parametrize("window", ["explorer", "editor"])
def test_gui_srs_117_status_bar_counts_uncommitted_changes(gui_gui, repo, window):
    """MELLON-GUI-SRS-117: the status bar counts uncommitted changes in the document folders: added, changed
    and deleted files, leaving out side files and files git ignores (here exactly 3)."""
    folder = repo / SRS
    gui_write(folder / "objects" / "VCU3-SRS-99.xml",
           '<?xml version="1.0" encoding="UTF-8"?>\n<object id="VCU3-SRS-99">\n'
           '  <attr name="type">Requirement</attr>\n  <attr name="status">Draft</attr>\n'
           '  <description>Added.</description>\n</object>\n')  # added
    (folder / "links" / "VCU3-SRS-30.xml").unlink()  # deleted
    _external_edit(_obj_file(repo, SRS, "VCU3-SRS-2"), "at least 20 Hz", "at least 25 Hz")  # changed
    gui_write(folder / "~$x.xml", "side")
    gui_write(folder / "figures" / ".$y.svg.bkp", "side")
    with open(repo / ".git" / "info" / "exclude", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n*.scratch\n")
    gui_write(folder / "notes.scratch", "ignored")
    pe = gui_gui.explorer(repo)
    win = pe if window == "explorer" else gui_gui.open_doc(pe, SRS)
    counts = _uncommitted_counts(gui_gui.status_texts(win))
    assert counts and set(counts) == {3}, gui_gui.status_texts(win)


def test_gui_srs_118_editor_status_bar_says_no_branch_on_detached_head(gui_gui, repo):
    """MELLON-GUI-SRS-118: while no branch is checked out, the Document Editor's status bar says so."""
    r = pygit2.Repository(str(repo))
    r.set_head(r.head.target)  # detached HEAD at the latest commit
    assert r.head_is_detached
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    text = gui_gui.status_texts(ed).lower()
    assert "detached" in text or "no branch" in text, text


def test_gui_srs_118_editor_status_bar_on_branch_says_nothing_of_detached(gui_gui, repo):
    """MELLON-GUI-SRS-118 (control): on a branch, the Document Editor's status bar doesn't say no branch is checked out."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    text = gui_gui.status_texts(ed).lower()
    assert "detached" not in text and "no branch" not in text, text


# --------------------------------------------------------------------------- About dialog


@pytest.mark.parametrize("window", ["explorer", "editor"])
def test_gui_srs_140_about_action_opens_about_dialog(gui_gui, repo, window):
    """MELLON-GUI-SRS-140: the Project Explorer and the Document Editor each have an About action that
    opens the About dialog."""
    pe = gui_gui.explorer(repo)
    win = pe if window == "explorer" else gui_gui.open_doc(pe, SRS)
    gui_gui.action(win, "About Mellon").trigger()
    assert [e[1] for e in gui_gui.opened("AboutDialog")] == ["AboutDialog"]


def test_gui_srs_141_about_dialog_shows_release_number(gui_gui, gui_version_run):
    """MELLON-GUI-SRS-141: the About dialog shows the release number, the one --version prints."""
    from mellon.gui.dialogs.about_dialog import AboutDialog

    printed = _RELEASE_NUMBER.findall(gui_version_run[0].stdout.decode("utf-8"))
    assert len(printed) == 1
    d = AboutDialog()
    text = "\n".join(lab.text() for lab in d.findChildren(QLabel)) + d.windowTitle()
    assert _RELEASE_NUMBER.findall(re.sub(r"<[^>]+>", " ", text)) == printed, text


# --------------------------------------------------------------------------- Project Explorer shortcuts


def test_gui_srs_145_ctrl_o_runs_open_repository(gui_gui, repo):
    """MELLON-GUI-SRS-145: Ctrl+O in the Project Explorer runs Open Repository… (its folder chooser opens)."""
    pe = gui_gui.explorer(repo)
    gui_gui.key(pe, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    assert gui_gui.opened("getExistingDirectory")


def test_gui_srs_146_ctrl_n_runs_new_document(gui_gui, repo):
    """MELLON-GUI-SRS-146: Ctrl+N in the Project Explorer, with a repository open, runs New document."""
    pe = gui_gui.explorer(repo)
    gui_gui.key(pe, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    assert gui_gui.opened("NewDocumentDialog")


def _failed_mark(gui_gui, pe, doc_id) -> str:
    m = gui_gui.doc_tree(pe).model()
    idx = gui_gui.doc_index(pe, doc_id)
    return f"{m.data(idx)}\n{m.data(idx, Qt.ItemDataRole.ToolTipRole) or ''}"


def test_gui_srs_147_f5_runs_refresh(gui_gui, repo):
    """MELLON-GUI-SRS-147: F5 in the Project Explorer runs Refresh: a document broken on disk is then listed
    as failed to load."""
    pe = gui_gui.explorer(repo)
    assert "VCU3-STS.xml" not in _failed_mark(gui_gui, pe, STS)
    gui_write(_manifest(repo, STS), "<document id='VCU3-STS'><broken")
    gui_gui.key(pe, Qt.Key.Key_F5)
    mark = _failed_mark(gui_gui, pe, STS)
    assert "VCU3-STS.xml" in mark and "fail" in mark.lower(), mark


_SRS2_ATTRS = {"Type": "Requirement", "Requirement Type": "Performance", "Priority": "Must", "Status": "Draft",
               "Verification Method": "Test"}


def _field_value(w) -> str:
    if isinstance(w, QComboBox):
        return w.currentText()
    if isinstance(w, (QTextEdit, QPlainTextEdit)):
        return w.toPlainText()
    return w.text()


@pytest.mark.parametrize("mode", ["read_only", "editing"])
def test_gui_srs_55_inspector_shows_attributes_description_and_links(gui_gui, repo, mode):
    """MELLON-GUI-SRS-55: the Inspector shows the selected object's attributes (by label, with their values),
    description and outgoing links; an attribute that isn't set shows no value."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS, editing=(mode == "editing"))
    gui_gui.select(ed, "VCU3-SRS-2")
    for label, value in _SRS2_ATTRS.items():
        assert _field_value(gui_gui.insp_field(ed, label)) == value, label
    try:
        rationale = gui_gui.insp_field(ed, "Rationale")
    except AssertionError:
        rationale = None  # not shown at all: no value either
    assert rationale is None or _field_value(rationale) == ""
    texts = "\n".join(gui_gui.insp_texts(ed))
    assert _description(repo, SRS, "VCU3-SRS-2") in texts
    assert "VCU3-SyRS-42" in texts and "satisfies" in texts


def _visible_texts(w) -> str:
    out = []
    for lab in w.findChildren(QLabel):
        if lab.isVisible():
            out.append(lab.text())
    return "\n".join(out)


def _visible_buttons(w) -> list[str]:
    return [_plain(b.text()) for b in w.findChildren(QPushButton) if b.isVisible()]


def test_gui_srs_68_no_objects_state(gui_gui, repo):
    """MELLON-GUI-SRS-68: a document with no objects says it has no objects and offers to add the first object."""
    from mellon.service.document import DocumentService

    DocumentService(repo).create_document("EMPTY", title="Empty")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, "EMPTY")
    shown = _visible_texts(ed.grid_pane).lower()
    assert "no objects" in shown, shown
    assert "Add first object" in _visible_buttons(ed.grid_pane)


def test_gui_srs_69_no_match_state_clear_filters(gui_gui, repo):
    """MELLON-GUI-SRS-69: when the filter, the filter box and Suspects only hide each object, the grid says no
    object matches and Clear filters clears the three, so each row shows."""
    _make_filter(repo, "Nothing", "no-object-has-this-rationale")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    gp = ed.grid_pane
    every = gui_gui.grid_ids(ed)
    _choose_filter(gui_gui, ed, "Nothing")
    QTest.keyClicks(gp.filter_box, "zzz")
    sus = [b for b in gp.banner.findChildren(QPushButton) if _plain(b.text()) == "Suspects only"][0]
    gui_gui.bot.mouseClick(sus, Qt.MouseButton.LeftButton)
    gui_gui.app.processEvents()
    assert gui_gui.grid_ids(ed) == []
    assert "no requirements match" in _visible_texts(gp).lower() or "no object" in _visible_texts(gp).lower()
    clear = [b for b in gp.findChildren(QPushButton) if _plain(b.text()) == "Clear filters" and b.isVisible()]
    assert clear, "no Clear filters button shown"
    gui_gui.bot.mouseClick(clear[0], Qt.MouseButton.LeftButton)
    gui_gui.app.processEvents()
    assert gp.active_filter() is None
    assert gp.filter_box.text() == ""
    assert not gp.is_suspect_only()
    assert gui_gui.grid_ids(ed) == every


def test_gui_srs_69_empty_document_with_filter_shows_no_objects_state(gui_gui, repo):
    """MELLON-GUI-SRS-69 (control): an empty document with the filter box in use shows the no-objects state,
    not the no-match state."""
    from mellon.service.document import DocumentService

    DocumentService(repo).create_document("EMPTY", title="Empty")
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, "EMPTY")
    QTest.keyClicks(ed.grid_pane.filter_box, "zzz")
    gui_gui.app.processEvents()
    shown = _visible_texts(ed.grid_pane).lower()
    assert "no objects" in shown and "match" not in shown, shown


def test_gui_srs_6_opening_other_repository_while_export_runs(gui_gui, repo, tmp_path, monkeypatch):
    """MELLON-GUI-SRS-6: opening another repository while an export from a Document Editor of the first one runs
    closes that Document Editor, and the export writes nothing into the new repository."""
    from PySide6.QtCore import QThreadPool

    other = _copy_fixture(tmp_path / "other")
    monkeypatch.setenv("MELLON_TECTONIC", str(_fake_tectonic(tmp_path / "tools", 3)))
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    _start_export(gui_gui, ed, repo / "exported.pdf")
    gui_gui.bot.waitUntil(lambda: ed.is_exporting(), timeout=5000)  # precondition: the export runs
    before = _snapshot(other)
    gui_gui.answers["getExistingDirectory"] = str(other)
    gui_gui.action(pe, "Open Repository").trigger()
    gui_gui.app.processEvents()
    assert Path(pe.controller.root) == other, "precondition: the other repository is open"
    assert not _shown(ed), "the Document Editor of the previous repository stayed open"
    assert gui_gui.open_list(pe) == []
    QThreadPool.globalInstance().waitForDone(20000)  # the export runs to its end
    gui_gui.bot.wait(200)
    assert _snapshot_diff(before, _snapshot(other)) == [], "the export wrote into the new repository"
