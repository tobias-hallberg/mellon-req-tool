"""External-change staleness detection + reload (GUI).

Builds a real two-doc pygit2 repo (child DOC declares parent PARENT) and
edits it through a *second* `DocumentService` to simulate another process —
the MCP server, a script, a sibling window. The editor window must notice on
activation, offer a reload, and show fresh content afterwards.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pygit2
import pytest


# pytest-qt respects this env var. Set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_suspects import (  # noqa: E402
    DOC_MANIFEST,
    DOC_OBJ,
    PARENT_MANIFEST,
    PARENT_OBJ_V1,
    _commit_all,
    _link_xml,
)
from mellon.loader import load_document  # noqa: E402
from tests.store_support import (  # noqa: E402 -- MELLON-STORE-SRS blind tests
    CONFLICT_HINT,
    CONFLICT_LINE,
    _break_doc_1,
    _shows_load_error,
    _svg_file,
    changed,
    git_commit_all,
    git_env,  # noqa: F401, F811 -- fixture
    git_init,
    gui,  # noqa: F401, F811 -- fixture
    make_doc,
    read_only_repo,  # noqa: F401, F811 -- fixture
    snapshot,
    write,
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Two-doc git repo: DOC-1 satisfies PARENT-42, link acked."""
    parent = tmp_path / "PARENT"
    (parent / "objects").mkdir(parents=True)
    (parent / "PARENT.xml").write_text(PARENT_MANIFEST, encoding="utf-8")
    (parent / "objects" / "PARENT-42.xml").write_text(PARENT_OBJ_V1, encoding="utf-8")

    doc = tmp_path / "DOC"
    (doc / "objects").mkdir(parents=True)
    (doc / "links").mkdir()
    (doc / "DOC.xml").write_text(DOC_MANIFEST, encoding="utf-8")
    (doc / "objects" / "DOC-1.xml").write_text(DOC_OBJ, encoding="utf-8")

    repo = pygit2.init_repository(str(tmp_path))
    (doc / "links" / "DOC-1.xml").write_text(_link_xml("0000000"), encoding="utf-8")
    c1 = _commit_all(repo, "init")
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    _commit_all(repo, "ack")
    return tmp_path


@pytest.fixture
def window(qtbot, repo_root: Path):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    svc = DocumentService(repo_root, cache=True)
    w = DocumentEditorWindow("DOC", svc)
    w._set_read_only(False)
    qtbot.addWidget(w)
    return w


def _external_service(repo_root: Path):
    """A second, cache-less service — stands in for the MCP server process."""
    from mellon.service.document import DocumentService

    return DocumentService(repo_root, cache=False)


def _activate(window) -> None:
    from PySide6.QtCore import QEvent

    window.event(QEvent(QEvent.Type.WindowActivate))


# --- staleness detection -----------------------------------------------------


def test_fresh_window_is_not_stale(window) -> None:
    _activate(window)
    assert window.grid_pane.stale_banner.isHidden()


def test_external_edit_flags_stale_on_activate(window, repo_root) -> None:
    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(window)
    assert not window.grid_pane.stale_banner.isHidden()


def test_parent_doc_edit_flags_child_stale(window, repo_root) -> None:
    _external_service(repo_root).set_description(
        "PARENT", "PARENT-42", "revised parent text"
    )
    _activate(window)
    assert not window.grid_pane.stale_banner.isHidden()


def test_own_edit_does_not_flag_stale(window, repo_root) -> None:
    # Every GUI mutation handler follows its service write with
    # reload_document — mimic that pair here.
    window.controller.require_service().set_description("DOC", "DOC-1", "own edit")
    window.controller.reload_document("DOC")
    _activate(window)
    assert window.grid_pane.stale_banner.isHidden()


# --- figures saved in place (draw.io round trip) -----------------------------

_FIG_V1 = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"/>'
_FIG_V2 = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="20">'
    '<rect width="80" height="20"/></svg>'
)


@pytest.fixture
def figured_window(window, repo_root, tmp_path):
    """`window` with DOC-1 carrying a figure attached through the window."""
    src = tmp_path / "v1.svg"
    src.write_text(_FIG_V1, encoding="utf-8")
    window.controller.require_service().set_figure("DOC", "DOC-1", src)
    window.controller.reload_document("DOC")
    return window


