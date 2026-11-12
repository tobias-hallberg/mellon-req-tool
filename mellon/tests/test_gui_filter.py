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


# MELLON-EDIT-SRS blind tests (176 and up)
import pygit2  # noqa: E402

from tests.edit_b_support import (  # noqa: E402
    _KIND_OPS,
    MessageBoxes,
    _attr_combos,
    _attr_headers,
    _choose,
    _eq,
    _filter_files,
    _filters_repo,
    _inspector_field,
    _name_field,
    _operator_combo,
    _row_combos,
    _views_menu_choose,
    answer_text,
    button,
    changed_files,
    drive_dialogs,
    git_commit_all,
    git_init,
    grid_ids,
    menu_action,
    menu_texts,
    open_editor,
    select,
    tree_hashes,
    write_doc,
)


def _document_combo(attr_combo):
    for c in _row_combos(attr_combo):
        if any(c.itemText(i) == "(any document)" for i in range(c.count())):
            return c
    raise AssertionError("no document chooser in the condition row")


def _pill_text(window) -> str:
    from PySide6.QtWidgets import QLabel

    pill = window.grid_pane.filter_pill
    if not pill.isVisible():
        return ""
    return " ".join(l.text() for l in pill.findChildren(QLabel))


@pytest.mark.parametrize("attr,kind", [
    ("status", "enum"), ("rationale", "text"), ("count", "integer"),
    ("weight", "real"), ("flag", "boolean"), ("due", "date")])
def test_edit_srs_282_advanced_filter_dialog_offers_only_fitting_operators(
        tmp_path, qtbot, monkeypatch, attr, kind):
    """MELLON-EDIT-SRS-282: the Advanced filter dialog offers for an attribute
    only the operators that fit its kind (the sets of MELLON-EDIT-SRS-292 to
    -296), and each of them."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    offered = {}

    def handler(dialog):
        combo = _attr_combos(dialog)[0]
        _choose(combo, attr)
        qtbot.wait(5)
        op = _operator_combo(combo)
        offered["ops"] = [op.itemData(i) for i in range(op.count())]

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "New filter").trigger()
    qtbot.wait(10)
    ops = offered["ops"]
    assert len(ops) == len(set(ops)), f"repeated operator: {ops}"
    assert set(ops) == _KIND_OPS[kind]


def test_edit_srs_283_advanced_filter_dialog_builds_link_conditions(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-283: the Advanced filter dialog offers conditions on
    outgoing links to a document and incoming links from a document. Build
    has-outgoing present to P and has-incoming absent from any document and
    save: the saved filter holds the two conditions."""
    from mellon.loader import load_document

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")

    def handler(dialog):
        _name_field(dialog).setText("Links")
        first = _attr_combos(dialog)[0]
        _choose(first, "__link_out__")
        _choose(_operator_combo(first), "present")
        _choose(_document_combo(first), "P")
        button(dialog, "+ Add rule").click()
        qtbot.wait(5)
        second = _attr_combos(dialog)[-1]
        assert second is not first, "no second condition row"
        _choose(second, "__link_in__")
        _choose(_operator_combo(second), "absent")
        _choose(_document_combo(second), "")
        button(dialog, "OK").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "New filter").trigger()
    qtbot.wait(10)
    filters = load_document(root / "D").filters
    assert [f.name for f in filters] == ["Links"]
    rules = sorted((r.kind, r.op, r.attr) for r in filters[0].rules)
    assert rules == [("has-incoming", "absent", ""), ("has-outgoing", "present", "P")]


