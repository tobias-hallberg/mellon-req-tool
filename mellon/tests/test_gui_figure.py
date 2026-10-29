"""Figure UI: toolbar dropdown button, double-click viewer, attach/clear wiring.

Runs against the committed `req-test/` fixture on Qt's offscreen platform.
Tests drive the controller / panel API directly (and monkeypatch the modal
file/confirm dialogs) rather than simulating real mouse events — faster and
less fragile, matching the other GUI test modules.
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


_SAMPLE_SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 20" '
    'width="40" height="20"><rect width="40" height="20" fill="#44aaff"/></svg>\n'
)


@pytest.fixture
def window(qtbot, tmp_path, clean_worktree):
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


@pytest.fixture
def sample_svg(tmp_path) -> Path:
    p = tmp_path / "diagram.svg"
    p.write_text(_SAMPLE_SVG, encoding="utf-8")
    return p


@pytest.fixture
def drawio_svg(tmp_path) -> Path:
    """An editable draw.io SVG: a picture plus the diagram in `content`."""
    p = tmp_path / "diagram.drawio.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40px" height="20px" '
        'content="&lt;mxfile&gt;&lt;diagram id=&quot;p&quot;&gt;&lt;mxGraphModel&gt;'
        '&lt;root&gt;&lt;mxCell id=&quot;0&quot;/&gt;&lt;/root&gt;'
        '&lt;/mxGraphModel&gt;&lt;/diagram&gt;&lt;/mxfile&gt;">'
        '<rect width="40" height="20" fill="#44aaff"/></svg>\n',
        encoding="utf-8",
    )
    return p


def _select_first_object(window) -> tuple[str, str]:
    doc_id = window.controller.current_doc_id()
    obj_id = next(iter(window.controller.document(doc_id).iter_outline_ids()))
    window.controller.select_object(doc_id, obj_id)
    window._refresh_action_states()
    return doc_id, obj_id


def _attach_via_service(window, doc_id: str, obj_id: str, svg: Path) -> None:
    window.controller.require_service().set_figure(doc_id, obj_id, svg)
    window.controller.reload_document(doc_id)


# --- toolbar action / button state -----------------------------------------


def test_figure_action_enabled_with_selection(window) -> None:
    _select_first_object(window)
    assert window._act_figure.isEnabled()
    assert window._figure_button.isEnabled()


def test_figure_action_gated_by_read_only(window) -> None:
    _select_first_object(window)
    window._set_read_only(True)
    # Attach/replace is a mutation → disabled; the button stays enabled so the
    # menu's "View full size" still works in read-only mode.
    assert not window._act_figure.isEnabled()
    assert window._figure_button.isEnabled()


# --- dropdown menu reflects figure presence ---------------------------------


def test_figure_menu_labels_without_figure(window) -> None:
    _select_first_object(window)
    window._populate_figure_menu()
    labels = [a.text() for a in window._figure_menu.actions()]
    assert labels == [
        "New draw.io figure…",
        "Attach figure…",
        "Clear figure",
        "View full size",
    ]
    by_label = {a.text(): a for a in window._figure_menu.actions()}
    assert by_label["New draw.io figure…"].isEnabled()
    assert by_label["Attach figure…"].isEnabled()
    assert not by_label["Clear figure"].isEnabled()
    assert not by_label["View full size"].isEnabled()


def test_figure_menu_labels_with_figure(window, sample_svg) -> None:
    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, sample_svg)
    window._populate_figure_menu()
    by_label = {a.text(): a for a in window._figure_menu.actions()}
    assert "Replace figure…" in by_label
    assert by_label["Replace figure…"].isEnabled()
    assert by_label["Clear figure"].isEnabled()
    assert by_label["View full size"].isEnabled()


def test_figure_menu_drawio_edit_disabled_for_plain_svg(window, sample_svg) -> None:
    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, sample_svg)
    window._populate_figure_menu()
    by_label = {a.text(): a for a in window._figure_menu.actions()}
    edit = by_label["Edit in draw.io…"]
    assert not edit.isEnabled()
    assert "wasn't made with draw.io" in edit.toolTip()


def test_figure_menu_drawio_edit_enabled_for_drawio_svg(window, drawio_svg) -> None:
    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, drawio_svg)
    window._populate_figure_menu()
    by_label = {a.text(): a for a in window._figure_menu.actions()}
    assert by_label["Edit in draw.io…"].isEnabled()


def test_figure_menu_drawio_disabled_read_only(window) -> None:
    _select_first_object(window)
    window._set_read_only(True)
    window._populate_figure_menu()
    by_label = {a.text(): a for a in window._figure_menu.actions()}
    assert not by_label["New draw.io figure…"].isEnabled()


# --- draw.io round trip (launch monkeypatched) ------------------------------


@pytest.fixture
def fake_drawio(monkeypatch):
    """draw.io "installed"; records what it was asked to open."""
    from mellon import drawio

    opened: list[Path] = []
    monkeypatch.setattr(drawio, "find_drawio", lambda: "draw.io.exe")
    monkeypatch.setattr(drawio, "open_in_drawio", lambda exe, path: opened.append(path))
    return opened


def test_new_drawio_figure_creates_blank_and_opens_it(window, fake_drawio) -> None:
    from mellon import drawio

    doc_id, obj_id = _select_first_object(window)
    window.grid_pane.edit_figure_in_drawio(doc_id, obj_id)

    obj = window.controller.document(doc_id).objects[obj_id]
    assert obj.figure_path == f"figures/{obj_id}.svg"
    (opened,) = fake_drawio
    assert opened == window.controller.folder(doc_id) / obj.figure_path
    assert drawio.is_drawio_file(opened)
    # Created through this window, so it isn't an outside change.
    assert window.controller.changed_figures() == frozenset()


def test_edit_in_drawio_opens_existing_figure(window, fake_drawio, drawio_svg) -> None:
    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, drawio_svg)
    before = (window.controller.folder(doc_id) / "figures" / f"{obj_id}.svg").read_bytes()

    window.grid_pane.edit_figure_in_drawio(doc_id, obj_id)

    (opened,) = fake_drawio
    assert opened.name == f"{obj_id}.svg"
    assert opened.read_bytes() == before


def test_edit_in_drawio_without_drawio_explains_and_writes_nothing(
    window, monkeypatch
) -> None:
    from mellon import drawio
    from mellon.gui.panels import grid_pane

    shown: list[str] = []
    monkeypatch.setattr(drawio, "find_drawio", lambda: None)
    monkeypatch.setattr(
        grid_pane.QMessageBox,
        "information",
        staticmethod(lambda parent, title, text: shown.append(text)),
    )
    doc_id, obj_id = _select_first_object(window)
    window.grid_pane.edit_figure_in_drawio(doc_id, obj_id)

    assert shown and "MELLON_DRAWIO" in shown[0]
    assert window.controller.document(doc_id).objects[obj_id].figure_path is None


# --- attach / clear wiring (modal dialogs monkeypatched) --------------------


def test_attach_figure_via_grid_sets_figure(window, sample_svg, monkeypatch) -> None:
    from mellon.gui.panels import grid_pane

    monkeypatch.setattr(
        grid_pane.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *a, **k: (str(sample_svg), "")),
    )
    doc_id, obj_id = _select_first_object(window)
    window.grid_pane.attach_figure(doc_id, obj_id)

    obj = window.controller.document(doc_id).objects[obj_id]
    assert obj.figure_path == f"figures/{obj_id}.svg"


def test_clear_figure_via_grid_removes(window, sample_svg, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from mellon.gui.panels import grid_pane

    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, sample_svg)
    assert window.controller.document(doc_id).objects[obj_id].figure_path is not None

    monkeypatch.setattr(
        grid_pane.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    window.grid_pane.clear_figure(doc_id, obj_id)

    assert window.controller.document(doc_id).objects[obj_id].figure_path is None


# --- delegate hit-rect ------------------------------------------------------


def test_delegate_figure_rect_present_and_absent(window, sample_svg) -> None:
    from PySide6.QtCore import QRect

    from mellon.gui.models.objects_grid import OBJ_FIGURE_PATH_ROLE  # noqa: F401

    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, sample_svg)

    model = window.grid_pane._model
    desc_col = model.column_index("heading_or_text")
    delegate = window.grid_pane.table.itemDelegateForColumn(desc_col)
    font = window.grid_pane.table.font()
    rect = QRect(0, 0, 420, 200)

    figured_row = model.row_for_obj_id(obj_id)
    idx = model.index(figured_row, desc_col)
    hit = delegate.figure_rect(rect, font, idx)
    assert hit is not None
    assert hit.width() > 0 and hit.height() > 0

    # A different object with no figure → None.
    other = next(
        oid
        for oid in window.controller.document(doc_id).iter_outline_ids()
        if oid != obj_id
    )
    other_idx = model.index(model.row_for_obj_id(other), desc_col)
    assert delegate.figure_rect(rect, font, other_idx) is None


# --- viewer dialog ----------------------------------------------------------


def test_view_figure_opens_dialog_with_path(window, sample_svg, monkeypatch) -> None:
    from mellon.gui.panels import grid_pane

    captured: dict[str, str] = {}

    class _FakeDialog:
        def __init__(self, path, obj_id, parent=None):
            captured["path"] = path
            captured["obj_id"] = obj_id

        def exec(self):
            return 0

    monkeypatch.setattr(grid_pane, "FigureViewDialog", _FakeDialog)
    doc_id, obj_id = _select_first_object(window)
    _attach_via_service(window, doc_id, obj_id, sample_svg)

    window.grid_pane.view_figure(doc_id, obj_id)
    assert captured["obj_id"] == obj_id
    opened = Path(captured["path"])
    assert opened.name == f"{obj_id}.svg"
    assert opened.parent.name == "figures"


def test_view_figure_noop_without_figure(window, monkeypatch) -> None:
    from mellon.gui.panels import grid_pane

    called = False

    class _FakeDialog:
        def __init__(self, *a, **k):
            nonlocal called
            called = True

        def exec(self):
            return 0

    monkeypatch.setattr(grid_pane, "FigureViewDialog", _FakeDialog)
    doc_id, obj_id = _select_first_object(window)
    window.grid_pane.view_figure(doc_id, obj_id)
    assert called is False


def test_thumbnail_keeps_drawio_adaptive_colours(qtbot, tmp_path) -> None:
    """draw.io writes `light-dark()` fills, which raw QtSvg paints black."""
    from PySide6.QtGui import QColor

    from mellon.gui.delegates import MultiLineTextDelegate

    svg = tmp_path / "adaptive.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#ff0000" '
        'style="fill: light-dark(#ff0000, #000000);"/></svg>',
        encoding="utf-8",
    )
    pm = MultiLineTextDelegate()._thumbnail(str(svg), 40)
    assert pm is not None
    assert pm.toImage().pixelColor(20, 10) == QColor("#ff0000")


def test_thumbnail_paints_figure_on_white(qtbot, tmp_path) -> None:
    from PySide6.QtGui import QColor

    from mellon.gui.delegates import MultiLineTextDelegate

    svg = tmp_path / "line.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect x="0" y="0" width="4" height="4" fill="#000"/></svg>',
        encoding="utf-8",
    )
    pm = MultiLineTextDelegate()._thumbnail(str(svg), 40)
    assert pm.toImage().pixelColor(30, 15) == QColor("#ffffff")


def test_blank_drawio_figure_has_no_thumbnail(qtbot, tmp_path) -> None:
    from mellon import drawio
    from mellon.gui.delegates import MultiLineTextDelegate

    svg = tmp_path / "blank.svg"
    svg.write_bytes(drawio.BLANK_DRAWIO_SVG)
    assert MultiLineTextDelegate()._thumbnail(str(svg), 200) is None


def test_figure_view_dialog_constructs_from_svg(qtbot, sample_svg) -> None:
    from mellon.gui.dialogs.figure_view import FigureViewDialog

    dlg = FigureViewDialog(str(sample_svg), "OBJ-1")
    qtbot.addWidget(dlg)
    assert dlg._view.is_valid()
    assert dlg.windowTitle() == "Figure — OBJ-1"
