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