def _figure(repo_root: Path) -> Path:
    return repo_root / "DOC" / "figures" / "DOC-1.svg"


def test_figure_saved_in_place_does_not_flag_stale(figured_window, repo_root) -> None:
    _figure(repo_root).write_text(_FIG_V2, encoding="utf-8")  # draw.io's save
    _activate(figured_window)
    assert figured_window.grid_pane.stale_banner.isHidden()
    model = figured_window.grid_pane._model
    assert "DOC-1" in model._dirty_ids
    # The snapshot moved past the save: nothing left to report.
    assert figured_window.controller.changed_figures() == frozenset()


def test_drawio_backup_file_does_not_flag_stale(figured_window, repo_root) -> None:
    (_figure(repo_root).parent / ".$DOC-1.svg.bkp").write_text(_FIG_V1)
    _activate(figured_window)
    assert figured_window.grid_pane.stale_banner.isHidden()


def test_figure_plus_object_change_flags_stale(figured_window, repo_root) -> None:
    _figure(repo_root).write_text(_FIG_V2, encoding="utf-8")
    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(figured_window)
    assert not figured_window.grid_pane.stale_banner.isHidden()


def test_figure_added_outside_flags_stale(window, repo_root) -> None:
    fig = _figure(repo_root)
    fig.parent.mkdir()
    fig.write_text(_FIG_V1, encoding="utf-8")
    _activate(window)
    assert not window.grid_pane.stale_banner.isHidden()


def test_parent_figure_change_flags_stale(window, repo_root) -> None:
    fig = repo_root / "PARENT" / "figures" / "PARENT-42.svg"
    fig.parent.mkdir()
    fig.write_text(_FIG_V1, encoding="utf-8")
    _activate(window)
    assert not window.grid_pane.stale_banner.isHidden()


def test_figure_save_keeps_existing_banner(figured_window, repo_root) -> None:
    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(figured_window)
    _figure(repo_root).write_text(_FIG_V2, encoding="utf-8")
    _activate(figured_window)
    assert not figured_window.grid_pane.stale_banner.isHidden()


# --- reload ------------------------------------------------------------------


def test_reload_clears_banner_and_shows_fresh_content(window, repo_root) -> None:
    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(window)
    assert not window.grid_pane.stale_banner.isHidden()

    window._reload_from_disk()

    assert window.grid_pane.stale_banner.isHidden()
    assert window.controller.current_obj_id() == "DOC-1"  # selection preserved
    model = window.grid_pane._model
    col = model.column_index("heading_or_text")
    assert model.index(0, col).data() == "edited outside"


def test_reload_picks_up_external_schema_change(window, repo_root) -> None:
    _external_service(repo_root).add_attribute("DOC", "priority", "text", False)
    window._reload_from_disk()
    assert "priority" in window.grid_pane._model.columns


def test_reload_via_banner_button(window, repo_root) -> None:
    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(window)
    window.grid_pane.stale_banner.reload_clicked.emit()
    assert window.grid_pane.stale_banner.isHidden()


def test_reload_action_has_f5_shortcut(window) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    assert window._reload_act.shortcut() == QKeySequence(Qt.Key.Key_F5)


def test_stale_banner_refuses_edits_until_reload(window, repo_root) -> None:
    from PySide6.QtCore import Qt

    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(window)
    obj_file = repo_root / "DOC" / "objects" / "DOC-1.xml"
    before = obj_file.read_bytes()
    model = window.grid_pane._model
    desc = model.index(0, model.column_index("heading_or_text"))

    # Grid cell edit.
    assert model.setData(desc, "cached view wins?", Qt.ItemDataRole.EditRole) is False
    assert "Reload first (F5)" in window._status.currentMessage()
    # Inspector edit: refused, the typed text stays in the field.
    window.inspector._description.setPlainText("inspector text")
    window.inspector._commit_description()
    assert window.inspector._description.toPlainText() == "inspector text"
    # Outline mutation.
    window._status.clearMessage()
    window._act_new_sibling.trigger()
    assert "Reload first (F5)" in window._status.currentMessage()

    assert obj_file.read_bytes() == before
    assert sorted(p.name for p in (repo_root / "DOC" / "objects").iterdir()) == [
        "DOC-1.xml"
    ]

    window._reload_from_disk()
    assert model.setData(desc, "after reload", Qt.ItemDataRole.EditRole) is True
    assert "after reload" in obj_file.read_text(encoding="utf-8")


