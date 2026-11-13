"""Tests for the `QSettings` factory (`gui/settings.py`)."""

from __future__ import annotations

import os
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_make_settings_honours_the_default_format(qtbot):
    """`QSettings(org, app)` ignores `setDefaultFormat` and always uses the
    native store (the registry on Windows), so the test isolation's INI
    redirect did nothing and every test run wrote to the real user store."""
    from PySide6.QtCore import QSettings

    from mellon.gui import settings as gui_settings

    s = gui_settings.make_settings()

    # conftest's autouse fixture set IniFormat + a temp folder for this test.
    assert QSettings.defaultFormat() == QSettings.Format.IniFormat
    assert s.format() == QSettings.Format.IniFormat
    assert s.scope() == QSettings.Scope.UserScope
    assert (s.organizationName(), s.applicationName()) == (
        gui_settings.ORG,
        gui_settings.APP,
    )
    s.setValue("probe", "x")
    s.sync()
    assert s.status() == QSettings.Status.NoError
    assert Path(s.fileName()).is_file()


# MELLON-GUI-SRS blind tests
from PySide6.QtWidgets import QApplication, QSplitter  # noqa: E402

from tests.gui_support import (  # noqa: E402
    SRS,
    SYRS,
    _doc_dirty,
    _git_commit_all,
    _plain,
    _snapshot,
    _snapshot_diff,
    _system_columns_ok,
)


# --------------------------------------------------------------------------- window state


def _pane_sizes(ed) -> tuple[int, int]:
    sp = [s for s in ed.findChildren(QSplitter) if s.count() == 3][0]
    sizes = sp.sizes()
    return sizes[0], sizes[2]


def _set_pane_sizes(ed, outline: int, right: int):
    sp = [s for s in ed.findChildren(QSplitter) if s.count() == 3][0]
    total = sum(sp.sizes())
    sp.setSizes([outline, total - outline - right, right])
    sp.splitterMoved.emit(outline, 1)
    QApplication.processEvents()


def test_gui_srs_133_editor_restores_pane_sizes_per_document(gui_gui, repo):
    """MELLON-GUI-SRS-133: a Document Editor restores the pane sizes its document's editor last had;
    another document keeps its own sizes."""
    pe = gui_gui.explorer(repo)
    parent = gui_gui.open_doc(pe, SYRS)
    parent.resize(700, 500)  # fits the screen, so the window reopens at this size
    gui_gui.app.processEvents()
    parent_sizes = _pane_sizes(parent)
    parent.close()
    doc = gui_gui.open_doc(pe, SRS)
    doc.resize(700, 500)
    gui_gui.app.processEvents()
    o, r = _pane_sizes(doc)
    _set_pane_sizes(doc, o + 57, r + 41)
    custom = _pane_sizes(doc)
    assert custom != (o, r)
    doc.close()
    gui_gui.app.processEvents()
    doc = gui_gui.open_doc(pe, SRS)
    assert _pane_sizes(doc) == custom
    parent = gui_gui.open_doc(pe, SYRS)
    assert _pane_sizes(parent) == parent_sizes != custom


def _settings_dump() -> dict:
    from mellon.gui.settings import make_settings

    s = make_settings()
    return {k: repr(s.value(k)) for k in s.allKeys()}


def test_gui_srs_134_settings_are_kept_outside_repository(gui_gui, repo):
    """MELLON-GUI-SRS-134: the theme, pane sizes, column widths, a hidden column and the active filter are kept
    in the user's settings, and the repository gets no change and no new file."""
    from mellon.schema import FilterRule
    from mellon.service.document import DocumentService

    DocumentService(repo).create_filter(SRS, "Musts", rules=[FilterRule(kind="attr", attr="priority", op="eq",
                                                                         value="Must")])
    _git_commit_all(repo, "Add a filter")
    before = _snapshot(repo)
    settings_before = _settings_dump()
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    gui_gui.action(ed, "Light").trigger()
    o, r = _pane_sizes(ed)
    _set_pane_sizes(ed, o + 31, r + 17)
    gui_gui.table(ed).horizontalHeader().resizeSection(gui_gui.col(ed, "Rationale"), 177)
    gp = ed.grid_pane
    gp.apply_visible_attrs(set(gp.all_user_attribute_names()) - {"priority"})
    filters = gui_gui.toolbar_button(ed, "Filters").menu()
    filters.aboutToShow.emit()  # the menu fills when it opens
    pick = [a for a in filters.actions() if _plain(a.text()) == "Musts"]
    assert pick, [a.text() for a in filters.actions()]
    pick[0].trigger()
    assert gp.active_filter() is not None
    ed.close()
    pe.close()
    gui_gui.app.processEvents()
    assert _snapshot_diff(before, _snapshot(repo)) == []
    assert _doc_dirty(repo, SRS) == []
    changed = {k for k, v in _settings_dump().items() if settings_before.get(k) != v}
    assert any("theme" in k for k in changed), changed
    assert any("filter" in k for k in changed), changed


def _geometry(w) -> tuple[int, int, int, int]:
    g = w.geometry()
    return g.x(), g.y(), g.width(), g.height()


def test_gui_srs_137_explorer_reopens_with_last_size_and_position(gui_gui, repo):
    """MELLON-GUI-SRS-137: a GUI window opens with the size and position it last had (Project Explorer)."""
    pe = gui_gui.explorer(repo)
    pe.resize(611, 433)
    pe.move(43, 61)
    gui_gui.app.processEvents()
    last = _geometry(pe)
    pe.close()
    gui_gui.app.processEvents()
    again = gui_gui.explorer(repo)
    assert _geometry(again) == last


def test_gui_srs_137_editor_reopens_with_last_size_and_position(gui_gui, repo):
    """MELLON-GUI-SRS-137: a GUI window opens with the size and position it last had (Document Editor)."""
    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    ed.resize(687, 451)
    ed.move(71, 53)
    gui_gui.app.processEvents()
    last = _geometry(ed)
    ed.close()
    gui_gui.app.processEvents()
    again = gui_gui.open_doc(pe, SRS)
    assert _geometry(again) == last


def test_gui_srs_65_zero_stored_widths_keep_columns_visible(gui_gui, repo):
    """MELLON-GUI-SRS-65: with a stored width of 0 for the state gutter, ID and description columns,
    a reopened grid still shows them, wider than 0."""
    from mellon.gui.settings import make_settings

    pe = gui_gui.explorer(repo)
    ed = gui_gui.open_doc(pe, SRS)
    hh = gui_gui.table(ed).horizontalHeader()
    for name in ("", "ID", "Description"):
        hh.resizeSection(gui_gui.headers(ed).index(name), 0)
    gui_gui.app.processEvents()
    ed.close()
    gui_gui.app.processEvents()
    s = make_settings()
    width_keys = [k for k in s.allKeys() if "width" in k.lower() and "grid" in k.lower()]
    for k in width_keys:  # make sure every stored width the GUI keeps for the grid says 0
        v = s.value(k)
        if isinstance(v, (int, str)) and str(v).isdigit():
            s.setValue(k, 0)
    s.sync()
    ed = gui_gui.open_doc(pe, SRS)
    assert _system_columns_ok(gui_gui, ed) == []
