"""Center pane: objects grid + filter box.

The grid is a `QTableView` over `ObjectsGridModel`. A `QSortFilterProxyModel`
wraps it so the filter box can narrow rows by a substring match against id or
heading text.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ... import drawio
from ...service import ServiceError
from .. import icons
from .. import settings as _settings_mod
from ..controller import RepoController
from ..dialogs.figure_view import FigureViewDialog
from ..delegates import (
    AttrDelegate,
    IdColumnDelegate,
    MultiLineTextDelegate,
    StateGutterDelegate,
)
from ..models.objects_grid import (
    OBJ_FIGURE_PATH_ROLE,
    OBJ_STATE_ROLE,
    ObjectsGridModel,
)
from ..theme import ThemeManager
from ...schema import Table, TableCell, TableColumn, TableRow


def drawio_action_text(figure_path: str | None) -> tuple[str, str | None]:
    """Label for the draw.io figure action, and why it's unavailable.

    The second item is None when the action can run: there's no figure yet
    ("New draw.io figure…") or the figure carries an editable draw.io
    diagram ("Edit in draw.io…"). A plain SVG can't be edited in draw.io.
    """
    if not figure_path:
        return "New draw.io figure…", None
    if not drawio.is_drawio_file(Path(figure_path)):
        return (
            "Edit in draw.io…",
            "This figure wasn't made with draw.io, so draw.io can't edit it. "
            "Replace it with a draw.io figure to edit it here.",
        )
    return "Edit in draw.io…", None


DRAWIO_NOT_FOUND_TEXT = (
    "Mellon couldn't find draw.io desktop.\n\n"
    f"Install it from {drawio.DOWNLOAD_URL} and try again. If it's installed "
    "somewhere unusual, set the MELLON_DRAWIO environment variable to the "
    "path of draw.io.exe."
)


class _ObjectsProxy(QSortFilterProxyModel):
    """Proxy that ANDs three filters: free-text substring, suspect-only
    toggle, and a precomputed match-set from the saved Filter dialog.

    QSortFilterProxyModel's built-in regex filter only ever accepts rows; we
    need to also *require* a non-OK state when the suspect-only toggle is on
    and *require* membership in the saved-filter match set when one is
    active, so we override filterAcceptsRow directly.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""
        self._suspect_only = False
        self._match_set: frozenset[str] | None = None

    def setTextFilter(self, text: str) -> None:
        self._needle = text.strip().lower()
        self.invalidate()

    def setSuspectOnly(self, on: bool) -> None:
        self._suspect_only = on
        self.invalidate()

    def suspectOnly(self) -> bool:
        return self._suspect_only

    def setMatchSet(self, ids: frozenset[str] | set[str] | None) -> None:
        """Restrict visible rows to objects whose id is in `ids`.

        Pass `None` to remove the restriction. Combined with the other
        filter axes via AND.
        """
        self._match_set = frozenset(ids) if ids is not None else None
        self.invalidate()

    def matchSet(self) -> frozenset[str] | None:
        return self._match_set

    def filterAcceptsRow(self, src_row: int, parent: QModelIndex) -> bool:
        model: ObjectsGridModel = self.sourceModel()  # type: ignore[assignment]

        if self._match_set is not None:
            obj_id = model.obj_id_at(src_row)
            if obj_id is None or obj_id not in self._match_set:
                return False

        if self._suspect_only:
            state_idx = model.index(src_row, 0, parent)
            state = state_idx.data(OBJ_STATE_ROLE)
            if state not in ("suspect", "broken"):
                return False

        if not self._needle:
            return True

        # Search id and description — that's the DOORS feel.
        for name in ("id", "heading_or_text"):
            col = model.column_index(name)
            if col < 0:
                continue
            val = model.index(src_row, col, parent).data()
            if val and self._needle in str(val).lower():
                return True
        return False