def test_edit_srs_287_new_filter_starts_and_saves_with_ancestors_kept(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-287: New filter... starts the dialog with ancestor objects
    kept; adding a condition and saving without touching the setting saves a
    filter that keeps them."""
    from PySide6.QtWidgets import QCheckBox

    from mellon.loader import load_document

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    start = {}

    def handler(dialog):
        boxes = [b for b in dialog.findChildren(QCheckBox)
                 if "ancestor" in b.text().lower()]
        assert len(boxes) == 1, "no keep-ancestors setting"
        start["kept"] = boxes[0].isChecked()
        _name_field(dialog).setText("Musts")
        combo = _attr_combos(dialog)[0]
        _choose(combo, "priority")
        _choose(_operator_combo(combo), "eq")
        button(dialog, "OK").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "New filter").trigger()
    qtbot.wait(10)
    assert start["kept"] is True
    filters = load_document(root / "D").filters
    assert [f.name for f in filters] == ["Musts"]
    assert filters[0].keep_ancestors is True


@pytest.mark.parametrize("name", ["", "   "], ids=["empty", "spaces"])
def test_edit_srs_278_advanced_filter_dialog_refuses_filter_without_name(
        tmp_path, qtbot, monkeypatch, name):
    """MELLON-EDIT-SRS-278: a filter with no name (empty, or only spaces) is
    not saved from the Advanced filter dialog: no filter file is written."""
    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")

    def handler(dialog):
        _name_field(dialog).setText(name)
        combo = _attr_combos(dialog)[0]
        _choose(combo, "status")
        _choose(_operator_combo(combo), "is-empty")
        ok = button(dialog, "OK")
        if ok.isEnabled():
            ok.click()
        qtbot.wait(5)
        if dialog.isVisible():
            dialog.reject()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "New filter").trigger()
    qtbot.wait(10)
    assert _filter_files(root) == {}


def test_edit_srs_288_manage_filters_renames_filter(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-288: from the Document Editor's Manage filters...,
    rename Approved to Reviewed: the Filters menu lists Reviewed and not
    Approved, and the filter keeps its conditions."""
    from PySide6.QtWidgets import QListWidget

    from mellon.loader import load_document

    root = _filters_repo(tmp_path, filters=[("Approved", [_eq("status", "Approved")])])
    MessageBoxes(monkeypatch)
    answer_text(monkeypatch, "Reviewed")
    window = open_editor(qtbot, root, "D")

    def handler(dialog):
        lst = dialog.findChildren(QListWidget)[0]
        lst.setCurrentRow(0)
        button(dialog, "Rename").click()
        button(dialog, "Close").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "Manage filters").trigger()
    qtbot.wait(10)
    names = menu_texts(window, "Filters")
    assert "Reviewed" in names and "Approved" not in names
    (filt,) = load_document(root / "D").filters
    assert filt.name == "Reviewed"
    assert [(r.attr, r.op, r.value) for r in filt.rules] == [("status", "eq", "Approved")]


