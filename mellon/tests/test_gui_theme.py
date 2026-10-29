"""Theme + column visibility tests.

Each test gets a fresh ThemeManager (autouse fixture in conftest) and a tmp
QSettings directory (same), so toggling doesn't bleed into siblings or the
real user config.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


REQ_TEST = (Path(__file__).resolve().parents[2] / "req-test").resolve()


# --- tokens & palette ---------------------------------------------------


def test_palettes_present():
    from mellon.gui.theme import TOKENS

    assert set(TOKENS) == {"dark", "light"}
    for name, palette in TOKENS.items():
        # Every token key should exist in both variants so format(**tokens)
        # doesn't KeyError when we hot-swap.
        shared = {
            "bg_0", "bg_1", "bg_2", "bg_3", "bg_hover", "bg_selected",
            "bg_selected_strong", "border_0", "border_1", "border_2",
            "fg_0", "fg_1", "fg_2", "fg_3",
            "accent", "accent_dim", "accent_bg", "warn_bg",
            "ok", "warn", "err", "purple", "blue", "suspect_mark",
        }
        assert shared <= set(palette)


def test_resolve_direct_choices(qapp):
    from mellon.gui.theme import resolve
    assert resolve("dark") == "dark"
    assert resolve("light") == "light"


# --- ThemeManager + QApplication -------------------------------------


def test_apply_dark_changes_palette(qapp):
    from mellon.gui.theme import TOKENS, ThemeManager

    tm = ThemeManager.instance()
    tm.apply("dark", persist=False)
    assert qapp.palette().color(qapp.palette().ColorRole.Window).name() == TOKENS["dark"]["bg_1"]


def test_apply_light_changes_palette(qapp):
    from mellon.gui.theme import TOKENS, ThemeManager

    tm = ThemeManager.instance()
    tm.apply("light", persist=False)
    assert qapp.palette().color(qapp.palette().ColorRole.Window).name() == TOKENS["light"]["bg_1"]


def test_theme_choice_persists(qapp):
    """Setting via apply() writes QSettings; next load_choice() returns it."""
    from mellon.gui.theme import ThemeManager

    tm = ThemeManager.instance()
    tm.apply("light", persist=True)
    # New manager instance reads the persisted value.
    ThemeManager.reset_for_tests()
    tm2 = ThemeManager.instance()
    assert tm2.load_choice() == "light"


def test_theme_changed_signal_fires(qapp, qtbot):
    from mellon.gui.theme import ThemeManager

    tm = ThemeManager.instance()
    tm.apply("dark", persist=False)
    seen: list[dict] = []
    tm.theme_changed.connect(lambda tokens: seen.append(tokens))
    tm.apply("light", persist=False)
    assert len(seen) == 1
    assert seen[0]["bg_1"].startswith("#")


# --- MainWindow Theme menu ------------------------------------------


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
    qtbot.addWidget(w)
    return w, dst


def test_theme_menu_has_three_options(window):
    w, _ = window
    assert set(w._theme_actions) == {"dark", "light", "system"}


def test_changing_theme_via_action_applies_palette(window, qapp):
    from mellon.gui.theme import TOKENS

    w, _ = window
    w._theme_actions["light"].trigger()
    assert qapp.palette().color(qapp.palette().ColorRole.Window).name() == TOKENS["light"]["bg_1"]
    w._theme_actions["dark"].trigger()
    assert qapp.palette().color(qapp.palette().ColorRole.Window).name() == TOKENS["dark"]["bg_1"]


def test_suspect_banner_restyles_on_theme_change(window):
    from mellon.gui.theme import TOKENS

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    # Start in dark.
    w._theme_actions["dark"].trigger()
    dark_style = w.grid_pane.banner.styleSheet()
    assert TOKENS["dark"]["warn"] in dark_style
    # Switch to light — banner re-styles via theme_changed signal.
    w._theme_actions["light"].trigger()
    light_style = w.grid_pane.banner.styleSheet()
    assert TOKENS["light"]["warn"] in light_style
    assert light_style != dark_style


# --- Column visibility (Views storage) -----------------------------


def test_user_attribute_columns_visible_by_default(window):
    """No default-hidden set: every manifest-declared attribute starts visible."""
    w, _ = window
    model = w.grid_pane._model
    for i in range(model.columnCount()):
        if not model.is_user_attribute_column(i):
            continue
        assert not w.grid_pane.table.isColumnHidden(i), (
            f"{model.column_name(i)} should start visible"
        )


def test_column_visibility_persists_across_window(qtbot, tmp_path: Path, clean_worktree):
    """A new DocumentEditorWindow reads persisted column state via QSettings."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)

    # First window: hide rationale via the public Views path.
    svc1 = DocumentService(dst, cache=True)
    docs1 = sorted(svc1.get_all_documents())
    w1 = DocumentEditorWindow(docs1[0], svc1)
    qtbot.addWidget(w1)
    rationale_i = w1.grid_pane._model.column_index("rationale")
    assert rationale_i >= 0
    visible = {n for n in w1.grid_pane.all_user_attribute_names() if n != "rationale"}
    w1.grid_pane.apply_visible_attrs(visible)
    assert w1.grid_pane.table.isColumnHidden(rationale_i)

    # Second window: the new grid pane should honor the persisted state.
    svc2 = DocumentService(dst, cache=True)
    docs2 = sorted(svc2.get_all_documents())
    w2 = DocumentEditorWindow(docs2[0], svc2)
    qtbot.addWidget(w2)
    rationale_i_2 = w2.grid_pane._model.column_index("rationale")
    assert w2.grid_pane.table.isColumnHidden(rationale_i_2)