class _OutlineTableView(QTableView):
    """QTableView that treats Tab / Shift+Tab as indent / outdent requests.

    Tab would normally move the focus to the next cell; for a DOORS-style
    outline grid it's much more useful as an indent affordance, so we route
    it to MainWindow via two signals and consume the event. Cell-edit mode
    is an exception — Tab there belongs to the editor widget.
    """

    indent_requested = Signal()
    outdent_requested = Signal()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self.state() != QTableView.State.EditingState:
            key = event.key()
            mods = event.modifiers()
            if key == Qt.Key.Key_Backtab or (
                key == Qt.Key.Key_Tab and mods & Qt.KeyboardModifier.ShiftModifier
            ):
                self.outdent_requested.emit()
                event.accept()
                return
            if key == Qt.Key.Key_Tab:
                self.indent_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class SuspectBanner(QWidget):
    """Amber banner shown when the active doc has suspect or broken links.

    Hosts the document-scoped suspect controls: a checkable "Suspects only"
    toggle (narrows the grid to the rows that need attention) and "Ack
    selected" (acknowledges every suspect link on the currently-selected
    object).
    """

    suspect_only_toggled = Signal(bool)
    ack_selected_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Object name lets us scope the stylesheet to this widget (and
        # descendants we name) rather than cascading to every QWidget child
        # — otherwise the QPushButtons inherit the amber background and blend
        # into it on dark themes where the default button text is light.
        self.setObjectName("SuspectBanner")
        # Plain QWidget subclasses don't paint their stylesheet background
        # unless WA_StyledBackground is set — without this, `background:` in
        # the QSS is silently ignored and the window color shows through.
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._label = QLabel()
        self._label.setObjectName("SuspectBannerLabel")
        self._suspect_only_btn = QPushButton(
            icons.icon("suspect_filter"), "Suspects only"
        )
        self._suspect_only_btn.setCheckable(True)
        self._suspect_only_btn.setToolTip(
            "Show only rows with suspect or broken links"
        )
        self._suspect_only_btn.toggled.connect(self.suspect_only_toggled.emit)
        self._ack_selected_btn = QPushButton(icons.icon("ack"), "Ack selected")
        self._ack_selected_btn.setToolTip(
            "Acknowledge every suspect link on the selected object"
        )
        self._ack_selected_btn.clicked.connect(self.ack_selected_clicked.emit)
        self._ack_selected_btn.setEnabled(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._suspect_only_btn)
        layout.addWidget(self._ack_selected_btn)

        self._apply_theme_tokens(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme_tokens)
        self.setVisible(False)

    def set_suspect_only_checked(self, on: bool) -> None:
        """Reflect external changes to the suspect-only filter into the UI."""
        if self._suspect_only_btn.isChecked() != on:
            # Block the signal so we don't bounce back through the toggle slot.
            was_blocked = self._suspect_only_btn.blockSignals(True)
            self._suspect_only_btn.setChecked(on)
            self._suspect_only_btn.blockSignals(was_blocked)

    def set_ack_selected_enabled(self, on: bool) -> None:
        """Enable Ack selected only when the selection has suspect links."""
        self._ack_selected_btn.setEnabled(on)

    def _apply_theme_tokens(self, tokens: dict) -> None:
        # Token-driven styling: a soft amber surface (warn color) with the
        # current theme's primary text color. Works on both light + dark.
        self.setStyleSheet(
            f"QWidget#SuspectBanner {{ background: {tokens['warn_bg']}; "
            f"border: 1px solid {tokens['warn']}; border-radius: 3px; }}"
            f"QLabel#SuspectBannerLabel {{ background: transparent; "
            f"border: none; color: {tokens['fg_0']}; font-size: 12px; }}"
        )
        self._suspect_only_btn.setIcon(icons.icon("suspect_filter"))
        self._ack_selected_btn.setIcon(icons.icon("ack"))

    def set_count(self, n: int) -> None:
        if n <= 0:
            self.setVisible(False)
            return
        self.setVisible(True)
        plural = "s" if n != 1 else ""
        self._label.setText(
            f"<b>{n} suspect link{plural}.</b>  Review the changes and acknowledge."
        )


class FilterPill(QWidget):
    """Compact pill summarising the saved Filter currently applied to the grid.

    Sits between the suspect banner and the free-text filter row. Hidden
    when no saved filter is active. Emits `edit_clicked` when the user wants
    to re-open the dialog and `clear_clicked` when they want to deactivate
    the saved filter (the on-disk file is preserved).
    """

    edit_clicked = Signal()
    clear_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FilterPill")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._label = QLabel()
        self._label.setObjectName("FilterPillLabel")
        self._edit_btn = QPushButton("Edit")
        self._edit_btn.setToolTip("Edit the active filter (Ctrl+Shift+F)")
        self._edit_btn.clicked.connect(self.edit_clicked.emit)
        self._clear_btn = QPushButton("×")
        self._clear_btn.setToolTip("Deactivate the saved filter (file is kept)")
        self._clear_btn.setFixedWidth(28)
        self._clear_btn.clicked.connect(self.clear_clicked.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(8)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._edit_btn)
        layout.addWidget(self._clear_btn)

        self._apply_theme_tokens(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme_tokens)
        self.setVisible(False)

    def _apply_theme_tokens(self, tokens: dict) -> None:
        # Cool-blue surface so it reads as informational, not as a warning.
        self.setStyleSheet(
            f"QWidget#FilterPill {{ background: {tokens['info_bg']}; "
            f"border: 1px solid {tokens['blue']}; border-radius: 3px; }}"
            f"QLabel#FilterPillLabel {{ background: transparent; "
            f"border: none; color: {tokens['fg_0']}; font-size: 12px; }}"
        )

    def show_filter(self, name: str, rule_count: int, combinator: str,
                    keep_ancestors: bool, match_count: int) -> None:
        combinator_label = "All" if combinator == "all" else "Any"
        plural = "s" if rule_count != 1 else ""
        anc = " · keep ancestors" if keep_ancestors else ""
        self._label.setText(
            f"<b>Filter: {name}</b>  ·  {rule_count} rule{plural} ({combinator_label})"
            f"{anc}  ·  {match_count} matching"
        )
        self.setVisible(True)

    def hide_filter(self) -> None:
        self.setVisible(False)


class StaleBanner(QWidget):
    """Amber banner shown when the document changed on disk under this window.

    Same shape and lifecycle as `SuspectBanner` / `ReadOnlyBanner`. Appears
    when another process (MCP server, sibling editor window) modified the
    document's folder — or a parent document's — since it was loaded. Hosts a
    single button that asks the window to reload from disk.
    """

    reload_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Same scope-stylesheet trick as SuspectBanner — see comments there.
        self.setObjectName("StaleBanner")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._label = QLabel()
        self._label.setObjectName("StaleBannerLabel")
        self._label.setText(
            "<b>Changed on disk.</b>  This document or one of its parents "
            "was modified outside this window."
        )
        self._reload_btn = QPushButton("Reload")
        self._reload_btn.setToolTip("Re-read the document from disk (F5)")
        self._reload_btn.clicked.connect(self.reload_clicked.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._reload_btn)

        self._apply_theme_tokens(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme_tokens)
        self.setVisible(False)

    def _apply_theme_tokens(self, tokens: dict) -> None:
        self.setStyleSheet(
            f"QWidget#StaleBanner {{ background: {tokens['warn_bg']}; "
            f"border: 1px solid {tokens['warn']}; border-radius: 3px; }}"
            f"QLabel#StaleBannerLabel {{ background: transparent; "
            f"border: none; color: {tokens['fg_0']}; font-size: 12px; }}"
        )


class ReadOnlyBanner(QWidget):
    """Blue banner shown while the document editor is in read-only mode.

    Complementary companion to `SuspectBanner`: same shape and lifecycle,
    different palette tokens (`info_bg` + `blue`). Hosts a single button
    that asks the window to leave read-only mode.
    """

    disable_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Same scope-stylesheet trick as SuspectBanner — see comments there.
        self.setObjectName("ReadOnlyBanner")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._label = QLabel()
        self._label.setObjectName("ReadOnlyBannerLabel")
        self._label.setText(
            "<b>Read-only mode.</b>  Editing is disabled."
        )
        self._disable_btn = QPushButton("Enable editing")
        self._disable_btn.setToolTip("Leave read-only mode and allow edits")
        self._disable_btn.clicked.connect(self.disable_clicked.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._disable_btn)

        self._apply_theme_tokens(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme_tokens)
        self.setVisible(False)

    def _apply_theme_tokens(self, tokens: dict) -> None:
        self.setStyleSheet(
            f"QWidget#ReadOnlyBanner {{ background: {tokens['info_bg']}; "
            f"border: 1px solid {tokens['blue']}; border-radius: 3px; }}"
            f"QLabel#ReadOnlyBannerLabel {{ background: transparent; "
            f"border: none; color: {tokens['fg_0']}; font-size: 12px; }}"
        )


_ACTIVE_VIEW_PREFIX = "grid/active_view"
_WIDTHS_PREFIX = "grid/widths"
_ORDER_PREFIX = "grid/order"
_DEFAULT_BUCKET = "__default"


def forget_view_storage(doc_id: str, slug: str) -> None:
    """Drop the QSettings entries that back a deleted view's GUI state.

    Removes the per-attribute widths under `grid/widths/<doc>/<slug>/...`,
    the per-bucket order under `grid/order/<doc>/<slug>` (only used for the
    default-bucket fallback today, but cleared for symmetry), and clears the
    active-view pointer if it still names this slug.
    """
    settings = _settings_mod.make_settings()
    settings.beginGroup(f"{_WIDTHS_PREFIX}/{doc_id}/{slug}")
    try:
        settings.remove("")
    finally:
        settings.endGroup()
    settings.remove(f"{_ORDER_PREFIX}/{doc_id}/{slug}")
    active_key = f"{_ACTIVE_VIEW_PREFIX}/{doc_id}"
    if str(settings.value(active_key, "")) == slug:
        settings.remove(active_key)


def migrate_view_storage(doc_id: str, old_slug: str, new_slug: str) -> None:
    """Move per-attribute widths from `old_slug` to `new_slug` and update active.

    No-op when `old_slug == new_slug`.
    """
    if old_slug == new_slug:
        return
    settings = _settings_mod.make_settings()
    src = f"{_WIDTHS_PREFIX}/{doc_id}/{old_slug}"
    settings.beginGroup(src)
    try:
        attrs = list(settings.childKeys())
        snapshot = {a: settings.value(a) for a in attrs}
    finally:
        settings.endGroup()
    settings.beginGroup(src)
    try:
        settings.remove("")
    finally:
        settings.endGroup()
    for attr, value in snapshot.items():
        settings.setValue(
            f"{_WIDTHS_PREFIX}/{doc_id}/{new_slug}/{attr}", value
        )
    active_key = f"{_ACTIVE_VIEW_PREFIX}/{doc_id}"
    if str(settings.value(active_key, "")) == old_slug:
        settings.setValue(active_key, new_slug)


class GridPane(QWidget):
    # Emitted when the empty-state primary button is clicked.
    create_first_object_requested = Signal()
    # Emitted when the "Clear filters" button in the filter-empty state is clicked.
    clear_filters_requested = Signal()
    # Emitted when the user clicks Edit on the filter pill — MainWindow opens
    # the filter dialog seeded with the active filter.
    filter_edit_requested = Signal()
    # Emitted whenever the saved filter on the grid changes — applied,
    # cleared, or replaced. Carries the new active Filter or None. MainWindow
    # uses this to keep the QSettings active-filter slug in sync without
    # every caller of apply_filter having to remember the persistence step.
    active_filter_changed = Signal(object)
    # Emitted when the inline table editor opens (carries the InlineTableWidget)
    # or closes (carries None). MainWindow tracks it so the toolbar "Table" menu
    # can drive the live editor.
    table_editor_changed = Signal(object)

    def __init__(self, controller: RepoController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._suspend_width_save = False
        self._suspend_order_save = False
        self._read_only = False
        # Banners and empty-state labels have wide single-line text whose
        # natural minimumSizeHint propagates up through the layout — without
        # this, the grid pane refuses to shrink below ~1000px and the splitter
        # handle won't move, which masquerades as broken pane-width persistence.
        self.setMinimumWidth(120)

        self._model = ObjectsGridModel(controller)
        self._proxy = _ObjectsProxy(self)
        self._proxy.setSourceModel(self._model)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter by id or text…  (Ctrl+F)")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._on_filter_changed)

        self.table = _OutlineTableView()
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        # Edit on double-click, single-click of an already-selected cell, F2,
        # or a selected-then-typed key.
        self.table.setEditTriggers(
            QTableView.EditTrigger.DoubleClicked
            | QTableView.EditTrigger.SelectedClicked
            | QTableView.EditTrigger.EditKeyPressed
            | QTableView.EditTrigger.AnyKeyPressed
        )
        # One persistent description-column delegate, reused across model
        # resets so its table-editor signals stay connected. Re-creating it in
        # `_install_delegates` would drop the connection on every doc switch.
        self._desc_delegate = MultiLineTextDelegate(self.table)
        self._desc_delegate.tableEditorOpened.connect(self._on_inline_editor_opened)
        self._desc_delegate.tableEditorClosed.connect(self._on_inline_editor_closed)
        self._install_delegates()
        self.table.verticalHeader().setVisible(False)
        # Description can contain multiple paragraphs; let each row grow to
        # fit its content (including visual wrap inside the description cell).
        # `Interactive` (not `ResizeToContents`) because the delegate needs to
        # grow the row manually as the user types — ResizeToContents ignores
        # setRowHeight. We explicitly refit rows on every model reset below.
        self.table.setWordWrap(True)
        # Description rows must show every line; never abbreviate with `…`.
        # The MultiLineTextDelegate's sizeHint sizes rows to fit, but this
        # belt-and-suspenders flag also covers any other wrapped column.
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setStretchLastSection(False)
        header = self.table.horizontalHeader()
        # Drag-to-reorder columns. Order is persisted in the active view's XML
        # (or in QSettings under grid/order/<doc>/__default when no view is
        # active). System columns are guarded against reorder in
        # _on_section_moved.
        header.setSectionsMovable(True)
        header.sectionMoved.connect(self._on_section_moved)
        header.resizeSection(0, 28)   # state gutter
        header.resizeSection(1, 140)  # ID column
        header.resizeSection(2, 420)  # Description

        # Load persisted visibility; fall back to defaults.
        doc_id = self._controller.current_doc_id()
        if doc_id:
            self._apply_hidden_columns(self._load_hidden_columns(doc_id))
        self.table.selectionModel().currentRowChanged.connect(self._on_row_changed)
        # Right-click menu for row-scoped operations (figures today; can grow).
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        # Double-clicking a figure thumbnail opens it full-size (see eventFilter);
        # double-clicking the text itself still starts the inline editor.
        self.table.viewport().installEventFilter(self)

        # Empty-state widget — shown when the current doc has no objects.
        self._empty = self._build_empty_state()
        # Distinct filter-empty state: the doc has objects but the active
        # filter (text + suspect-only) matches nothing.
        self._filter_empty = self._build_filter_empty_state()

        self._stack = QStackedWidget()
        self._stack.addWidget(self.table)
        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._filter_empty)

        # Stale banner sits topmost — "your view of the file is outdated"
        # outranks both the window mode and per-document link state.
        self.stale_banner = StaleBanner()

        # Read-only banner sits above the suspect banner — it's a window-mode
        # indicator, the suspect banner is per-document state.
        self.read_only_banner = ReadOnlyBanner()

        # Suspect banner above the grid.
        self.banner = SuspectBanner()
        self.banner.suspect_only_toggled.connect(self._proxy.setSuspectOnly)

        # Filter pill below the suspect banner; emits signals consumed by
        # MainWindow so dialog launching stays out of the panel.
        self.filter_pill = FilterPill()
        self.filter_pill.edit_clicked.connect(
            lambda: self.filter_edit_requested.emit()
        )
        self.filter_pill.clear_clicked.connect(self.clear_active_filter)

        # The currently-applied saved filter (None when not active).
        self._active_filter = None  # type: ignore[var-annotated]
        # Per-document remembered slug → filter, used to re-apply on doc switch.
        self._doc_filter_slug: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.stale_banner)
        layout.addWidget(self.read_only_banner)
        layout.addWidget(self.banner)
        layout.addWidget(self.filter_pill)
        layout.addWidget(self.filter_box)
        layout.addWidget(self._stack)

        controller.object_selected.connect(self._on_external_selection)
        controller.document_selected.connect(self._refresh_banner)
        self._model.modelReset.connect(self._on_model_reset)
        self._model.modelReset.connect(self._update_empty_state)
        self._model.modelReset.connect(self._refresh_banner)
        self._model.modelReset.connect(self.table.resizeRowsToContents)
        self._proxy.rowsInserted.connect(lambda *_: self._update_empty_state())
        self._proxy.rowsRemoved.connect(lambda *_: self._update_empty_state())
        self._proxy.layoutChanged.connect(self._update_empty_state)
        controller.repo_loaded.connect(lambda *_: self._update_empty_state())
        # After a commit every object's dirty bit may flip blue. Repaint the
        # id column without resetting the model so scroll/selection survive.
        # (Link states are refreshed by the window whenever HEAD moves.)
        controller.committed.connect(lambda *_: self._model.refresh_dirty_states())
        # Re-fit rows when the Description column is resized (visual wrap
        # changes the wrapped line count and therefore the row height).
        # Debounced: a header drag emits sectionResized on every mouse move,
        # and re-measuring every row each time hangs on large documents.
        self._refit_rows_timer = QTimer(self)
        self._refit_rows_timer.setSingleShot(True)
        self._refit_rows_timer.setInterval(150)
        self._refit_rows_timer.timeout.connect(self.table.resizeRowsToContents)
        header.sectionResized.connect(
            lambda _col, _old, _new: self._refit_rows_timer.start()
        )
        header.sectionResized.connect(self._on_section_resized)
        ThemeManager.instance().theme_changed.connect(self._reapply_icons)
        self._update_empty_state()
        self._refresh_banner()

    def _reapply_icons(self, *_args) -> None:
        self._empty_add_btn.setIcon(icons.icon("new_sibling"))
        self._filter_clear_btn.setIcon(icons.icon("filter_clear"))

    def focus_filter(self) -> None:
        self.filter_box.setFocus()
        self.filter_box.selectAll()

    def set_stale(self, on: bool) -> None:
        """Show/hide the "changed on disk" banner; cell edits are refused
        while it's up."""
        self.stale_banner.setVisible(bool(on))
        self._model.set_stale(on)

    def is_stale(self) -> bool:
        return not self.stale_banner.isHidden()

    def commit_open_editor(self) -> None:
        """Commit and close the open cell editor, if any.

        A model reset drops editors without committing them, and actions
        fired by shortcut or toolbar button don't move focus, so the editor's
        own focus-out commit never runs before such an action reloads.
        """
        self._close_open_editor(commit=True)

    def _close_open_editor(self, *, commit: bool) -> None:
        """Close the open cell editor, if any, committing its text first
        when `commit` is set."""
        if self.table.state() != QTableView.State.EditingState:
            return
        # The editor sits on the current row, but not necessarily in the
        # current column: selecting a row moves the current index to column 0.
        model = self.table.model()
        row = self.table.currentIndex().row()
        for col in range(model.columnCount()):
            idx = model.index(row, col)
            editor = self.table.indexWidget(idx)
            if editor is not None:
                delegate = self.table.itemDelegateForIndex(idx)
                if commit:
                    delegate.commitData.emit(editor)
                delegate.closeEditor.emit(
                    editor, QAbstractItemDelegate.EndEditHint.NoHint
                )
                return

    def set_read_only(self, on: bool) -> None:
        """Enter/leave read-only mode for the grid + its banner.

        Strips ItemIsEditable from the model, closes an open cell editor
        without committing it (like the Inspector, entering read-only drops
        typed-but-uncommitted text), shows or hides the read-only banner,
        and disables the empty-state "Add first object" affordance.
        """
        self._read_only = bool(on)
        self._model.set_read_only(on)
        if on:
            self._close_open_editor(commit=False)
        self.read_only_banner.setVisible(bool(on))
        self._empty_add_btn.setEnabled(not on)

    def _build_empty_state(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        label = QLabel("This document has no objects yet.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: palette(mid); font-size: 10pt;")
        btn = QPushButton(icons.icon("new_sibling"), "Add first object")
        btn.setDefault(True)
        btn.clicked.connect(self.create_first_object_requested.emit)
        self._empty_add_btn = btn
        layout.addWidget(label)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(2)
        return widget

    def _build_filter_empty_state(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        label = QLabel("No requirements match the current filter.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: palette(mid); font-size: 10pt;")
        btn = QPushButton(icons.icon("filter_clear"), "Clear filters")
        btn.clicked.connect(self.clear_filters_requested.emit)
        self._filter_clear_btn = btn
        layout.addWidget(label)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(2)
        return widget

    def _update_empty_state(self) -> None:
        doc_id = self._controller.current_doc_id()
        has_doc = doc_id is not None
        if not has_doc:
            self._stack.setCurrentWidget(self.table)
            return
        if self._model.rowCount() == 0:
            self._stack.setCurrentWidget(self._empty)
            return
        if self._proxy.rowCount() == 0:
            # Doc has objects, but the active filter hid them all.
            self._stack.setCurrentWidget(self._filter_empty)
            return
        self._stack.setCurrentWidget(self.table)

    def clear_all_filters(self) -> None:
        """Clear the text filter, the suspect-only toggle, and the saved filter."""
        self.filter_box.clear()
        self.set_suspect_only(False)
        self.clear_active_filter()

    # --- saved filter (DOORS-style multi-rule) -------------------------

    def apply_filter(self, filt) -> None:
        """Activate a saved Filter on the grid.

        Computes the matching object-id set via `mellon.filter.evaluate`
        (using a cross-doc incoming-link provider built from the controller's
        repo), pushes it into the proxy as the third filter axis, and updates
        the pill summary. Pass `None` to clear.
        """
        from ...filter import evaluate as _evaluate

        if filt is None:
            self._active_filter = None
            self._proxy.setMatchSet(None)
            self.filter_pill.hide_filter()
            doc_id = self._controller.current_doc_id()
            if doc_id and doc_id in self._doc_filter_slug:
                del self._doc_filter_slug[doc_id]
            self._update_empty_state()
            self.active_filter_changed.emit(None)
            return

        doc_id = self._controller.current_doc_id()
        if doc_id is None:
            return
        try:
            doc = self._controller.document(doc_id)
        except (ServiceError, KeyError):
            return

        provider = self._build_incoming_provider(doc_id)
        match_set = _evaluate(doc, filt, incoming_provider=provider)

        self._active_filter = filt
        self._doc_filter_slug[doc_id] = filt.slug
        self._proxy.setMatchSet(match_set)
        self.filter_pill.show_filter(
            name=filt.name,
            rule_count=len(filt.rules),
            combinator=filt.combinator,
            keep_ancestors=filt.keep_ancestors,
            match_count=len(match_set),
        )
        self._update_empty_state()
        self.active_filter_changed.emit(filt)

    def clear_active_filter(self) -> None:
        """Deactivate the saved filter for the session (file is preserved)."""
        self.apply_filter(None)

    def active_filter(self):
        """Return the currently-applied saved Filter, or None."""
        return self._active_filter

    def remembered_filter_slug(self, doc_id: str) -> str | None:
        """Return the slug of the filter that was active for `doc_id`, if any."""
        return self._doc_filter_slug.get(doc_id)

    def _build_incoming_provider(self, doc_id: str):
        """Closure that resolves cross-doc incoming-link sources for filter eval.

        Returns a callable suitable for `evaluate(..., incoming_provider=...)`.
        Falls back to "no cross-doc info" when no service is open.
        """
        svc = self._controller.service
        if svc is None:
            return None

        def provider(target_id: str):
            try:
                rows = svc.incoming_links(doc_id, target_id)
            except Exception:
                return ()
            return [src_doc for src_doc, _src_id, _link in rows]

        return provider

    # --- column visibility (storage backing the Views feature) -------

    _SETTINGS_KEY = "grid/hidden_columns"

    def _load_hidden_columns(self, doc_id: str) -> set[str]:
        """Return the persisted set of hidden column names for `doc_id`."""
        stored = _settings_mod.make_settings().value(
            f"{self._SETTINGS_KEY}/{doc_id}", None
        )
        if stored is None:
            return set()
        # QSettings may round-trip a list as a comma-separated string on some
        # backends; normalise both paths.
        if isinstance(stored, str):
            return {s for s in stored.split(",") if s}
        return set(stored or ())

    def _save_hidden_columns(self, doc_id: str, hidden: set[str]) -> None:
        _settings_mod.make_settings().setValue(
            f"{self._SETTINGS_KEY}/{doc_id}", sorted(hidden)
        )

    def _apply_hidden_columns(self, hidden: set[str]) -> None:
        for col_index in range(self._model.columnCount()):
            if not self._model.is_user_attribute_column(col_index):
                self.table.setColumnHidden(col_index, False)
                continue
            name = self._model.column_name(col_index) or ""
            self.table.setColumnHidden(col_index, name in hidden)

    # --- views (named column-visibility presets) ------------------------

    def all_user_attribute_names(self) -> list[str]:
        """Names of every user-attribute column in document order."""
        out: list[str] = []
        for col_index in range(self._model.columnCount()):
            if self._model.is_user_attribute_column(col_index):
                name = self._model.column_name(col_index)
                if name:
                    out.append(name)
        return out

    def current_visible_attrs(self) -> set[str]:
        """User-attribute names currently shown in the grid (system columns excluded)."""
        doc_id = self._controller.current_doc_id()
        if not doc_id:
            return set(self.all_user_attribute_names())
        hidden = self._load_hidden_columns(doc_id)
        return {n for n in self.all_user_attribute_names() if n not in hidden}

    def apply_visible_attrs(self, visible: set[str]) -> None:
        """Hide every user attribute not in `visible`; persist the resulting hidden set."""
        hidden = {n for n in self.all_user_attribute_names() if n not in visible}
        doc_id = self._controller.current_doc_id()
        if doc_id:
            self._save_hidden_columns(doc_id, hidden)
        self._apply_hidden_columns(hidden)

    # --- views (per-view active-bucket + widths) -----------------------

    def current_active_view_slug(self) -> str | None:
        """Slug of the currently-active view for the open document, or None."""
        doc_id = self._controller.current_doc_id()
        if not doc_id:
            return None
        bucket = self._read_active_bucket(doc_id)
        return None if bucket == _DEFAULT_BUCKET else bucket

    def apply_view(
        self, slug: str | None, visible: tuple[str, ...] | list[str] | set[str]
    ) -> None:
        """Activate a view (or clear it), apply its visibility, restore widths.

        Pass `slug=None` to fall back to the per-document default bucket.

        When `visible` is an ordered sequence (tuple/list), it drives both
        which user-attribute columns are shown and their order. When a `set`
        is passed (legacy callers), order is looked up via `_order_for_bucket`
        instead.
        """
        doc_id = self._controller.current_doc_id()
        bucket = slug if slug else _DEFAULT_BUCKET
        if doc_id:
            self._write_active_bucket(doc_id, bucket)
        self.apply_visible_attrs(set(visible))
        if isinstance(visible, set):
            order = self._order_for_bucket(doc_id, bucket) if doc_id else []
        else:
            order = list(visible)
        if order:
            self._apply_column_order(order)
        self._apply_widths(bucket)

    def snapshot_widths_to(self, slug: str) -> None:
        """Copy the current header's user-attribute widths into the view's bucket.

        Used right after creating a new view so the new bucket starts with the
        layout the user already had on screen.
        """
        doc_id = self._controller.current_doc_id()
        if not doc_id:
            return
        header = self.table.horizontalHeader()
        for idx in range(self._model.columnCount()):
            if not self._model.is_user_attribute_column(idx):
                continue
            name = self._model.column_name(idx)
            if not name:
                continue
            self._write_width(doc_id, slug, name, header.sectionSize(idx))

    # --- internal: active-bucket + width persistence ---

    def _read_active_bucket(self, doc_id: str) -> str:
        bucket = _settings_mod.make_settings().value(
            f"{_ACTIVE_VIEW_PREFIX}/{doc_id}", _DEFAULT_BUCKET
        )
        return str(bucket) if bucket else _DEFAULT_BUCKET

    def _write_active_bucket(self, doc_id: str, bucket: str) -> None:
        _settings_mod.make_settings().setValue(
            f"{_ACTIVE_VIEW_PREFIX}/{doc_id}", bucket
        )

    def _write_width(self, doc_id: str, bucket: str, attr: str, width: int) -> None:
        _settings_mod.make_settings().setValue(
            f"{_WIDTHS_PREFIX}/{doc_id}/{bucket}/{attr}", int(width)
        )

    def _read_widths(self, doc_id: str, bucket: str) -> dict[str, int]:
        settings = _settings_mod.make_settings()
        settings.beginGroup(f"{_WIDTHS_PREFIX}/{doc_id}/{bucket}")
        try:
            out: dict[str, int] = {}
            for key in settings.childKeys():
                try:
                    out[key] = int(settings.value(key))
                except (TypeError, ValueError):
                    pass
            return out
        finally:
            settings.endGroup()

    def _apply_widths(self, bucket: str) -> None:
        """Restore saved column widths for `bucket` onto the current header."""
        doc_id = self._controller.current_doc_id()
        if not doc_id:
            return
        widths = self._read_widths(doc_id, bucket)
        if not widths:
            return
        header = self.table.horizontalHeader()
        self._suspend_width_save = True
        try:
            for idx in range(self._model.columnCount()):
                if not self._model.is_user_attribute_column(idx):
                    continue
                name = self._model.column_name(idx)
                if name and name in widths:
                    header.resizeSection(idx, widths[name])
        finally:
            self._suspend_width_save = False

    def _on_section_resized(self, idx: int, _old: int, new: int) -> None:
        if self._suspend_width_save:
            return
        if not self._model.is_user_attribute_column(idx):
            return
        if new <= 0:  # hidden columns report width 0; ignore.
            return
        doc_id = self._controller.current_doc_id()
        if not doc_id:
            return
        name = self._model.column_name(idx)
        if not name:
            return
        self._write_width(doc_id, self._read_active_bucket(doc_id), name, new)

    # --- column reorder ------------------------------------------------

    def _system_column_count(self) -> int:
        n = 0
        for i in range(self._model.columnCount()):
            if not self._model.is_user_attribute_column(i):
                n += 1
            else:
                break
        return n

    def _current_user_attr_order(self) -> list[str]:
        """User-attribute names in current header visual order."""
        header = self.table.horizontalHeader()
        out: list[str] = []
        for visual in range(self._model.columnCount()):
            logical = header.logicalIndex(visual)
            if not self._model.is_user_attribute_column(logical):
                continue
            name = self._model.column_name(logical)
            if name:
                out.append(name)
        return out

    def _on_section_moved(self, _logical: int, old_visual: int, new_visual: int) -> None:
        """Persist the new column order, or revert moves that cross the
        system/user-attribute boundary (system columns must stay leftmost)."""
        if self._suspend_order_save:
            return
        n_system = self._system_column_count()
        if old_visual < n_system or new_visual < n_system:
            self._suspend_order_save = True
            try:
                self.table.horizontalHeader().moveSection(new_visual, old_visual)
            finally:
                self._suspend_order_save = False
            return
        self._persist_current_order()

    def _persist_current_order(self) -> None:
        doc_id = self._controller.current_doc_id()
        if not doc_id:
            return
        order = self._current_user_attr_order()
        slug = self.current_active_view_slug()
        if slug:
            if self._read_only:
                # The view XML is part of the document; keep the visual
                # reorder but don't write it while read-only.
                return
            # Update the view's <attribute> order on disk. Visible set is
            # unchanged because we pass exactly the currently-visible attrs.
            try:
                self._controller.set_view_visible(doc_id, slug, tuple(order))
            except ServiceError:
                # Transient service error — leave the visual reorder in
                # place but don't persist.
                return
        else:
            _settings_mod.make_settings().setValue(
                f"{_ORDER_PREFIX}/{doc_id}/{_DEFAULT_BUCKET}",
                order,
            )

    def _read_default_bucket_order(self, doc_id: str) -> list[str]:
        stored = _settings_mod.make_settings().value(
            f"{_ORDER_PREFIX}/{doc_id}/{_DEFAULT_BUCKET}", None
        )
        if stored is None:
            return []
        if isinstance(stored, str):
            return [s for s in stored.split(",") if s]
        return [str(s) for s in (stored or ()) if s]

    def _apply_column_order(self, order: list[str]) -> None:
        """Reorder header sections so user-attribute columns appear in the
        sequence given by `order`. Names not present on the current model are
        skipped silently. System columns are not touched."""
        if not order:
            return
        header = self.table.horizontalHeader()
        n_system = self._system_column_count()
        self._suspend_order_save = True
        try:
            for target_offset, name in enumerate(order):
                logical = self._model.column_index(name)
                if logical < 0:
                    continue
                current_visual = header.visualIndex(logical)
                desired_visual = n_system + target_offset
                if current_visual != desired_visual:
                    header.moveSection(current_visual, desired_visual)
        finally:
            self._suspend_order_save = False

    def _install_delegates(self) -> None:
        """Attach delegates to each column based on the model's current schema.

        The system columns (state, id, description) get their bespoke painters
        and editors. Every other column — `type` plus each user attribute —
        gets an `AttrDelegate`, which looks up the manifest schema at editor
        creation time, so it works for any kind without further branching.
        """
        for col_index in range(self._model.columnCount()):
            attr_name = self._model.column_name(col_index)
            if attr_name == "state":
                self.table.setItemDelegateForColumn(
                    col_index, StateGutterDelegate(self.table)
                )
                continue
            if attr_name == "id":
                self.table.setItemDelegateForColumn(
                    col_index, IdColumnDelegate(self.table)
                )
                continue
            if attr_name == "heading_or_text":
                self.table.setItemDelegateForColumn(col_index, self._desc_delegate)
                continue
            if attr_name is None:
                continue
            self.table.setItemDelegateForColumn(
                col_index, AttrDelegate(self._controller, attr_name, self.table)
            )

    def _on_model_reset(self) -> None:
        """Re-bind delegates and re-apply column visibility / order / widths
        after a reset.

        The column count and identity change every time the user opens a
        different document, so anything that's keyed off column position
        (delegates, hidden state, visual order) has to be redone.
        """
        self._install_delegates()
        doc_id = self._controller.current_doc_id()
        if doc_id:
            self._apply_hidden_columns(self._load_hidden_columns(doc_id))
            bucket = self._read_active_bucket(doc_id)
            self._apply_column_order(self._order_for_bucket(doc_id, bucket))
            self._apply_widths(bucket)

    def _order_for_bucket(self, doc_id: str, bucket: str) -> list[str]:
        """Look up the saved column order for `bucket`.

        For an active view the source of truth is the view's `<attribute>`
        order on disk; for the default bucket the order is in QSettings.
        """
        if bucket == _DEFAULT_BUCKET:
            return self._read_default_bucket_order(doc_id)
        try:
            doc = self._controller.document(doc_id)
        except (ServiceError, KeyError):
            return []
        for view in doc.views:
            if view.slug == bucket:
                return list(view.visible)
        return []

    # --- handlers -------------------------------------------------------

    def _on_filter_changed(self, text: str) -> None:
        self._proxy.setTextFilter(text)

    def set_suspect_only(self, on: bool) -> None:
        """Toggle the suspect-only filter (driven by the banner's toggle)."""
        self._proxy.setSuspectOnly(on)
        self.banner.set_suspect_only_checked(on)

    def is_suspect_only(self) -> bool:
        return self._proxy.suspectOnly()

    def refresh_link_states(self) -> None:
        """Re-read link states (gutter + suspect banner) after HEAD moved."""
        self._model.refresh_link_states()
        self._refresh_banner()

    def refresh_figures(self) -> None:
        """Repaint after figure files were rewritten in place on disk.

        No model reset, so an open cell editor and the scroll position
        survive. Thumbnails are cached by mtime, so a repaint re-renders the
        changed ones; rows are re-fitted for a new aspect ratio and the id
        column re-reads which objects are dirty.
        """
        self._model.refresh_dirty_states()
        self.table.viewport().update()
        self.table.resizeRowsToContents()

    def _refresh_banner(self, *_args) -> None:
        doc_id = self._controller.current_doc_id()
        if doc_id is None:
            self.banner.set_count(0)
            return
        self.banner.set_count(self._controller.suspect_count(doc_id))

    def _on_row_changed(self, current, _previous) -> None:
        if not current.isValid():
            return
        src = self._proxy.mapToSource(current)
        obj_id = self._model.obj_id_at(src.row())
        doc_id = self._controller.current_doc_id()
        if obj_id is not None and doc_id is not None:
            # Guard against re-entrancy when the controller re-emits.
            if self._controller.current_obj_id() != obj_id:
                self._controller.select_object(doc_id, obj_id)

    def _on_external_selection(self, doc_id: str, obj_id: str) -> None:
        if self._controller.current_doc_id() != doc_id:
            return
        row = self._model.row_for_obj_id(obj_id)
        if row < 0:
            return
        src = self._model.index(row, 0)
        proxy = self._proxy.mapFromSource(src)
        if proxy.isValid():
            self.table.setCurrentIndex(proxy)
            self.table.scrollTo(proxy)

    # --- row context menu ----------------------------------------------

    def _on_table_context_menu(self, pos: QPoint) -> None:
        proxy_idx = self.table.indexAt(pos)
        if not proxy_idx.isValid():
            return
        src_idx = self._proxy.mapToSource(proxy_idx)
        obj_id = self._model.obj_id_at(src_idx.row())
        doc_id = self._controller.current_doc_id()
        if obj_id is None or doc_id is None:
            return

        desc_col = self._model.column_index("heading_or_text")
        figure_path: str | None = None
        if desc_col >= 0:
            desc_idx = self._model.index(src_idx.row(), desc_col)
            data = desc_idx.data(OBJ_FIGURE_PATH_ROLE)
            figure_path = str(data) if data else None

        menu = QMenu(self.table)
        menu.setToolTipsVisible(True)
        writable = not self._read_only and not self.is_stale()
        drawio_label, drawio_blocked = drawio_action_text(figure_path)
        drawio_action = QAction(drawio_label, menu)
        drawio_action.setEnabled(writable and drawio_blocked is None)
        if drawio_blocked:
            drawio_action.setToolTip(drawio_blocked)
        drawio_action.triggered.connect(
            lambda: self.edit_figure_in_drawio(doc_id, obj_id)
        )
        menu.addAction(drawio_action)

        attach_label = "Replace figure…" if figure_path else "Attach figure…"
        attach_action = QAction(attach_label, menu)
        attach_action.setEnabled(writable)
        attach_action.triggered.connect(
            lambda: self.attach_figure(doc_id, obj_id)
        )
        menu.addAction(attach_action)

        clear_action = QAction("Clear figure", menu)
        clear_action.setEnabled(bool(figure_path) and writable)
        clear_action.triggered.connect(
            lambda: self.clear_figure(doc_id, obj_id)
        )
        menu.addAction(clear_action)

        view_action = QAction("View full size", menu)
        view_action.setEnabled(bool(figure_path))
        view_action.triggered.connect(
            lambda: self.view_figure(doc_id, obj_id)
        )
        menu.addAction(view_action)

        # Table actions (insert/remove/structure) live in the toolbar "Table"
        # menu, not here.
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # --- figure operations (shared by context menu, toolbar, double-click) --

    def attach_figure(self, doc_id: str, obj_id: str) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            f"Attach figure to {obj_id}",
            str(Path.home()),
            "SVG figures (*.svg);;All files (*)",
        )
        if not path_str:
            return
        svc = self._controller.service
        if svc is None:
            return
        try:
            svc.set_figure(doc_id, obj_id, Path(path_str))
        except ServiceError as e:
            QMessageBox.warning(self, "Attach figure failed", str(e))
            return
        self._controller.reload_document(doc_id)

    def edit_figure_in_drawio(self, doc_id: str, obj_id: str) -> None:
        """Open `obj_id`'s figure in draw.io desktop.

        An object without a figure first gets a blank draw.io figure. draw.io
        saves back into the same file, and the window picks the change up when
        it is next activated (`RepoController.changed_figures`).
        """
        exe = drawio.find_drawio()
        if exe is None:
            QMessageBox.information(self, "draw.io not found", DRAWIO_NOT_FOUND_TEXT)
            return
        path = self.figure_abs_path(obj_id)
        if path is None:
            svc = self._controller.service
            if svc is None:
                return
            with tempfile.TemporaryDirectory(prefix="mellon-figure-") as tmp:
                blank = Path(tmp) / f"{obj_id}.svg"
                blank.write_bytes(drawio.BLANK_DRAWIO_SVG)
                try:
                    svc.set_figure(doc_id, obj_id, blank)
                except ServiceError as e:
                    QMessageBox.warning(self, "New draw.io figure failed", str(e))
                    return
            self._controller.reload_document(doc_id)
            path = self.figure_abs_path(obj_id)
            if path is None:
                return
        try:
            drawio.open_in_drawio(exe, Path(path))
        except drawio.DrawioError as e:
            QMessageBox.warning(self, "Could not start draw.io", str(e))

    def clear_figure(self, doc_id: str, obj_id: str) -> None:
        choice = QMessageBox.question(
            self,
            "Clear figure",
            f"Remove the figure attached to {obj_id}?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Ok:
            return
        svc = self._controller.service
        if svc is None:
            return
        try:
            svc.clear_figure(doc_id, obj_id)
        except ServiceError as e:
            QMessageBox.warning(self, "Clear figure failed", str(e))
            return
        self._controller.reload_document(doc_id)

    def _on_inline_editor_opened(self, editor) -> None:
        self.table_editor_changed.emit(editor)

    def _on_inline_editor_closed(self, *_args) -> None:
        # `*_args` tolerates the spurious argument PySide hands the slot when
        # the editor is torn down via QObject.destroyed.
        self.table_editor_changed.emit(None)

    def begin_table_edit(self, obj_id: str) -> None:
        """Open the inline table editor on `obj_id`'s description cell.

        `view.edit()` runs `createEditor` synchronously, so by the time this
        returns the editor exists and `table_editor_changed` has fired — the
        toolbar Table menu can act on it immediately.
        """
        row = self._model.row_for_obj_id(obj_id)
        desc_col = self._model.column_index("heading_or_text")
        if row < 0 or desc_col < 0:
            return
        proxy_idx = self._proxy.mapFromSource(self._model.index(row, desc_col))
        if proxy_idx.isValid():
            self.table.setCurrentIndex(proxy_idx)
            self.table.edit(proxy_idx)

    def insert_table(self, doc_id: str, obj_id: str) -> None:
        """Attach a default 2x2 table (one header row) to `obj_id`."""
        svc = self._controller.service
        if svc is None:
            return
        default = Table(
            columns=(TableColumn("left"), TableColumn("left")),
            rows=(
                TableRow((TableCell("Column 1"), TableCell("Column 2")), header=True),
                TableRow((TableCell(""), TableCell(""))),
            ),
        )
        try:
            svc.set_table(doc_id, obj_id, default)
        except ServiceError as e:
            QMessageBox.warning(self, "Insert table failed", str(e))
            return
        self._controller.reload_document(doc_id)

    def remove_table(self, doc_id: str, obj_id: str) -> None:
        choice = QMessageBox.question(
            self,
            "Remove table",
            f"Remove the table attached to {obj_id}?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Ok:
            return
        svc = self._controller.service
        if svc is None:
            return
        try:
            svc.clear_table(doc_id, obj_id)
        except ServiceError as e:
            QMessageBox.warning(self, "Remove table failed", str(e))
            return
        self._controller.reload_document(doc_id)

    def view_figure(self, doc_id: str, obj_id: str) -> None:
        """Open the object's figure full-size. No-op if none is attached."""
        path = self.figure_abs_path(obj_id)
        if not path:
            return
        FigureViewDialog(path, obj_id, self).exec()

    def figure_abs_path(self, obj_id: str) -> str | None:
        """Absolute path to `obj_id`'s figure SVG, or None when unattached.

        Reads the same `OBJ_FIGURE_PATH_ROLE` the delegate paints from, so the
        viewer always opens exactly what's shown in the grid.
        """
        row = self._model.row_for_obj_id(obj_id)
        if row < 0:
            return None
        desc_col = self._model.column_index("heading_or_text")
        if desc_col < 0:
            return None
        data = self._model.index(row, desc_col).data(OBJ_FIGURE_PATH_ROLE)
        return str(data) if data else None

    def current_figure_path(self) -> str | None:
        """Figure path for the currently-selected object, or None."""
        obj_id = self._controller.current_obj_id()
        if obj_id is None:
            return None
        return self.figure_abs_path(obj_id)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        # Double-click on a figure thumbnail opens it full-size and consumes
        # the event so the description editor doesn't also open. Anything else
        # (including double-clicking the text) falls through to default editing.
        if (
            event.type() == QEvent.Type.MouseButtonDblClick
            and obj is self.table.viewport()
        ):
            pos = event.position().toPoint()
            proxy_idx = self.table.indexAt(pos)
            desc_col = self._model.column_index("heading_or_text")
            if proxy_idx.isValid() and proxy_idx.column() == desc_col:
                delegate = self.table.itemDelegateForColumn(desc_col)
                src_idx = self._proxy.mapToSource(proxy_idx)
                fig_rect = None
                if isinstance(delegate, MultiLineTextDelegate):
                    fig_rect = delegate.figure_rect(
                        self.table.visualRect(proxy_idx),
                        self.table.font(),
                        src_idx,
                    )
                if fig_rect is not None and fig_rect.contains(pos):
                    obj_id = self._model.obj_id_at(src_idx.row())
                    doc_id = self._controller.current_doc_id()
                    if obj_id is not None and doc_id is not None:
                        self.view_figure(doc_id, obj_id)
                        return True
        return super().eventFilter(obj, event)
