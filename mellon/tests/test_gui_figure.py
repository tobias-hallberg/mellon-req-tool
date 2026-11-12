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


# MELLON-EDIT-SRS blind tests (176 and up)
from tests.edit_b_support import (  # noqa: E402
    SVG_A,
    SVG_B,
    SVG_RED,
    MessageBoxes,
    _choose_file,
    _esc,
    _fake_program,
    _figure_repo,
    _plain,
    activate,
    find_action,
    grid_ids,
    menu_action,
    open_editor,
    select,
    toolbar_menu,
)
from tests.store_support import DECL  # noqa: E402


SVG_GREEN = (DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
             '<rect width="40" height="20" fill="#00ff00"/></svg>\n').encode()


# Transparent background: only a small red mark in the middle.
SVG_TRANSPARENT = (
    DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
    '<rect x="40" y="20" width="20" height="10" fill="#ff0000"/></svg>\n').encode()


# A plain SVG whose text element reads <mxfile>: no draw.io content attribute.
SVG_MXFILE_TEXT = (
    DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="20">'
    '<text x="2" y="15">&lt;mxfile&gt;</text></svg>\n').encode()


_DIAGRAM = ('<mxfile host="drawio"><diagram id="p1" name="Page-1">'
            '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            '</root></mxGraphModel></diagram></mxfile>')


# A draw.io figure: the diagram in the root's content attribute.
SVG_DRAWIO = (
    DECL + '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" '
    f'content="{_esc(_DIAGRAM)}"><rect width="40" height="20" fill="#ffffff"/>'
    '</svg>\n').encode()


def _thumbnail_color(window, row_obj: str, where: str = "center"):
    """Colour of the grid's figure thumbnail for `row_obj`: at its centre, or
    3 px inside its top-left corner."""
    from PySide6.QtCore import Qt

    view = window.grid_pane.table
    model = view.model()
    dcol = next(c for c in range(model.columnCount())
                if model.headerData(c, Qt.Horizontal) == "Description")
    row = grid_ids(window).index(row_obj)
    index = model.index(row, dcol)
    view.scrollTo(index)
    delegate = view.itemDelegateForColumn(dcol)
    rect = delegate.figure_rect(view.visualRect(index), view.font(), index)
    assert rect is not None and rect.isValid(), "no thumbnail drawn"
    image = view.viewport().grab().toImage()
    point = rect.center() if where == "center" else rect.topLeft() + \
        type(rect.topLeft())(3, 3)
    return image.pixelColor(point).name()


def test_edit_srs_230_figure_menu_replaces_existing_figure(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-230: the Figure menu attaches the SVG file the user
    chooses, replacing any existing figure: attach A, then replace with B;
    the figure file holds B's bytes."""
    root = _figure_repo(tmp_path)
    MessageBoxes(monkeypatch)
    a = tmp_path / "a.svg"
    a.write_bytes(SVG_A)
    b = tmp_path / "b.svg"
    b.write_bytes(SVG_B)
    window = open_editor(qtbot, root, "F")
    select(window, "F-0", qtbot)
    _choose_file(monkeypatch, a)
    menu_action(window, "Figure", "Attach figure").trigger()
    qtbot.wait(10)
    fig = root / "F" / "figures" / "F-0.svg"
    assert fig.read_bytes() == SVG_A
    _choose_file(monkeypatch, b)
    attach = [a for a in toolbar_menu(window, "Figure").actions()
              if _plain(a.text()) in ("Replace figure", "Attach figure")]
    assert attach and attach[0].isEnabled()
    attach[0].trigger()
    qtbot.wait(10)
    assert fig.read_bytes() == SVG_B


def test_edit_srs_242_new_drawio_figure_only_without_figure(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-242: while the selected object has no figure the
    Figure menu offers New draw.io figure; with a figure attached it isn't
    offered, or is disabled."""
    root = _figure_repo(tmp_path, SVG_A)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "F")
    select(window, "F-1", qtbot)
    assert menu_action(window, "Figure", "New draw.io figure").isEnabled()
    select(window, "F-0", qtbot)
    offered = [a for a in toolbar_menu(window, "Figure").actions()
               if _plain(a.text()) == "New draw.io figure" and a.isVisible()
               and a.isEnabled()]
    assert offered == []


def _edit_in_drawio(window):
    found = [a for a in toolbar_menu(window, "Figure").actions()
             if _plain(a.text()) == "Edit in draw.io"]
    assert found, "the Figure menu has no Edit in draw.io"
    return found[0]


def test_edit_srs_245_edit_in_drawio_disabled_for_plain_svg_with_mxfile_text(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-245: a plain SVG whose text element reads <mxfile> (no
    draw.io content attribute) isn't a draw.io figure: Edit in draw.io is
    disabled."""
    root = _figure_repo(tmp_path, SVG_MXFILE_TEXT)
    MessageBoxes(monkeypatch)
    fake = _fake_program(tmp_path / "drawio" / "draw.io.exe")
    monkeypatch.setenv("MELLON_DRAWIO", str(fake))
    window = open_editor(qtbot, root, "F")
    select(window, "F-0", qtbot)
    assert _edit_in_drawio(window).isEnabled() is False


def test_edit_srs_245_edit_in_drawio_enabled_for_drawio_figure(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-245, other side (with -244): a draw.io figure, whose
    root content attribute carries the diagram, leaves Edit in draw.io
    enabled."""
    root = _figure_repo(tmp_path, SVG_DRAWIO)
    MessageBoxes(monkeypatch)
    fake = _fake_program(tmp_path / "drawio" / "draw.io.exe")
    monkeypatch.setenv("MELLON_DRAWIO", str(fake))
    window = open_editor(qtbot, root, "F")
    select(window, "F-0", qtbot)
    assert _edit_in_drawio(window).isEnabled() is True


def test_edit_srs_254_thumbnail_shows_the_figure(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-254: the grid shows a thumbnail of the object's figure
    with its description: a figure filled with one colour (green) gives a
    thumbnail area of that colour."""
    root = _figure_repo(tmp_path, SVG_GREEN)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "F")
    assert _thumbnail_color(window, "F-0") == "#00ff00"


def test_edit_srs_250_saved_figure_shows_on_return_without_reload(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-250: attach a blue figure from the Document Editor,
    overwrite its file in place with a red picture (as draw.io desktop saves)
    and return to the window: the thumbnail shows red, without a reload."""
    root = _figure_repo(tmp_path)
    MessageBoxes(monkeypatch)
    blue = tmp_path / "blue.svg"
    blue.write_bytes(SVG_A)
    window = open_editor(qtbot, root, "F")
    select(window, "F-0", qtbot)
    _choose_file(monkeypatch, blue)
    menu_action(window, "Figure", "Attach figure").trigger()
    qtbot.wait(10)
    assert _thumbnail_color(window, "F-0") == "#0000ff"
    fig = root / "F" / "figures" / "F-0.svg"
    with open(fig, "r+b") as fh:  # in place, as draw.io desktop writes
        fh.seek(0)
        fh.write(SVG_RED)
        fh.truncate()
    activate(window, qtbot)
    assert _thumbnail_color(window, "F-0") == "#ff0000"


def test_edit_srs_256_dark_theme_draws_figures_on_white(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-256: with the dark theme active, a figure with a
    transparent background is drawn on white, in the thumbnail and in View
    full size."""
    from PySide6.QtWidgets import QDialog, QWidget

    root = _figure_repo(tmp_path, SVG_TRANSPARENT)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "F")
    find_action(window, "Dark").trigger()
    qtbot.wait(20)
    assert window.palette().window().color().lightness() < 128, "theme not dark"
    assert _thumbnail_color(window, "F-0", "corner") == "#ffffff"
    seen = {}

    def fake_exec(dialog):
        dialog.show()
        qtbot.wait(30)
        image = dialog.grab().toImage()
        # the picture is 2:1, so the point at 1/4 width, mid height of the
        # dialog's picture area lies on the picture's transparent part
        views = [c for c in dialog.findChildren(QWidget) if c.width() > 300
                 and c.height() > 300]
        area = views[0].geometry() if views else dialog.rect()
        point = area.topLeft() + type(area.topLeft())(area.width() // 4,
                                                      area.height() // 2)
        seen["color"] = image.pixelColor(point).name()
        seen["mark"] = image.pixelColor(area.center()).name()
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    select(window, "F-0", qtbot)
    menu_action(window, "Figure", "View full size").trigger()
    qtbot.wait(10)
    assert seen.get("mark") == "#ff0000", "the full-size picture wasn't drawn"
    assert seen.get("color") == "#ffffff"
