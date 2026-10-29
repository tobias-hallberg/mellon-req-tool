"""Branding / Mellon logo asset tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()


# --- branding module --------------------------------------------------------


def test_window_icon_has_multiple_sizes(qapp):
    from mellon.gui import branding

    icon = branding.window_icon()
    assert not icon.isNull()
    sizes = icon.availableSizes()
    assert len(sizes) >= 4
    # Covers both small (taskbar) and large (Alt-Tab) target sizes.
    assert any(s.width() <= 16 for s in sizes)
    assert any(s.width() >= 64 for s in sizes)


def test_app_icon_is_non_null(qapp):
    from mellon.gui import branding

    icon = branding.app_icon()
    assert not icon.isNull()


def test_about_pixmap_renders_at_requested_size(qapp):
    from mellon.gui import branding

    pm = branding.about_pixmap(96)
    assert not pm.isNull()
    assert pm.width() == 96 and pm.height() == 96


def test_branding_cache_invalidates_on_theme_change(qapp):
    from mellon.gui import branding
    from mellon.gui.theme import ThemeManager

    # The conftest's autouse fixture resets ThemeManager between tests, so the
    # branding module's earlier subscription is to a discarded singleton. Clear
    # both module-level globals so window_icon() rebinds to the live one.
    branding._cache.clear()
    branding._subscribed = False

    tm = ThemeManager.instance()
    tm.apply("dark", persist=False)
    branding.window_icon()
    assert branding._cache, "cache should be populated after window_icon()"
    tm.apply("light", persist=False)
    assert not branding._cache, "theme_changed should fire refresh()"


def test_window_icon_accent_swaps_per_theme(qapp):
    """Dark and light themes must produce different pixmaps for the same size.

    The ink is `fg_1` (different) and the accent dot swaps `#4fb3b8` (dark) for
    `#1e8085` (light), so the resulting QPixmap bytes will differ.
    """
    from mellon.gui import branding
    from mellon.gui.theme import ThemeManager

    tm = ThemeManager.instance()
    tm.apply("dark", persist=False)
    dark = branding.about_pixmap(96).toImage()
    tm.apply("light", persist=False)
    light = branding.about_pixmap(96).toImage()
    assert dark != light


# --- About dialog -----------------------------------------------------------


def test_about_dialog_constructs(qapp, qtbot):
    from mellon.gui.dialogs.about_dialog import AboutDialog

    dlg = AboutDialog()
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "About Mellon"


# --- window-icon wiring -----------------------------------------------------


def test_project_explorer_window_has_icon(qapp, qtbot):
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    w = ProjectExplorerWindow()
    qtbot.addWidget(w)
    assert not w.windowIcon().isNull()


@pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(),
    reason="req-test fixture not available",
)
def test_document_editor_window_has_icon(qapp, qtbot, tmp_path, clean_worktree):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)
    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    w = DocumentEditorWindow(docs[0], svc)
    qtbot.addWidget(w)
    assert not w.windowIcon().isNull()


# --- Help → About wiring ----------------------------------------------------


def test_project_explorer_has_help_about_action(qapp, qtbot):
    from mellon.gui.project_explorer_window import ProjectExplorerWindow

    w = ProjectExplorerWindow()
    qtbot.addWidget(w)
    labels = [a.text() for a in w._main_menu.actions()]
    assert any("About" in t for t in labels)


@pytest.mark.skipif(
    not (REQ_TEST / ".git").exists(),
    reason="req-test fixture not available",
)
def test_document_editor_has_help_about_action(qapp, qtbot, tmp_path, clean_worktree):
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)
    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    w = DocumentEditorWindow(docs[0], svc)
    qtbot.addWidget(w)

    labels = [a.text() for a in w._main_menu.actions()]
    assert any("About" in t for t in labels)
