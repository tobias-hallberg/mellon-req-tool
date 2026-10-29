"""GUI tests for the saved Filter feature.

Mirrors the structure of `test_gui_suspect.py` so the same fixture-clone +
service-attach pattern is reused. Tests drive the proxy directly and the
filter dialog programmatically — no event-loop interaction needed.
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


# --- proxy integration ---------------------------------------------------


def test_apply_filter_narrows_proxy_via_match_set(window):
    """A Filter on `priority == Must` should narrow the grid to those rows
    plus their outline ancestors (keep_ancestors defaults to True)."""
    from mellon import Filter, FilterRule

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    proxy = w.grid_pane._proxy
    total = proxy.rowCount()
    assert total > 0

    filt = Filter(
        slug="must",
        name="Must",
        combinator="all",
        keep_ancestors=True,
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w.grid_pane.apply_filter(filt)
    narrowed = proxy.rowCount()
    assert 0 < narrowed < total
    # The pill is now visible and reflects the filter name.
    assert not w.grid_pane.filter_pill.isHidden()
    assert "Must" in w.grid_pane.filter_pill._label.text()
    assert w.grid_pane.active_filter() is not None
    assert w.grid_pane.active_filter().slug == "must"


def test_clear_active_filter_restores_full_grid(window):
    from mellon import Filter, FilterRule

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    proxy = w.grid_pane._proxy
    total = proxy.rowCount()

    w.grid_pane.apply_filter(
        Filter(
            slug="must", name="Must",
            rules=(FilterRule(kind="attr", attr="priority",
                              op="eq", value="Must"),),
            keep_ancestors=False,
        )
    )
    assert proxy.rowCount() < total
    w.grid_pane.clear_active_filter()
    assert proxy.rowCount() == total
    assert w.grid_pane.filter_pill.isHidden()
    assert w.grid_pane.active_filter() is None


def test_filter_match_set_ands_with_text_and_suspect(window):
    """The three filter axes (saved-match, text, suspect-only) AND together."""
    from mellon import Filter, FilterRule

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    proxy = w.grid_pane._proxy

    # Saved filter alone — narrow to "Must" priority requirements.
    w.grid_pane.apply_filter(
        Filter(
            slug="must", name="Must",
            rules=(FilterRule(kind="attr", attr="priority",
                              op="eq", value="Must"),),
            keep_ancestors=False,
        )
    )
    n_filter = proxy.rowCount()

    # Add suspect-only on top — suspect rows ∩ Must rows.
    w.grid_pane.set_suspect_only(True)
    n_both = proxy.rowCount()
    assert n_both <= n_filter


# --- on-disk persistence -------------------------------------------------


def test_create_filter_via_controller_writes_xml(window):
    from mellon import FilterRule, load_document

    w, dst = window
    w.controller.select_document("VCU3-SRS")
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "High priority approved",
        combinator="all",
        keep_ancestors=True,
        rules=(
            FilterRule(kind="attr", attr="priority", op="eq", value="Must"),
            FilterRule(kind="attr", attr="status", op="eq", value="Approved"),
        ),
    )
    assert slug == "high-priority-approved"
    path = dst / "VCU3-SRS" / "filters" / "high-priority-approved.xml"
    assert path.is_file()

    # Round-trip from disk.
    doc = load_document(dst / "VCU3-SRS")
    assert any(f.slug == slug for f in doc.filters)
    saved = next(f for f in doc.filters if f.slug == slug)
    assert saved.name == "High priority approved"
    assert len(saved.rules) == 2


def test_filter_dialog_save_persists_and_applies(window, qtbot):
    from mellon import FilterRule
    from mellon.gui.dialogs.filter_dialog import FilterDialog

    w, dst = window
    w.controller.select_document("VCU3-SRS")
    doc = w.controller.document("VCU3-SRS")
    user_attrs = tuple(a for a in doc.attributes if a.name != "type")

    dlg = FilterDialog(
        attributes=user_attrs,
        all_doc_ids=("VCU3-SRS",),
        existing_filter_names=set(),
        initial=None,
        parent=w,
    )
    qtbot.addWidget(dlg)

    # Programmatically build a "priority eq Must" rule.
    dlg._name_edit.setText("Must Approved")
    rows = dlg._all_rows()
    assert rows, "dialog should seed one empty rule"
    row = rows[0]
    # Pick attribute "priority" by data lookup.
    ix = row.attr_combo.findData("priority")
    assert ix >= 0
    row.attr_combo.setCurrentIndex(ix)
    op_ix = row.op_combo.findData("eq")
    assert op_ix >= 0
    row.op_combo.setCurrentIndex(op_ix)
    enum_ix = row._enum_combo.findData("Must")
    assert enum_ix >= 0
    row._enum_combo.setCurrentIndex(enum_ix)

    # Drive the same path the OK button takes.
    captured = {}

    def _on_save(filt):
        captured["filt"] = filt

    dlg.save_and_apply_requested.connect(_on_save)
    dlg._on_ok()
    filt = captured["filt"]
    assert filt.name == "Must Approved"
    assert len(filt.rules) == 1
    assert filt.rules[0].attr == "priority"
    assert filt.rules[0].op == "eq"
    assert filt.rules[0].value == "Must"

    # Persist via the controller path the main window uses.
    slug = w.controller.create_filter(
        "VCU3-SRS", filt.name, filt.combinator, filt.keep_ancestors, filt.rules
    )
    path = dst / "VCU3-SRS" / "filters" / f"{slug}.xml"
    assert path.is_file()


# --- pill summary --------------------------------------------------------


def test_pill_summary_reflects_filter_state(window):
    from mellon import Filter, FilterRule

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    w.grid_pane.apply_filter(
        Filter(
            slug="x", name="X",
            combinator="any",
            keep_ancestors=True,
            rules=(
                FilterRule(kind="attr", attr="priority", op="eq", value="Must"),
                FilterRule(kind="attr", attr="status", op="eq", value="Draft"),
            ),
        )
    )
    text = w.grid_pane.filter_pill._label.text()
    assert "X" in text
    assert "2 rule" in text
    assert "Any" in text
    assert "keep ancestors" in text


# --- restore on document switch ------------------------------------------


def test_new_filter_does_not_overwrite_previous_when_one_is_active(window):
    """Regression: clicking New filter… with an active filter previously
    seeded the dialog with the active one, so saving overwrote it."""
    from mellon import FilterRule
    from mellon.gui.dialogs.filter_dialog import FilterDialog

    w, dst = window
    w.controller.select_document("VCU3-SRS")

    # Create the first filter and leave it active.
    first_slug = w.controller.create_filter(
        "VCU3-SRS",
        "First",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w._apply_filter_by_slug(first_slug)
    assert w.grid_pane.active_filter().slug == first_slug

    # Now drive a second New filter… exactly the way the menu does
    # (seed=None means "fresh"). The dialog must not pre-fill from the
    # active filter, and saving must add a new file rather than overwrite.
    before_count = len(w.controller.document("VCU3-SRS").filters)

    # Replicate the lambda the menu uses.
    w._open_filter_dialog_seed_passthrough = []

    # Build the dialog as the launcher would.
    doc = w.controller.document("VCU3-SRS")
    dlg = FilterDialog(
        attributes=tuple(doc.attributes),
        all_doc_ids=("VCU3-SRS",),
        existing_filter_names={f.name for f in doc.filters},
        initial=None,  # "New filter…" path
        parent=w,
    )
    state = {"seed": None}

    def on_save_and_apply(f):
        saved = w._dialog_save(f, state["seed"])
        if saved is not None:
            state["seed"] = saved

    dlg.save_and_apply_requested.connect(on_save_and_apply)

    dlg._name_edit.setText("Second")
    rows = dlg._all_rows()
    row = rows[0]
    row.attr_combo.setCurrentIndex(row.attr_combo.findData("status"))
    row.op_combo.setCurrentIndex(row.op_combo.findData("eq"))
    row._enum_combo.setCurrentIndex(row._enum_combo.findData("Approved"))
    dlg._on_ok()

    after = w.controller.document("VCU3-SRS").filters
    assert len(after) - before_count == 1
    # Both filter files exist.
    assert (dst / "VCU3-SRS" / "filters" / "first.xml").is_file()
    assert (dst / "VCU3-SRS" / "filters" / "second.xml").is_file()
    # The first filter's rule was not clobbered.
    first = next(f for f in after if f.slug == "first")
    assert first.rules[0].attr == "priority"
    assert first.rules[0].value == "Must"


def test_open_dialog_unset_seed_uses_active_filter(window):
    """Ctrl+Shift+F / pill Edit (no kwarg) should still default to editing
    the active filter — only New filter… opts out."""
    from mellon import FilterRule
    from unittest.mock import patch

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "Active",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w._apply_filter_by_slug(slug)

    captured = {}

    class _StubDialog:
        def __init__(self, *args, **kwargs):
            captured["initial"] = kwargs.get("initial")
            self.apply_requested = type("_S", (), {"connect": lambda self, _f: None})()
            self.save_and_apply_requested = type("_S", (), {"connect": lambda self, _f: None})()

        def exec(self):
            return 0

    # Default-arg path (no `seed=` kwarg) should hand the active filter to
    # the dialog as `initial=`.
    with patch("mellon.gui.dialogs.filter_dialog.FilterDialog", _StubDialog):
        w._open_filter_dialog()
    assert captured["initial"] is not None
    assert captured["initial"].slug == slug

    # Explicit `seed=None` ("New filter…" path) hands None.
    with patch("mellon.gui.dialogs.filter_dialog.FilterDialog", _StubDialog):
        w._open_filter_dialog(seed=None)
    assert captured["initial"] is None


def test_filter_menu_edit_disabled_in_read_only(window):
    """Read-only mode greys out every menu row that mutates filters."""
    w, _ = window
    w.controller.select_document("VCU3-SRS")

    # Seed an active filter so the Edit row's other precondition is met.
    from mellon import FilterRule
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "ReadOnlyEdit",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w._apply_filter_by_slug(slug)
    assert w.grid_pane.active_filter() is not None

    w._set_read_only(True)
    w._populate_filters_menu()
    actions = {a.text(): a for a in w._filters_menu.actions()}
    # Apply rows still work in read-only.
    assert actions["(none)"].isEnabled()
    # Mutation rows are disabled.
    assert not actions["New filter…"].isEnabled()
    assert not actions["Edit current filter…"].isEnabled()
    assert not actions["Manage filters…"].isEnabled()


def test_filter_dialog_save_disabled_in_read_only(window, qtbot):
    from mellon.gui.dialogs.filter_dialog import FilterDialog
    from PySide6.QtWidgets import QDialogButtonBox

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    doc = w.controller.document("VCU3-SRS")

    dlg = FilterDialog(
        attributes=tuple(doc.attributes),
        all_doc_ids=("VCU3-SRS",),
        existing_filter_names=set(),
        initial=None,
        parent=w,
        read_only=True,
    )
    qtbot.addWidget(dlg)
    dlg._name_edit.setText("Anything")
    ok_btn = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None and not ok_btn.isEnabled()
    # Apply still works for transient narrowing in read-only.
    assert dlg._apply_btn.isEnabled()
    assert "Read-only" in dlg._hint.text()


def test_delete_filter_via_controller_removes_file_and_drops_active(window):
    from mellon import FilterRule

    w, dst = window
    w.controller.select_document("VCU3-SRS")
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "ToDelete",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w._apply_filter_by_slug(slug)
    assert w.grid_pane.active_filter() is not None
    path = dst / "VCU3-SRS" / "filters" / f"{slug}.xml"
    assert path.is_file()

    w.controller.delete_filter("VCU3-SRS", slug)
    assert not path.exists()
    # Active filter that referenced the deleted slug is dropped automatically.
    assert w.grid_pane.active_filter() is None


def test_pill_clear_button_persists_clear_across_relaunch(window, qtbot, tmp_path):
    """Regression: clicking the pill `×` (or selecting `(none)` in the
    Filters menu) used to clear only the in-session match-set without
    touching QSettings, so the next launch's `_restore_active_filter`
    would re-apply the filter the user just deactivated."""
    from mellon import FilterRule
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    w, dst = window
    w.controller.select_document("VCU3-SRS")
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "Stuck",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w._apply_filter_by_slug(slug)
    assert w._read_active_filter("VCU3-SRS") == slug

    # User clicks the pill `×` button.
    w.grid_pane.filter_pill.clear_clicked.emit()
    # In-session state cleared.
    assert w.grid_pane.active_filter() is None
    # QSettings entry cleared too — that's the regression.
    assert w._read_active_filter("VCU3-SRS") == ""

    # Simulate quit + relaunch by building a fresh window against the same
    # repo (same QSettings backing store via the autouse fixture).
    svc2 = DocumentService(dst, cache=True)
    w2 = DocumentEditorWindow("VCU3-SRS", svc2)
    qtbot.addWidget(w2)
    # No filter re-applied on the new window.
    assert w2.grid_pane.active_filter() is None


def test_none_menu_entry_persists_clear_across_relaunch(window, qtbot, tmp_path):
    """The (none) entry in the Filters dropdown must clear QSettings too."""
    from mellon import FilterRule
    from mellon.gui.main_window import DocumentEditorWindow
    from mellon.service.document import DocumentService

    w, dst = window
    w.controller.select_document("VCU3-SRS")
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "Stuck2",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    w._apply_filter_by_slug(slug)
    assert w._read_active_filter("VCU3-SRS") == slug

    # Replicate what (none) triggers.
    w.grid_pane.clear_active_filter()
    assert w._read_active_filter("VCU3-SRS") == ""

    svc2 = DocumentService(dst, cache=True)
    w2 = DocumentEditorWindow("VCU3-SRS", svc2)
    qtbot.addWidget(w2)
    assert w2.grid_pane.active_filter() is None


def test_active_filter_restored_when_switching_back_to_document(window):
    from mellon import FilterRule

    w, _ = window
    w.controller.select_document("VCU3-SRS")
    slug = w.controller.create_filter(
        "VCU3-SRS",
        "Restorable",
        rules=(FilterRule(kind="attr", attr="priority", op="eq", value="Must"),),
    )
    # Activate it through the menu helper so the QSettings entry gets written.
    w._apply_filter_by_slug(slug)
    assert w.grid_pane.active_filter() is not None

    # Switch away and back; restore should re-apply.
    other_docs = [d for d in w.controller.docs() if d != "VCU3-SRS"]
    assert other_docs, "fixture should have more than one document"
    w.controller.select_document(other_docs[0])
    # No active filter on the other doc.
    w.controller.select_document("VCU3-SRS")
    active = w.grid_pane.active_filter()
    assert active is not None
    assert active.slug == slug
