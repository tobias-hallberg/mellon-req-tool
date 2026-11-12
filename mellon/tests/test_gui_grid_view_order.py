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


# MELLON-EDIT-SRS blind tests (1-175)
#
# Written blind from MELLON-EDIT-SRS and the public signatures (docs/specification.md,
# step 7). Helpers are in tests/edit_support.py, which builds on tests/store_support.py.

from tests.edit_support import (  # noqa: E402
    commit_all,  # noqa: F401, F811 -- fixture
    grid_ids,
    modals,  # noqa: F401, F811 -- fixture
    open_editor_window,
    write_doc,
)
from tests.store_support import git_env  # noqa: E402,F401,F811


def test_edit_srs_122_grid_rows_follow_outline_preorder(qtbot, tmp_path, modals, commit_all):
    """MELLON-EDIT-SRS-122: the grid lists the objects in outline order (pre-order), one row
    each, for a nested outline whose order differs from the numbers."""
    root = tmp_path
    outline = [("D-5", [("D-2", ["D-9", "D-1"]), "D-3"]), "D-0", ("D-4", ["D-8"])]
    ids = ["D-5", "D-2", "D-9", "D-1", "D-3", "D-0", "D-4", "D-8"]
    write_doc(root, "D", objects={i: ({"type": "Requirement"}, f"text {i}") for i in ids},
              outline=outline, next_number=10)
    commit_all(root)
    w = open_editor_window(qtbot, root, "D")
    assert grid_ids(w) == ids


# MELLON-EDIT-SRS blind tests (176 and up)
from lxml import etree  # noqa: E402

from tests.edit_b_support import (  # noqa: E402
    _FIXED_HEADERS,
    MessageBoxes,
    _attr_headers,
    _filters_repo,
    _new_view_handler,
    _plain,
    _views_menu_choose,
    answer_text,
    button,
    drive_dialogs,
    grid_headers,
    menu_action,
    menu_texts,
    open_editor,
)