def test_stale_grid_opens_no_cell_editor(window, repo_root) -> None:
    """While stale, a cell editor's text could only be refused and thrown
    away on leaving the cell, so none opens (as in read-only mode)."""
    from PySide6.QtCore import Qt

    _external_service(repo_root).set_description("DOC", "DOC-1", "edited outside")
    _activate(window)
    model = window.grid_pane._model
    table = window.grid_pane.table
    desc = model.index(0, model.column_index("heading_or_text"))
    idx = window.grid_pane._proxy.mapFromSource(desc)

    assert not model.flags(desc) & Qt.ItemFlag.ItemIsEditable
    table.setCurrentIndex(idx)
    table.edit(idx)
    assert table.indexWidget(idx) is None

    window._reload_from_disk()
    assert model.flags(desc) & Qt.ItemFlag.ItemIsEditable


def test_reload_after_doc_deleted_keeps_banner(window, repo_root) -> None:
    shutil.rmtree(repo_root / "DOC")
    window._reload_from_disk()
    assert not window.grid_pane.stale_banner.isHidden()
    assert "DOC" not in window.controller.docs()


# --- documents that fail to load ---------------------------------------------


# What a pull with a conflicting edit leaves behind.
CONFLICTED_DOC_OBJ = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<<<<<<< HEAD\n"
    '<object id="DOC-1"><attr name="status">Draft</attr>'
    "<description>ours</description></object>\n"
    "=======\n"
    '<object id="DOC-1"><attr name="status">Draft</attr>'
    "<description>theirs</description></object>\n"
    ">>>>>>> feature\n"
)


def _break_doc(repo_root: Path) -> Path:
    obj = repo_root / "DOC" / "objects" / "DOC-1.xml"
    obj.write_text(CONFLICTED_DOC_OBJ, encoding="utf-8")
    return obj


def _capture_boxes(monkeypatch, kind: str = "warning") -> list[str]:
    from PySide6.QtWidgets import QMessageBox

    texts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        kind,
        staticmethod(lambda _parent, _title, text, *a, **k: texts.append(text) or 0),
    )
    return texts


def test_reload_of_broken_doc_shows_the_load_error(
    window, repo_root, monkeypatch
) -> None:
    """F5 on a document a pull left with conflict markers must say where it
    broke, not that the document no longer exists."""
    import re

    texts = _capture_boxes(monkeypatch)
    _break_doc(repo_root)

    window._reload_from_disk()

    assert len(texts) == 1
    assert re.search(r"DOC-1\.xml:\d+\n", texts[0])
    assert "(unresolved merge conflict?)" in texts[0]
    assert "no longer exists" not in window._status.currentMessage()
    assert not window.grid_pane.stale_banner.isHidden()


def test_grid_survives_its_document_breaking_on_disk(window, repo_root) -> None:
    """Painting a grid whose document no longer loads must not raise; the
    last good copy stays on screen and no cell offers an editor."""
    from PySide6.QtCore import Qt

    from mellon.gui.models.objects_grid import (
        OBJ_FIGURE_PATH_ROLE,
        OBJ_LINK_STATES_ROLE,
        OBJ_TABLE_ROLE,
    )

    _break_doc(repo_root)
    window.controller.require_service().cache.invalidate(["DOC"])
    model = window.grid_pane._model

    roles = (
        Qt.ItemDataRole.DisplayRole,
        Qt.ItemDataRole.EditRole,
        Qt.ItemDataRole.ToolTipRole,
        OBJ_FIGURE_PATH_ROLE,
        OBJ_TABLE_ROLE,
        OBJ_LINK_STATES_ROLE,
    )
    for col in range(model.columnCount()):
        idx = model.index(0, col)
        assert not model.flags(idx) & Qt.ItemFlag.ItemIsEditable
        for role in roles:
            model.data(idx, role)
    status = model.index(0, model.column_index("status"))
    assert model.data(status) == "Draft"
    window.grab()  # paints every visible cell