def test_edit_srs_289_manage_filters_deletes_filter(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-289: from Manage filters..., delete a filter (confirming):
    its filter file is gone and the Filters menu no longer lists it."""
    from PySide6.QtWidgets import QListWidget

    root = _filters_repo(tmp_path, filters=[("Approved", [_eq("status", "Approved")]),
                                            ("Musts", [_eq("priority", "Must")])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")

    def handler(dialog):
        lst = dialog.findChildren(QListWidget)[0]
        row = [lst.item(i).text() for i in range(lst.count())].index("Approved")
        lst.setCurrentRow(row)
        button(dialog, "Delete").click()
        button(dialog, "Close").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "Manage filters").trigger()
    qtbot.wait(10)
    assert list(_filter_files(root)) == ["musts.xml"]
    names = menu_texts(window, "Filters")
    assert "Approved" not in names and "Musts" in names


def test_edit_srs_313_filters_menu_lists_filters_and_activates_choice(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-313: the Filters menu lists Alpha and Beta by name;
    choosing Beta makes Beta the active filter: its name shows above the grid
    and only its rows show."""
    root = _filters_repo(tmp_path, filters=[
        ("Alpha", [_eq("status", "Draft")]),
        ("Beta", [_eq("status", "Approved")])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    names = menu_texts(window, "Filters")
    assert "Alpha" in names and "Beta" in names
    menu_action(window, "Filters", "Beta").trigger()
    qtbot.wait(10)
    active = window.grid_pane.active_filter()
    assert active is not None and active.name == "Beta"
    assert "Beta" in _pill_text(window)
    # Beta matches D-2 and D-4; ancestors kept: D-0 and D-3.
    assert grid_ids(window) == ["D-0", "D-2", "D-3", "D-4"]


def test_edit_srs_314_active_filter_shows_matches_and_their_ancestors(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-314: with priority eq Must and ancestor objects kept,
    the grid shows exactly the objects with priority Must (D-1, D-4) and
    each of their ancestor objects (D-0, D-3), in outline order."""
    from mellon.schema import Filter

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    window.grid_pane.apply_filter(Filter(slug="musts", name="Musts",
                                         keep_ancestors=True,
                                         rules=(_eq("priority", "Must"),)))
    qtbot.wait(10)
    assert grid_ids(window) == ["D-0", "D-1", "D-3", "D-4"]


def test_edit_srs_316_clearing_active_filter_keeps_the_filter(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-316: make a saved filter active and clear it with the
    clear control above the grid: the filter file's bytes are unchanged and
    the Filters menu still lists the filter."""
    from PySide6.QtWidgets import QAbstractButton

    root = _filters_repo(tmp_path, filters=[("Alpha", [_eq("status", "Draft")])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    before = _filter_files(root)
    menu_action(window, "Filters", "Alpha").trigger()
    qtbot.wait(10)
    assert window.grid_pane.active_filter() is not None
    clear = [b for b in window.grid_pane.filter_pill.findChildren(QAbstractButton)
             if b.text() in ("×", "x", "X") or "deactivate" in b.toolTip().lower()]
    assert clear, "no clear control with the active filter's name"
    clear[0].click()
    qtbot.wait(10)
    assert window.grid_pane.active_filter() is None
    assert len(grid_ids(window)) == 6
    assert _filter_files(root) == before
    assert "Alpha" in menu_texts(window, "Filters")


def test_edit_srs_317_reopened_editor_restores_active_filter(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-317: a filter active when the editor closed is active
    again in a new Document Editor on the document; once its file is deleted
    outside Mellon, reopening makes no filter active."""
    root = _filters_repo(tmp_path, filters=[("Alpha", [_eq("status", "Draft")])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    menu_action(window, "Filters", "Alpha").trigger()
    qtbot.wait(10)
    window.close()
    qtbot.wait(10)
    again = open_editor(qtbot, root, "D")
    active = again.grid_pane.active_filter()
    assert active is not None and active.name == "Alpha"
    again.close()
    qtbot.wait(10)
    (root / "D" / "filters" / "alpha.xml").unlink()
    third = open_editor(qtbot, root, "D")
    assert third.grid_pane.active_filter() is None
    assert len(grid_ids(third)) == 6


def test_edit_srs_318_active_filter_kept_outside_repository_per_user(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-318: making a filter active changes no file of the
    repository (git status stays clean); another user's settings store,
    opening the same document, has no filter active."""
    from PySide6.QtCore import QCoreApplication, QSettings

    root = _filters_repo(tmp_path, filters=[("Alpha", [_eq("status", "Draft")])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    work_before = {k: v for k, v in tree_hashes(root).items()
                   if not k.startswith(".git/")}
    menu_action(window, "Filters", "Alpha").trigger()
    qtbot.wait(10)
    window.close()
    qtbot.wait(10)
    assert pygit2.Repository(str(root)).status() == {}
    work_after = {k: v for k, v in tree_hashes(root).items()
                  if not k.startswith(".git/")}
    assert changed_files(work_before, work_after) == []
    # Another user: a separate, empty settings store.
    other = tmp_path / "other-user-settings"
    other.mkdir()
    for fmt in (QSettings.IniFormat, QSettings.NativeFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(other))
    QCoreApplication.setOrganizationName("blind-other-user-org")
    QCoreApplication.setApplicationName("blind-other-user-app")
    elsewhere = open_editor(qtbot, root, "D")
    assert elsewhere.grid_pane.active_filter() is None


def test_edit_srs_319_edit_shows_row_that_now_matches_active_filter(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-319: with status eq Draft active, set the hidden
    Approved requirement D-2 to Draft in the Inspector: its row shows at
    once."""
    from mellon.schema import Filter

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    window.grid_pane.apply_filter(Filter(slug="drafts", name="Drafts",
                                         keep_ancestors=False,
                                         rules=(_eq("status", "Draft"),)))
    qtbot.wait(10)
    assert grid_ids(window) == ["D-1", "D-5"]
    select(window, "D-2", qtbot)
    combo = _inspector_field(window, "status")
    _choose(combo, "Draft") if combo.findData("Draft") >= 0 else \
        combo.setCurrentIndex(combo.findText("Draft"))
    qtbot.wait(10)
    from mellon.loader import load_document
    assert load_document(root / "D").objects["D-2"].attrs["status"] == "Draft"
    assert grid_ids(window) == ["D-1", "D-2", "D-5"]


def test_edit_srs_319_edit_hides_row_that_stops_matching(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-319, other direction: with status eq Draft active,
    setting the shown D-1 to Approved in the Inspector hides its row at
    once."""
    from mellon.schema import Filter

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    window.grid_pane.apply_filter(Filter(slug="drafts", name="Drafts",
                                         keep_ancestors=False,
                                         rules=(_eq("status", "Draft"),)))
    qtbot.wait(10)
    select(window, "D-1", qtbot)
    combo = _inspector_field(window, "status")
    combo.setCurrentIndex(combo.findText("Approved"))
    qtbot.wait(10)
    assert grid_ids(window) == ["D-5"]


def test_edit_srs_321_no_match_says_so_and_offers_to_clear(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-321: with an active filter no object meets, the grid
    says that no object matches and offers to clear the filters; its clear
    control clears the active filter and every row shows again."""
    from PySide6.QtWidgets import QLabel

    from mellon.schema import Filter

    root = _filters_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    window.grid_pane.apply_filter(Filter(slug="none", name="Nobody",
                                         keep_ancestors=True,
                                         rules=(_eq("count", "999"),)))
    qtbot.wait(10)
    assert grid_ids(window) == []
    said = [l.text() for l in window.grid_pane.findChildren(QLabel)
            if l.isVisible() and "match" in l.text().lower()
            and "filter:" not in l.text().lower()]
    assert said, "the grid doesn't say that no object matches"
    clear = button(window.grid_pane, "Clear filters")
    assert clear.isVisible()
    clear.click()
    qtbot.wait(10)
    assert window.grid_pane.active_filter() is None
    assert len(grid_ids(window)) == 6


def test_edit_srs_322_changing_view_keeps_active_filter(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-322: with filter F active, choose view V1, then V2,
    then no view: F stays active and the shown rows stay the same."""
    root = _filters_repo(tmp_path, filters=[("F", [_eq("status", "Draft")])],
                         views=[("V1", ["status"]), ("V2", ["priority", "rationale"])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    menu_action(window, "Filters", "F").trigger()
    qtbot.wait(10)
    rows = grid_ids(window)
    assert rows == ["D-0", "D-1", "D-5"]
    for view in ("V1", "V2"):
        _views_menu_choose(window, view)
        qtbot.wait(10)
        assert window.grid_pane.active_filter().name == "F"
        assert grid_ids(window) == rows
    window.grid_pane.apply_view(None, [])
    qtbot.wait(10)
    assert window.grid_pane.active_filter().name == "F"
    assert grid_ids(window) == rows


def test_edit_srs_323_changing_active_filter_keeps_shown_columns(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-323: with view V active, choose filter F1, then F2,
    then clear the filter: the shown attribute columns and their order are
    V's each time."""
    root = _filters_repo(tmp_path, filters=[("F1", [_eq("status", "Draft")]),
                                            ("F2", [_eq("priority", "Must")])],
                         views=[("V", ["priority", "status"])])
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "D")
    _views_menu_choose(window, "V")
    qtbot.wait(10)
    columns = _attr_headers(window)
    assert columns == ["priority", "status"]
    for name in ("F1", "F2"):
        menu_action(window, "Filters", name).trigger()
        qtbot.wait(10)
        assert _attr_headers(window) == columns
    menu_action(window, "Filters", "(none)").trigger()
    qtbot.wait(10)
    assert window.grid_pane.active_filter() is None
    assert _attr_headers(window) == columns


def _narrow_repo(tmp_path: Path) -> Path:
    """N (parent P), flat. Filter box 'key', filter status eq Draft and
    Suspects only (a link without an acknowledged commit is suspect):
      N-0 key,   Draft,    link   -> passes all three
      N-1 key,   Draft,    -      -> fails Suspects only
      N-2 key,   Approved, link   -> fails the filter
      N-3 other, Draft,    link   -> fails the filter box
      N-4 other, Approved, -      -> fails all three"""
    root = tmp_path / "repo"
    root.mkdir()
    write_doc(root, "P", objects={"P-0": ({"type": "Requirement"}, "parent")},
              next_number=1)
    write_doc(root, "N", objects={
        "N-0": ({"type": "Requirement", "status": "Draft"}, "key one"),
        "N-1": ({"type": "Requirement", "status": "Draft"}, "key two"),
        "N-2": ({"type": "Requirement", "status": "Approved"}, "key three"),
        "N-3": ({"type": "Requirement", "status": "Draft"}, "other four"),
        "N-4": ({"type": "Requirement", "status": "Approved"}, "other five"),
    }, links={s: [("P", "P-0", "satisfies")] for s in ("N-0", "N-2", "N-3")},
        parents=["P"], next_number=5)
    git_init(root)
    git_commit_all(root, "seed")
    return root


def _set_box(window, qtbot, text: str) -> None:
    box = window.grid_pane.filter_box
    box.clear()
    qtbot.keyClicks(box, text)
    qtbot.wait(400)  # let a typing delay, if any, run out


def _set_suspects_only(window, qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    btn = [b for b in window.grid_pane.findChildren(QPushButton)
           if b.text() == "Suspects only"]
    if btn and btn[0].isVisible() and btn[0].isCheckable():
        if not btn[0].isChecked():
            btn[0].click()
    else:
        window.grid_pane.set_suspect_only(True)
    qtbot.wait(10)
    assert window.grid_pane.is_suspect_only()


def test_edit_srs_274_grid_shows_objects_passing_all_three_narrowings(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-274: the grid shows only the objects that the filter
    box, the active filter and Suspects only each let through, the same
    whichever order they were set in: exactly N-0."""
    from mellon.schema import Filter

    root = _narrow_repo(tmp_path)
    MessageBoxes(monkeypatch)
    drafts = Filter(slug="drafts", name="Drafts", keep_ancestors=False,
                    rules=(_eq("status", "Draft"),))
    window = open_editor(qtbot, root, "N")
    _set_box(window, qtbot, "key")
    window.grid_pane.apply_filter(drafts)
    qtbot.wait(10)
    _set_suspects_only(window, qtbot)
    assert grid_ids(window) == ["N-0"]
    window.close()
    other = open_editor(qtbot, root, "N")
    other.grid_pane.clear_all_filters()
    _set_suspects_only(other, qtbot)
    other.grid_pane.apply_filter(drafts)
    qtbot.wait(10)
    _set_box(other, qtbot, "key")
    assert grid_ids(other) == ["N-0"]


def test_edit_srs_274_each_narrowing_alone(tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-274 control: each narrowing alone lets through the
    objects worked out for it, so each takes part in the combined result."""
    from mellon.schema import Filter

    root = _narrow_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "N")
    _set_box(window, qtbot, "key")
    assert grid_ids(window) == ["N-0", "N-1", "N-2"]
    _set_box(window, qtbot, "")
    window.grid_pane.apply_filter(Filter(slug="drafts", name="Drafts",
                                         keep_ancestors=False,
                                         rules=(_eq("status", "Draft"),)))
    qtbot.wait(10)
    assert grid_ids(window) == ["N-0", "N-1", "N-3"]
    window.grid_pane.clear_active_filter()
    _set_suspects_only(window, qtbot)
    assert grid_ids(window) == ["N-0", "N-2", "N-3"]


def _incoming_repo(tmp_path: Path) -> Path:
    """SRS-1 is the target of a link from STS-1; nothing links to SRS-2;
    OTHER links only within itself."""
    root = tmp_path / "repo"
    root.mkdir()
    write_doc(root, "SRS", objects={
        "SRS-1": ({"type": "Requirement"}, "verified"),
        "SRS-2": ({"type": "Requirement"}, "not verified"),
    }, next_number=3)
    write_doc(root, "STS", objects={
        "STS-1": ({"type": "Requirement"}, "test"),
    }, links={"STS-1": [("SRS", "SRS-1", "verifies")]}, parents=["SRS"],
        next_number=2)
    write_doc(root, "OTHER", objects={
        "OTHER-1": ({"type": "Requirement"}, "a"),
        "OTHER-2": ({"type": "Requirement"}, "b"),
    }, links={"OTHER-1": [("OTHER", "OTHER-2", "refines")]}, next_number=3)
    git_init(root)
    git_commit_all(root, "seed")
    return root


@pytest.mark.parametrize("source,expected", [
    ("STS", ["SRS-1"]), ("OTHER", []), ("", ["SRS-1"])],
    ids=["from-STS", "from-OTHER", "from-any"])
def test_edit_srs_309_has_incoming_present_reads_links_from_repository(
        tmp_path, qtbot, monkeypatch, source, expected):
    """MELLON-EDIT-SRS-309: has-incoming present naming STS matches SRS-1
    (STS-1 links to it); naming OTHER matches nothing; naming no document
    matches SRS-1. The links are read from the repository by the Document
    Editor, not given by a stand-in."""
    from mellon.schema import Filter, FilterRule

    root = _incoming_repo(tmp_path)
    MessageBoxes(monkeypatch)
    window = open_editor(qtbot, root, "SRS")
    window.grid_pane.apply_filter(Filter(
        slug="verified", name="Verified", keep_ancestors=False,
        rules=(FilterRule(kind="has-incoming", attr=source, op="present"),)))
    qtbot.wait(10)
    assert grid_ids(window) == expected


def test_edit_srs_278_manage_filters_refuses_rename_to_empty_name(
        tmp_path, qtbot, monkeypatch):
    """MELLON-EDIT-SRS-278 from the Document Editor: renaming a filter to an
    empty name in Manage filters... saves nothing; the filter file is
    unchanged and the Filters menu still lists the filter."""
    from PySide6.QtWidgets import QListWidget

    root = _filters_repo(tmp_path, filters=[("Approved", [_eq("status", "Approved")])])
    MessageBoxes(monkeypatch)
    answer_text(monkeypatch, "")
    window = open_editor(qtbot, root, "D")
    before = _filter_files(root)

    def handler(dialog):
        lst = dialog.findChildren(QListWidget)[0]
        lst.setCurrentRow(0)
        button(dialog, "Rename").click()
        button(dialog, "Close").click()

    drive_dialogs(monkeypatch, qtbot, handler)
    menu_action(window, "Filters", "Manage filters").trigger()
    qtbot.wait(10)
    assert _filter_files(root) == before
    assert "Approved" in menu_texts(window, "Filters")
