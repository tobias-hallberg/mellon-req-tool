"""GUI: column reordering writes the active view's XML on disk.

Lets the user drag a column header to reorder; when a view is active the
new order is written back to `views/<slug>.xml`. When no view is active
(default bucket), order is persisted in QSettings.
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


def _user_attr_visual_order(grid_pane) -> list[str]:
    return grid_pane._current_user_attr_order()


def _seed_view(controller, doc_id: str, name: str, visible: tuple[str, ...]) -> str:
    """Create a view via the editor and reload the doc so the controller sees it."""
    slug = controller.create_view(doc_id, name, visible)
    controller.reload_document(doc_id)
    return slug


def test_drag_reorder_writes_view_xml(window):
    w, root = window
    w.controller.select_document("VCU3-SRS")
    visible = ("req-type", "priority", "status", "verification-method", "rationale")
    slug = _seed_view(w.controller, "VCU3-SRS", "Complete", visible)

    w.grid_pane.apply_view(slug, visible)

    before = _user_attr_visual_order(w.grid_pane)
    assert list(visible) == before

    # Move the last visible user-attr to the front of the user-attr range.
    header = w.grid_pane.table.horizontalHeader()
    last_name = before[-1]
    last_logical = w.grid_pane._model.column_index(last_name)
    last_visual = header.visualIndex(last_logical)
    n_system = w.grid_pane._system_column_count()
    header.moveSection(last_visual, n_system)

    expected = [last_name] + before[:-1]
    assert _user_attr_visual_order(w.grid_pane) == expected

    # The view XML on disk now lists attributes in the new order.
    xml = (root / "VCU3-SRS" / "views" / "complete.xml").read_text(
        encoding="utf-8"
    )
    pos = [xml.index(f'name="{name}"') for name in expected]
    assert pos == sorted(pos), xml


def test_drag_reorder_does_not_write_view_xml_when_read_only(window):
    w, root = window
    w.controller.select_document("VCU3-SRS")
    visible = ("req-type", "priority", "status", "verification-method", "rationale")
    slug = _seed_view(w.controller, "VCU3-SRS", "Complete", visible)
    w.grid_pane.apply_view(slug, visible)
    view_file = root / "VCU3-SRS" / "views" / "complete.xml"
    before_xml = view_file.read_bytes()
    w._set_read_only(True)

    before = _user_attr_visual_order(w.grid_pane)
    header = w.grid_pane.table.horizontalHeader()
    last_visual = header.visualIndex(w.grid_pane._model.column_index(before[-1]))
    header.moveSection(last_visual, w.grid_pane._system_column_count())

    # The reorder stays visual only; the document's view XML is untouched.
    assert _user_attr_visual_order(w.grid_pane) == [before[-1]] + before[:-1]
    assert view_file.read_bytes() == before_xml


def test_drag_reorder_default_bucket_persists_in_qsettings(window):
    from mellon.gui import settings as gui_settings

    w, _root = window
    w.controller.select_document("VCU3-SRS")

    # No view active → default bucket.
    assert w.grid_pane.current_active_view_slug() is None

    before = _user_attr_visual_order(w.grid_pane)
    assert before, "expected at least one user-attribute column"

    header = w.grid_pane.table.horizontalHeader()
    last_logical = w.grid_pane._model.column_index(before[-1])
    last_visual = header.visualIndex(last_logical)
    header.moveSection(last_visual, w.grid_pane._system_column_count())

    expected = [before[-1]] + before[:-1]
    stored = gui_settings.make_settings().value(
        "grid/order/VCU3-SRS/__default", None
    )
    if isinstance(stored, str):
        stored = [s for s in stored.split(",") if s]
    assert list(stored) == expected


def test_system_columns_cannot_be_dragged_into_user_range(window):
    w, _root = window
    w.controller.select_document("VCU3-SRS")

    header = w.grid_pane.table.horizontalHeader()
    n_system = w.grid_pane._system_column_count()
    assert n_system >= 1

    user_order_before = _user_attr_visual_order(w.grid_pane)
    # Try to move the first system column past the system/user boundary.
    header.moveSection(0, n_system + 1)

    # Boundary guard reverts the move; user-attr order is unchanged.
    assert _user_attr_visual_order(w.grid_pane) == user_order_before
    assert header.visualIndex(0) == 0


def test_apply_view_applies_column_order_from_xml(window):
    w, _root = window
    w.controller.select_document("VCU3-SRS")

    visible = ("req-type", "priority", "status", "verification-method", "rationale")
    slug = _seed_view(w.controller, "VCU3-SRS", "Complete", visible)

    # Simulate the view XML having been edited to reverse order, e.g. via MCP.
    reversed_visible = tuple(reversed(visible))
    from mellon.editor import set_view_visible
    set_view_visible(w.controller.folder("VCU3-SRS"), slug, reversed_visible)
    w.controller.reload_document("VCU3-SRS")
    w.controller.select_document("VCU3-SRS")

    # Apply the view (as the view menu would).
    w.grid_pane.apply_view(slug, reversed_visible)
    assert _user_attr_visual_order(w.grid_pane) == list(reversed_visible)