def test_read_only_toggles_cleanly_on_a_doc_that_no_longer_loads(
    window, repo_root, monkeypatch
) -> None:
    """After a failed F5 the window keeps the last good copy; toggling
    read-only must still complete (menu checkbox, signal, actions) instead
    of raising from the Inspector or the outline actions."""
    _capture_boxes(monkeypatch)
    window.controller.select_object("DOC", "DOC-1")
    _break_doc(repo_root)
    window._reload_from_disk()
    seen: list[bool] = []
    window.read_only_changed.connect(seen.append)

    window._set_read_only(True)
    window._set_read_only(False)

    assert seen == [True, False]
    assert not window._read_only_act.isChecked()
    assert not window._act_indent.isEnabled()
    assert window.inspector._id_label.text() == "DOC-1"  # last good copy


def test_explorer_lists_doc_that_fails_to_load(qtbot, repo_root) -> None:
    import re

    from PySide6.QtCore import Qt

    from mellon.gui.models.project_tree import DOC_ID_ROLE, LOAD_ERROR_ROLE

    _break_doc(repo_root)
    explorer = _explorer(qtbot, repo_root)
    model = explorer._project_model

    rows = {
        model.data(model.index(r, 0), DOC_ID_ROLE): model.index(r, 0)
        for r in range(model.rowCount())
    }
    assert set(rows) == {"DOC", "PARENT"}
    assert model.data(rows["PARENT"], LOAD_ERROR_ROLE) is None
    broken = rows["DOC"]
    assert model.data(broken, LOAD_ERROR_ROLE) is not None
    assert "failed to load" in broken.data()
    tooltip = broken.data(Qt.ItemDataRole.ToolTipRole)
    assert re.search(r"DOC-1\.xml:\d+\n", tooltip)
    assert "(unresolved merge conflict?)" in tooltip
    assert "1 failed to load" in explorer._status.currentMessage()


def test_activating_failed_doc_shows_error_instead_of_editor(
    qtbot, repo_root, monkeypatch
) -> None:
    from mellon.gui.models.project_tree import DOC_ID_ROLE

    _break_doc(repo_root)
    explorer = _explorer(qtbot, repo_root)
    texts = _capture_boxes(monkeypatch)
    model = explorer._project_model
    row = next(
        r for r in range(model.rowCount())
        if model.data(model.index(r, 0), DOC_ID_ROLE) == "DOC"
    )

    explorer._on_doc_activated(model.index(row, 0))

    assert explorer._editor_windows == {}
    assert len(texts) == 1
    assert "DOC-1.xml" in texts[0]
    assert "(unresolved merge conflict?)" in texts[0]


def test_explorer_f5_relists_documents_from_disk(qtbot, repo_root) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    from mellon.gui.models.project_tree import DOC_ID_ROLE, LOAD_ERROR_ROLE

    explorer = _explorer(qtbot, repo_root)
    assert explorer._refresh_act.shortcut() == QKeySequence(Qt.Key.Key_F5)

    def doc_failed() -> bool:
        model = explorer._project_model
        for r in range(model.rowCount()):
            idx = model.index(r, 0)
            if model.data(idx, DOC_ID_ROLE) == "DOC":
                return model.data(idx, LOAD_ERROR_ROLE) is not None
        raise AssertionError("DOC not listed")

    obj = repo_root / "DOC" / "objects" / "DOC-1.xml"
    good = obj.read_bytes()
    _break_doc(repo_root)
    assert not doc_failed()  # still the cached listing
    explorer._refresh_act.trigger()
    assert doc_failed()

    obj.write_bytes(good)  # conflict resolved
    explorer._refresh_act.trigger()
    assert not doc_failed()


# --- suspect gutter freshness --------------------------------------------------


def _gutter(window, obj_id: str) -> tuple[str, str]:
    from mellon.gui.models.objects_grid import OBJ_LINK_STATES_ROLE

    model = window.grid_pane._model
    idx = model.index(model.row_for_obj_id(obj_id), model.column_index("state"))
    return idx.data(OBJ_LINK_STATES_ROLE)