def test_column_visibility_is_per_document(qtbot, tmp_path: Path, clean_worktree):
    """Hiding a column in one doc must not hide the same-named column in another."""
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    dst = tmp_path / "req-test"
    shutil.copytree(REQ_TEST, dst)
    clean_worktree(dst)

    svc = DocumentService(dst, cache=True)
    docs = sorted(svc.get_all_documents())
    # Need at least two docs that both declare a "rationale" attribute for
    # the assertion to be meaningful; pick the first two with overlap.
    target_attr = None
    chosen: list[str] = []
    for doc_id in docs:
        attrs = {a.name for a in svc.get_document(doc_id).attributes}
        if "rationale" in attrs:
            chosen.append(doc_id)
        if len(chosen) == 2:
            target_attr = "rationale"
            break
    if target_attr is None:
        pytest.skip("Fixture lacks two docs sharing an attribute name.")

    doc_a, doc_b = chosen
    wa = DocumentEditorWindow(doc_a, svc)
    qtbot.addWidget(wa)
    visible_a = {n for n in wa.grid_pane.all_user_attribute_names() if n != target_attr}
    wa.grid_pane.apply_visible_attrs(visible_a)
    a_idx = wa.grid_pane._model.column_index(target_attr)
    assert wa.grid_pane.table.isColumnHidden(a_idx)

    # Open doc B fresh — its rationale column must still be visible.
    wb = DocumentEditorWindow(doc_b, svc)
    qtbot.addWidget(wb)
    b_idx = wb.grid_pane._model.column_index(target_attr)
    assert b_idx >= 0
    assert not wb.grid_pane.table.isColumnHidden(b_idx), (
        f"{target_attr} should still be visible in {doc_b}; "
        "hidden_columns must be scoped per-document"
    )


def test_system_columns_never_hidden(window):
    """The four system columns (state, id, description, type) stay visible."""
    from mellon.gui.models.objects_grid import SYSTEM_COLUMNS

    w, _ = window
    model = w.grid_pane._model
    for col in SYSTEM_COLUMNS:
        i = model.column_index(col)
        assert i >= 0, f"{col} missing from grid"
        assert not w.grid_pane.table.isColumnHidden(i), (
            f"{col} should never be hidden"
        )