def test_edit_srs_325_views_menu_lists_views_and_activates_choice(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-325: the Views menu lists Dev and Test; choosing Test
    makes Test the active view (its attribute columns show, in its order);
    choosing Dev then makes Dev active."""
    root = _filters_repo(tmp_path, views=[("Dev", ["status", "rationale"]),
                                          ("Test", ["count", "priority"])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    names = menu_texts(window, "Views")
    assert "Dev" in names and "Test" in names
    _views_menu_choose(window, "Test")
    qtbot.wait(10)
    assert window.grid_pane.current_active_view_slug() == "test"
    assert _attr_headers(window) == ["count", "priority"]
    _views_menu_choose(window, "Dev")
    qtbot.wait(10)
    assert window.grid_pane.current_active_view_slug() == "dev"
    assert _attr_headers(window) == ["status", "rationale"]


def test_edit_srs_326_active_view_shows_exactly_its_attribute_columns(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-326: with Test (count, priority) active, the attribute
    columns besides the fixed ones of MELLON-EDIT-SRS-327 (state, object ID,
    description and type) are exactly count, priority, in that order."""
    root = _filters_repo(tmp_path, views=[("Test", ["count", "priority"])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    _views_menu_choose(window, "Test")
    qtbot.wait(10)
    assert _attr_headers(window) == ["count", "priority"]


def test_edit_srs_327_system_columns_show_in_each_view(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-327: with a view listing only rationale active, the
    grid still shows the state, object ID, description and type columns;
    status, which the view doesn't list, is hidden."""
    root = _filters_repo(tmp_path, views=[("Only", ["rationale"])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    _views_menu_choose(window, "Only")
    qtbot.wait(10)
    shown = grid_headers(window)
    for column in _FIXED_HEADERS:
        assert column in shown, f"column {column or 'state'!r} hidden in a view"
    assert "rationale" in shown and "status" not in shown


def test_edit_srs_329_new_view_saves_name_and_chosen_columns(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-329: Views menu, New view..., name QA, choose status and
    priority: views/qa.xml lists status then priority, and the Views menu
    lists QA."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    drive_dialogs(monkeypatch, qtbot, _new_view_handler(qtbot, "QA", ["status", "priority"]))
    menu_action(window, "Views", "New view").trigger()
    qtbot.wait(10)
    view_file = root / "D" / "views" / "qa.xml"
    assert view_file.exists()
    el = etree.parse(str(view_file)).getroot()
    assert el.get("name") == "QA"
    assert [a.get("name") for a in el.findall("attribute")] == ["status", "priority"]
    assert "QA" in menu_texts(window, "Views")


def test_edit_srs_331_manage_views_renames_view(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-331: Views menu, Manage views..., rename QA to Review:
    the Views menu lists Review, not QA, and Review keeps QA's columns."""
    from PySide6.QtWidgets import QListWidget

    from mellon.loader import load_document

    root = _filters_repo(tmp_path, views=[("QA", ["status", "priority"])])
    MessageBoxes(monkeypatch)
    answer_text(monkeypatch, "Review")
    window = open_editor(qtbot, root, "D")

    def handler(dialog):
        lst = dialog.findChildren(QListWidget)[0]
        lst.setCurrentRow(0)
        button(dialog, "Rename").click()
        button(dialog, "Close").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Views", "Manage views").trigger()
    qtbot.wait(10)
    names = menu_texts(window, "Views")
    assert "Review" in names and "QA" not in names
    (view,) = load_document(root / "D").views
    assert view.name == "Review"
    assert list(view.visible) == ["status", "priority"]


def test_edit_srs_332_manage_views_deletes_view(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-332: Manage views..., delete Review (confirming): its
    view file is gone and the Views menu no longer lists it."""
    from PySide6.QtWidgets import QListWidget

    root = _filters_repo(tmp_path, views=[("Review", ["status"]), ("Dev", ["rationale"])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")

    def handler(dialog):
        lst = dialog.findChildren(QListWidget)[0]
        row = [lst.item(i).text() for i in range(lst.count())].index("Review")
        lst.setCurrentRow(row)
        button(dialog, "Delete").click()
        button(dialog, "Close").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Views", "Manage views").trigger()
    qtbot.wait(10)
    assert sorted(p.name for p in (root / "D" / "views").glob("*.xml")) == ["dev.xml"]
    names = menu_texts(window, "Views")
    assert "Review" not in names and "Dev" in names


def _header_menu(window, qtbot, monkeypatch, column_label: str):
    """Open the grid column header's context menu over `column_label`; the
    menu that would show is returned instead of waiting for a click."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication, QMenu

    menus = []

    def capture(menu, *args, **kwargs):
        menus.append(menu)
        return None

    for name in ("exec", "exec_", "popup"):
        monkeypatch.setattr(QMenu, name, capture, raising=False)
    view = window.grid_pane.table
    header = view.horizontalHeader()
    model = view.model()
    logical = next(c for c in range(model.columnCount())
                   if model.headerData(c, Qt.Horizontal) == column_label)
    pos = QPoint(header.sectionViewportPosition(logical) + 3, header.height() // 2)
    event = QContextMenuEvent(QContextMenuEvent.Mouse, pos, header.mapToGlobal(pos))
    QApplication.sendEvent(header.viewport(), event)
    qtbot.wait(10)
    if not menus and header.contextMenuPolicy() == Qt.CustomContextMenu:
        header.customContextMenuRequested.emit(pos)
        qtbot.wait(10)
    return menus[-1] if menus else None


@pytest.mark.xfail(strict=True, reason=(
    "not built: MELLON-EDIT-SRS-333 is Proposed"))
def test_edit_srs_333_column_header_hides_and_shows_attribute_column(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-333: from the grid's column header, hide the
    rationale column, then show it again."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    assert "rationale" in grid_headers(window)
    menu = _header_menu(window, qtbot, monkeypatch, "rationale")
    assert menu is not None, "the column header offers no menu"
    toggle = [a for a in menu.actions() if _plain(a.text()) == "rationale"
              and a.isCheckable()]
    assert toggle, "the header menu has no show/hide entry for rationale"
    toggle[0].trigger()
    qtbot.wait(10)
    assert "rationale" not in grid_headers(window)
    menu = _header_menu(window, qtbot, monkeypatch, "status")
    [a for a in menu.actions() if _plain(a.text()) == "rationale"][0].trigger()
    qtbot.wait(10)
    assert "rationale" in grid_headers(window)