def test_committing_a_parent_revision_flips_open_gutters_to_suspect(
    qtbot, repo_root
) -> None:
    """Revising a parent and committing must turn its links SUSPECT in the
    committing window at once and in an open child window when it is next
    activated, without F5."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    svc = DocumentService(repo_root, cache=True)  # shared, as the explorer does
    parent_win = DocumentEditorWindow("PARENT", svc)
    child_win = DocumentEditorWindow("DOC", svc)
    qtbot.addWidget(parent_win)
    qtbot.addWidget(child_win)
    assert _gutter(parent_win, "PARENT-42") == ("none", "ok")
    assert _gutter(child_win, "DOC-1") == ("ok", "none")

    svc.set_description("PARENT", "PARENT-42", "revised parent text")
    parent_win.controller.reload_document("PARENT")
    assert _gutter(parent_win, "PARENT-42") == ("none", "ok")  # not committed yet
    parent_win.controller.commit_document("PARENT", "Revise PARENT-42")

    assert _gutter(parent_win, "PARENT-42") == ("none", "suspect")
    _activate(child_win)
    assert _gutter(child_win, "DOC-1") == ("suspect", "none")
    assert not child_win.grid_pane.banner.isHidden()


def test_head_move_refreshes_inspector_link_state_and_ack_selected(
    qtbot, repo_root
) -> None:
    """The gutter, the Inspector's link tag + Ack button and the banner's
    "Ack selected" must agree after a parent revision is committed."""
    from PySide6.QtWidgets import QLabel, QPushButton

    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    svc = DocumentService(repo_root, cache=True)
    parent_win = DocumentEditorWindow("PARENT", svc)
    child_win = DocumentEditorWindow("DOC", svc)
    qtbot.addWidget(parent_win)
    qtbot.addWidget(child_win)
    child_win._set_read_only(False)
    child_win.controller.select_object("DOC", "DOC-1")

    def outgoing_row():
        lst = child_win.inspector._outgoing
        return lst.itemWidget(lst.item(0))

    assert "[OK]" in outgoing_row().findChild(QLabel).text()

    svc.set_description("PARENT", "PARENT-42", "revised parent text")
    parent_win.controller.reload_document("PARENT")
    parent_win.controller.commit_document("PARENT", "Revise PARENT-42")
    _activate(child_win)

    assert _gutter(child_win, "DOC-1") == ("suspect", "none")
    assert "[SUSPECT]" in outgoing_row().findChild(QLabel).text()
    assert outgoing_row().findChild(QPushButton) is not None  # Ack
    assert child_win.grid_pane.banner._ack_selected_btn.isEnabled()


# --- project explorer --------------------------------------------------------


def test_explorer_picks_up_external_doc_create_and_delete(qtbot, repo_root) -> None:
    from mellon.gui.models.project_tree import DOC_ID_ROLE
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    w = ProjectExplorerWindow()
    qtbot.addWidget(w)
    assert w.open_repo(repo_root)

    def listed_ids() -> set[str]:
        model = w._project_model
        return {
            model.data(model.index(row, 0), DOC_ID_ROLE)
            for row in range(model.rowCount())
        }

    assert listed_ids() == {"DOC", "PARENT"}

    _external_service(repo_root).create_document("NEWDOC", title="New")
    _activate(w)
    assert listed_ids() == {"DOC", "PARENT", "NEWDOC"}

    shutil.rmtree(repo_root / "NEWDOC")
    _activate(w)
    assert listed_ids() == {"DOC", "PARENT"}


def _explorer(qtbot, root: Path):
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    w = ProjectExplorerWindow()
    qtbot.addWidget(w)
    assert w.open_repo(root)
    return w


def test_closed_editor_windows_are_freed(qtbot, repo_root) -> None:
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    explorer = _explorer(qtbot, repo_root)
    editors = []
    for _ in range(3):
        for doc_id in ("DOC", "PARENT"):
            explorer._open_document(doc_id)
            editors.append(explorer._editor_windows[doc_id])
            editors[-1].close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert explorer._editor_windows == {}
    assert explorer._open_list.count() == 0
    assert not any(shiboken6.isValid(e) for e in editors)


def test_opening_another_repo_closes_old_repo_editors(
    qtbot, repo_root, tmp_path_factory
) -> None:
    # A second clone with the same doc ids, e.g. another branch's worktree.
    clone_b = tmp_path_factory.mktemp("clone_b")
    shutil.copytree(repo_root, clone_b, dirs_exist_ok=True)
    explorer = _explorer(qtbot, repo_root)
    explorer._open_document("DOC")
    old = explorer._editor_windows["DOC"]

    assert explorer.open_repo(clone_b)

    assert explorer._editor_windows == {}
    assert explorer._open_list.count() == 0
    explorer._open_document("DOC")
    new = explorer._editor_windows["DOC"]
    assert new is not old
    assert new.controller.root == clone_b.resolve()


def test_failed_repo_open_clears_document_list(qtbot, repo_root, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    explorer = _explorer(qtbot, repo_root)
    assert explorer._project_model.rowCount() == 2

    assert not explorer.open_repo(repo_root / "does-not-exist")
    assert explorer._project_model.rowCount() == 0


def test_refused_folder_keeps_the_open_repo(qtbot, repo_root, monkeypatch) -> None:
    """Picking a document folder instead of the clone root is refused before
    anything loads; the repo that was open stays listed and usable."""
    texts = _capture_boxes(monkeypatch)
    explorer = _explorer(qtbot, repo_root)

    assert not explorer.open_repo(repo_root / "DOC")

    assert len(texts) == 1 and "not the root of its git work tree" in texts[0]
    assert explorer.controller.root == repo_root.resolve()
    assert explorer._project_model.rowCount() == 2
    assert explorer._act_new_document.isEnabled()


# MELLON-STORE-SRS blind tests


@pytest.fixture
def editor_repo(tmp_path, git_env):
    root = git_init(git_env, tmp_path / "ed")
    make_doc(root, "DOC", n=3)
    git_commit_all(git_env, root, "seed")
    return root


def test_store_srs_37_reread_after_edit_names_file_and_line(gui, editor_repo):
    """MELLON-STORE-SRS-37: when a Document Editor reads its document again and a
    data file can't be read, it shall show the load error. After an edit to
    DOC-2 while DOC-1.xml holds conflict markers, the window shows the file and
    the line of the load error, not a general failure."""
    ed = gui.editor(editor_repo, "DOC")
    gui.set_read_only(ed, False)
    _break_doc_1(editor_repo)
    gui.messages.clear()
    gui.grid_edit(ed, "DOC-2", "rationale", "edited after the break")
    shown = _shows_load_error(gui, ed)
    assert "DOC-1.xml" in shown, f"load error file not shown: {shown!r}"
    assert f"line {CONFLICT_LINE}" in shown or f":{CONFLICT_LINE}" in shown, shown


def test_store_srs_37_reread_after_edit_says_merge_conflict(gui, editor_repo):
    """MELLON-STORE-SRS-37 with -30: the load error the Document Editor shows
    after an edit-triggered re-read is the load error, so for a file holding
    conflict markers it says the file looks like an unresolved merge conflict."""
    ed = gui.editor(editor_repo, "DOC")
    gui.set_read_only(ed, False)
    _break_doc_1(editor_repo)
    gui.messages.clear()
    gui.grid_edit(ed, "DOC-2", "rationale", "edited after the break")
    shown = _shows_load_error(gui, ed)
    assert "DOC-1.xml" in shown, f"precondition: the load error was shown: {shown!r}"
    assert CONFLICT_HINT.search(shown), shown


def _attempt(gui, ed, kind: str, tmp_path: Path):
    from PySide6.QtWidgets import QFileDialog

    if kind == "grid-cell":
        gui.grid_edit(ed, "DOC-1", "rationale", "stale grid edit")
        gui.grid_edit(ed, "DOC-2", "rationale", "stale grid edit")
    elif kind == "inspector-description":
        gui.inspector_description(ed, "DOC-1", "stale inspector text")
    elif kind == "new-sibling":
        gui.select(ed, "DOC-2")
        gui.trigger(ed, "New")
    elif kind == "attach-figure":
        svg = _svg_file(tmp_path)
        gui.monkeypatch.setattr(QFileDialog, "getOpenFileName",
                                staticmethod(lambda *a, **k: (str(svg), "SVG (*.svg)")))
        gui.select(ed, "DOC-2")
        ed.grid_pane.attach_figure("DOC", "DOC-2")
        gui.process()
    elif kind == "table":
        gui.select(ed, "DOC-2")
        ed.grid_pane.insert_table("DOC", "DOC-2")
        gui.process()
    else:  # pragma: no cover
        raise AssertionError(kind)


EDIT_KINDS = ["grid-cell", "inspector-description", "new-sibling", "attach-figure", "table"]


@pytest.mark.parametrize("kind", EDIT_KINDS)
def test_store_srs_39_edits_refused_while_document_cant_be_read(gui, editor_repo, tmp_path, kind):
    """MELLON-STORE-SRS-39: while a Document Editor shows a copy of a document
    that can't be read now, it shall refuse each edit, also after the user
    leaves read-only mode. After F5 with DOC-1.xml broken, the edit is refused;
    after read-only mode is switched on and off it is refused again; every file
    in DOC's folder keeps its bytes."""
    ed = gui.editor(editor_repo, "DOC")
    gui.set_read_only(ed, False)
    _break_doc_1(editor_repo)
    gui.trigger(ed, "Reload from disk")
    assert any("DOC-1.xml" in t for _, _, t in gui.messages), "precondition: F5 showed the load error"
    folder = editor_repo / "DOC"
    before = snapshot(folder)
    _attempt(gui, ed, kind, tmp_path)
    assert changed(before, snapshot(folder)) == [], "an edit was written while DOC can't be read"
    gui.set_read_only(ed, True)
    gui.set_read_only(ed, False)
    _attempt(gui, ed, kind, tmp_path)
    assert changed(before, snapshot(folder)) == [], "an edit was written after leaving read-only mode"


@pytest.mark.parametrize("kind", EDIT_KINDS)
def test_store_srs_39_same_edit_is_written_while_document_reads(gui, editor_repo, tmp_path, kind):
    """MELLON-STORE-SRS-39 (other side of the condition): while the document can
    be read and read-only mode is off, the same edit is written to DOC's folder.
    This also shows that the refusal test above makes real edits."""
    ed = gui.editor(editor_repo, "DOC")
    gui.set_read_only(ed, False)
    folder = editor_repo / "DOC"
    before = snapshot(folder)
    _attempt(gui, ed, kind, tmp_path)
    assert changed(before, snapshot(folder)) != [], f"the {kind} edit wrote nothing"


@pytest.fixture
def recovered_editor(gui, editor_repo, tmp_path):
    ed = gui.editor(editor_repo, "DOC")
    gui.set_read_only(ed, False)
    good = _break_doc_1(editor_repo)
    gui.trigger(ed, "Reload from disk")
    _attempt(gui, ed, "grid-cell", tmp_path)
    write(editor_repo / "DOC" / "objects" / "DOC-1.xml", good)
    gui.messages.clear()
    gui.trigger(ed, "Reload from disk")
    return ed


def test_store_srs_40_reload_without_load_error_allows_edits(gui, recovered_editor, editor_repo):
    """MELLON-STORE-SRS-40: when a Document Editor reads its document again
    without a load error, it shall stop refusing edits for the load error. After
    DOC-1.xml is restored and F5 pressed, the Changed on disk banner is gone, no
    load error is shown, and with read-only mode off a grid edit is written."""
    ed = recovered_editor
    assert not gui.stale_banner_shown(ed), "Changed on disk banner still shown"
    assert not any("DOC-1.xml" in t for _, _, t in gui.messages), gui.messages
    gui.set_read_only(ed, False)
    gui.grid_edit(ed, "DOC-2", "rationale", "after recovery")
    obj = load_document(editor_repo / "DOC").objects["DOC-2"]
    assert obj.attrs.get("rationale") == "after recovery", obj


def test_store_srs_40_read_only_mode_still_applies_after_reload(gui, recovered_editor, editor_repo):
    """MELLON-STORE-SRS-40: after the reload, read-only mode applies as before:
    with read-only mode on, a grid edit stays refused."""
    ed = recovered_editor
    gui.set_read_only(ed, True)
    folder = editor_repo / "DOC"
    before = snapshot(folder)
    gui.grid_edit(ed, "DOC-2", "rationale", "read-only edit")
    assert changed(before, snapshot(folder)) == []


def test_store_srs_210_gui_open_and_reload_change_no_file(gui, read_only_repo):
    """MELLON-STORE-SRS-210: when Mellon opens or reloads a document, it shall
    leave each data file and figure file unchanged. Opening the repository in
    the Project Explorer and both documents in Document Editors, and pressing F5
    in each, changes no file outside .git."""
    root, _ = read_only_repo
    before = snapshot(root, include_git=False)
    pe, ok = gui.explorer(root)
    assert ok
    eds = [gui.editor(root, d) for d in ("PARENT", "CHILD")]
    gui.press_f5(pe)
    for ed in eds:
        gui.press_f5(ed)
    assert changed(before, snapshot(root, include_git=False)) == []
