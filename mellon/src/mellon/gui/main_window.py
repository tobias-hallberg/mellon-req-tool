"""DocumentEditorWindow — three-pane editor for a single document.

Owns its own `RepoController` which shares the caller-supplied
`DocumentService`. All pane wiring happens here; the panels and models don't
know about each other.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QObject,
    QRunnable,
    QSize,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QToolButton,
    QWidget,
)
from PySide6.QtCore import Qt

from ..drawio import DOWNLOAD_URL, find_drawio
from ..editor import Journal
from ..export import TectonicError, pdf as export_pdf
from ..git_ops import GitError, dirty_count, discover_repo, head_summary
from ..schema import Document
from ..suspects import LinkState, LinkStatus
from ..service import DocumentService, ServiceError
from . import branding, icons
from .controller import RepoController, describe_load_error
from .dialogs.about_dialog import show_about
from .dialogs.commit_dialog import CommitDialog
from .dialogs.link_picker import LinkPicker
from .dialogs.trace_view import TraceView
from .models.objects_grid import STALE_EDIT_MESSAGE
from .panels.grid_pane import GridPane, drawio_action_text
from .panels.outline_pane import OutlinePane
from .panels.right_pane import RightPane
from .settings import make_settings
from .theme import ThemeManager, build_theme_menu


_GEOMETRY_KEY = "document_editor/geometry"
_SPLITTER_KEY = "document_editor/splitter"
_OUTLINE_VISIBLE_KEY = "document_editor/outline_visible"
_OUTLINE_WIDTH_KEY = "document_editor/outline_width"
_INSPECTOR_VISIBLE_KEY = "document_editor/inspector_visible"
_INSPECTOR_WIDTH_KEY = "document_editor/inspector_width"
_EXPORT_DIR_KEY = "document_editor/export_dir"
# Links listed in the pre-export suspect warning before "… and N more".
_LINK_WARNING_ROWS = 10

_OUTLINE_DEFAULT_WIDTH = 280
_INSPECTOR_DEFAULT_WIDTH = 400


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    if value is None:
        return default
    return bool(value)


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class _ExportSignals(QObject):
    """Carries a PDF export's outcome back to the GUI thread."""

    finished = Signal(int, list)  # bytes written, ids of figures left out
    failed = Signal(str)  # message for the user (tectonic error, write error)
    crashed = Signal(object)  # unexpected exception, re-raised on the GUI thread


class _ExportJob(QRunnable):
    """Render a document to PDF and write the file, off the GUI thread.

    Tectonic can take minutes (its first run downloads a TeX bundle), so the
    window must stay responsive. The file is written here as well, so an
    export still lands if its window is closed meanwhile.
    """

    def __init__(
        self,
        signals: _ExportSignals,
        doc: Document,
        folder: Path,
        rev: str | None,
        target: Path,
        parents: dict[str, Document] | None = None,
    ) -> None:
        super().__init__()
        self._signals = signals
        self._doc = doc
        self._folder = folder
        self._rev = rev
        self._target = target
        self._parents = parents

    def run(self) -> None:
        missing: list[str] = []
        try:
            blob = export_pdf(
                self._doc, folder=self._folder, rev=self._rev, unrendered=missing,
                parents=self._parents,
            )
        except TectonicError as e:
            self._emit("failed", str(e))
            return
        except Exception as e:  # noqa: BLE001 — reported on the GUI thread
            self._emit("crashed", e)
            return
        try:
            self._target.write_bytes(blob)
        except OSError as e:
            self._emit("failed", f"Could not write file:\n{e}")
            return
        self._emit("finished", len(blob), missing)

    def _emit(self, signal: str, *args: object) -> None:
        # Quitting the app mid-export: Qt waits for this job, but PySide has
        # already deleted the signals object, so emitting raises. There is no
        # window left to tell; the file (if any) is already written.
        try:
            getattr(self._signals, signal).emit(*args)
        except RuntimeError:
            pass


class DocumentEditorWindow(QMainWindow):
    window_closed = Signal(str)  # emitted with doc_id on close
    read_only_changed = Signal(bool)

    def __init__(
        self,
        doc_id: str,
        service: DocumentService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Free the window and its widgets on close; otherwise every closed
        # editor stays alive and keeps receiving theme/controller signals.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._doc_id = doc_id
        self._read_only = True
        # The live inline table editor, when one is open (set via the grid
        # pane's `table_editor_changed` signal); the toolbar Table menu drives it.
        self._active_table_editor = None
        # The running PDF export's signals and (doc_id, target, doc), if any.
        self._export_signals: _ExportSignals | None = None
        self._export_ctx: tuple[str, str] | None = None
        # HEAD short hash as last read by `_refresh_git_cell`, and the HEAD
        # the grid's link states were last refreshed for.
        self._git_head: str | None = None
        self._links_head: str | None = None
        # Undo history since the last commit: (label, recorded file changes)
        # for each edit made in this window, newest last.
        self._undo_steps: list[tuple[str, Journal]] = []
        self._redo_steps: list[tuple[str, Journal]] = []
        self.resize(1280, 820)

        self.controller = RepoController(self)

        # --- panels ------------------------------------------------------
        self.outline_pane = OutlinePane(self.controller, self)
        self.grid_pane = GridPane(self.controller, self)
        self.right_pane = RightPane(self.controller, self)
        # Backward-compat alias: tests + older call sites reach for the
        # inspector tab directly via `window.inspector`.
        self.inspector = self.right_pane.inspector

        # Horizontal splitter: [left | center | right]
        self._hsplit = QSplitter(Qt.Orientation.Horizontal, self)
        self._hsplit.addWidget(self.outline_pane)
        self._hsplit.addWidget(self.grid_pane)
        self._hsplit.addWidget(self.right_pane)
        self._hsplit.setStretchFactor(0, 2)
        self._hsplit.setStretchFactor(1, 5)
        self._hsplit.setStretchFactor(2, 3)
        self._hsplit.setSizes([280, 720, 400])
        self._hsplit.setCollapsible(2, True)
        sstate = make_settings().value(f"{_SPLITTER_KEY}/{doc_id}")
        # QSettings on Windows hands back QByteArray, not Python bytes.
        if isinstance(sstate, (bytes, bytearray, QByteArray)):
            self._hsplit.restoreState(sstate)
        self.setCentralWidget(self._hsplit)

        # --- menu + toolbar ---------------------------------------------
        # Toolbar first so QActions defined there can be reused in the menu.
        self._build_toolbar()
        self._main_menu = self._build_menus()
        self._install_hamburger()

        # --- status bar -------------------------------------------------
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._git_cell = QLabel()
        self._git_cell.setStyleSheet("padding: 0 6px; color: palette(mid);")
        self._status.addPermanentWidget(self._git_cell)

        # --- controller wiring ------------------------------------------
        self.controller.repo_loaded.connect(self._on_repo_loaded)
        self.controller.repo_load_failed.connect(self._on_repo_load_failed)
        self.controller.document_selected.connect(self._on_document_selected)
        self.controller.document_selected.connect(self._refresh_action_states)
        self.controller.object_selected.connect(self._refresh_action_states)
        self.controller.edit_failed.connect(self._on_edit_failed)
        self.controller.edit_succeeded.connect(self._on_edit_succeeded)
        self.controller.repo_loaded.connect(lambda *_: self._refresh_git_cell())
        self.controller.edit_succeeded.connect(lambda *_: self._refresh_git_cell())
        self.controller.edit_succeeded.connect(self._refresh_action_states)
        self.controller.committed.connect(lambda *_: self._refresh_git_cell())
        # After `_refresh_git_cell` has read the new HEAD.
        self.controller.committed.connect(lambda *_: self._check_head_moved())
        self.controller.committed.connect(self._refresh_action_states)
        # Undo never reaches back past a commit.
        self.controller.committed.connect(lambda *_: self._clear_undo())
        self.grid_pane.create_first_object_requested.connect(self._new_top_level)
        self.grid_pane.stale_banner.reload_clicked.connect(self._reload_from_disk)
        self.grid_pane.banner.ack_selected_clicked.connect(self._ack_selected)
        self.grid_pane.read_only_banner.disable_clicked.connect(
            lambda: self._set_read_only(False)
        )
        self.grid_pane.table.indent_requested.connect(self._indent)
        self.grid_pane.table.outdent_requested.connect(self._outdent)
        self.grid_pane.clear_filters_requested.connect(self._clear_all_filters)
        self.grid_pane.filter_edit_requested.connect(self._open_filter_dialog)
        self.grid_pane.table_editor_changed.connect(self._on_table_editor_changed)
        # Every apply_filter (including pill `[×]`) flows through this slot
        # so the QSettings entry stays in sync with the in-session state and
        # the next launch doesn't re-apply a deactivated filter.
        self.grid_pane.active_filter_changed.connect(self._on_active_filter_changed)
        self.controller.filters_changed.connect(self._on_filters_changed)
        self.controller.document_selected.connect(self._restore_active_filter)

        # --- attach service and select document -------------------------
        self.controller.attach_service(service)
        self.controller.select_document(doc_id)
        # Every write the service makes for this document becomes an undo
        # step here; unhooked on close (and destroy, should close not run).
        service.add_change_listener(self._on_files_changed)
        listener = self._on_files_changed
        self.destroyed.connect(lambda *_: service.remove_change_listener(listener))

        doc_name = self.controller.document(doc_id).metadata.title or doc_id
        self.setWindowTitle(f"{doc_name} — Document Explorer — Mellon")
        self.setWindowIcon(branding.window_icon())

        # Apply the default read-only mode now that panes are wired. Push the
        # state through the same path the menu/banner toggle uses so banner
        # visibility, model flags, inspector button, and toolbar all line up.
        self.grid_pane.set_read_only(self._read_only)
        self.right_pane.set_read_only(self._read_only)
        self._refresh_action_states()
        self._refresh_git_cell()
        self._links_head = self._git_head  # the grid was just built

        # --- Restore last geometry --------------------------------------
        s = make_settings()
        geom = s.value(f"{_GEOMETRY_KEY}/{doc_id}")
        if isinstance(geom, (bytes, bytearray, QByteArray)):
            self.restoreGeometry(geom)

        # Restore pane visibility. Setting the toolbar buttons triggers the
        # toggled handlers which adjust visibility + splitter sizes; block
        # those so we apply state directly without fighting the just-restored
        # splitter sizes from `restoreState`.
        outline_visible = _coerce_bool(s.value(f"{_OUTLINE_VISIBLE_KEY}/{doc_id}", True), True)
        inspector_visible = _coerce_bool(s.value(f"{_INSPECTOR_VISIBLE_KEY}/{doc_id}", True), True)
        for btn, visible, pane in (
            (self._toggle_left_btn, outline_visible, self.outline_pane),
            (self._toggle_right_btn, inspector_visible, self.right_pane),
        ):
            blocked = btn.blockSignals(True)
            btn.setChecked(visible)
            btn.blockSignals(blocked)
            pane.setVisible(visible)

    # --- close -----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        s = make_settings()
        doc_id = self._doc_id
        s.setValue(f"{_GEOMETRY_KEY}/{doc_id}", self.saveGeometry())
        s.setValue(f"{_SPLITTER_KEY}/{doc_id}", self._hsplit.saveState())
        s.setValue(f"{_OUTLINE_VISIBLE_KEY}/{doc_id}", self.outline_pane.isVisible())
        s.setValue(f"{_INSPECTOR_VISIBLE_KEY}/{doc_id}", self.right_pane.isVisible())
        sizes = self._hsplit.sizes()
        if self.outline_pane.isVisible() and len(sizes) == 3 and sizes[0] > 0:
            s.setValue(f"{_OUTLINE_WIDTH_KEY}/{doc_id}", sizes[0])
        if self.right_pane.isVisible() and len(sizes) == 3 and sizes[2] > 0:
            s.setValue(f"{_INSPECTOR_WIDTH_KEY}/{doc_id}", sizes[2])
        if self.controller.service is not None:
            self.controller.service.remove_change_listener(self._on_files_changed)
        self.window_closed.emit(doc_id)
        super().closeEvent(event)

    # --- disk freshness ---------------------------------------------------

    def event(self, e: QEvent) -> bool:  # type: ignore[override]
        # Re-check on-disk freshness whenever the user comes back to this
        # window — the moment they'd start acting on possibly-stale data.
        if (
            e.type() == QEvent.Type.WindowActivate
            and getattr(self, "controller", None) is not None
        ):
            self._check_disk_freshness()
            self._refresh_git_cell()
            self._check_head_moved()
        return super().event(e)

    def _check_disk_freshness(self) -> None:
        # A figure saved in place (by draw.io, after "Edit in draw.io") is
        # picked up without the banner; anything else changed means stale.
        changed_figures = self.controller.changed_figures()
        self._set_stale(changed_figures is None)
        if changed_figures:
            self.grid_pane.refresh_figures()

    def _check_head_moved(self) -> None:
        """Re-read the grid's link states when HEAD moved since they were
        last read. A commit — in this window, another window or a git
        client — can flip this document's links to SUSPECT without touching
        its files (revising a parent does), so no reload would notice."""
        if self._git_head == self._links_head:
            return
        self._links_head = self._git_head
        # A commit from outside this window (a git client): once this
        # document has nothing uncommitted, its edits are in that commit.
        if self._undo_steps or self._redo_steps:
            try:
                dirty = self.controller.require_service().has_uncommitted_changes(
                    self._doc_id
                )
            except (ServiceError, RuntimeError):
                dirty = False
            if not dirty:
                self._clear_undo()
        self.grid_pane.refresh_link_states()
        # Same for the Inspector's link tags + Ack buttons and the banner's
        # "Ack selected".
        self.inspector.refresh_links()
        self._refresh_action_states()

    def _set_stale(self, on: bool) -> None:
        """Show/hide the "Changed on disk" banner. While it's up, edits are
        refused (no auto-reload) so cached text can't overwrite the change."""
        self.grid_pane.set_stale(on)
        self.inspector.set_stale(on)
        if on:
            self._clear_undo()  # the files are no longer what the steps left

    def _reload_from_disk(self) -> None:
        """Drop every cache and re-read this document from disk.

        Invalidates the whole shared cache (not just this doc) so cross-doc
        state — suspect scans, incoming links, parent snapshots — refreshes
        too. Selection is preserved when the object survives the reload.
        """
        svc = self.controller.service
        if svc is None:
            return
        self.flush_pending_edits()
        self._clear_undo()
        if svc.cache is not None:
            svc.cache.invalidate_all()
        self.controller.reload_document(self._doc_id)
        if self._doc_id not in self.controller.docs():
            self._set_stale(True)
            err = self.controller.load_error(self._doc_id)
            if err is None:
                self._status.showMessage(
                    "Document no longer exists on disk.", 8000
                )
                return
            # Still on disk but broken (e.g. a pull left conflict markers):
            # say what and where, not that it is gone.
            self._status.showMessage(f"{self._doc_id} failed to load.", 8000)
            QMessageBox.warning(
                self,
                "Document failed to load",
                f"{self._doc_id} could not be reloaded:\n\n"
                f"{describe_load_error(err)}\n\n"
                "Fix the file (for a merge conflict, use Resolve merge… in "
                "the Project Explorer, or your git client), then press F5 "
                "again.",
            )
            return
        self._set_stale(False)
        self._refresh_git_cell()
        self._refresh_action_states()
        self._status.showMessage("Reloaded from disk.", 4000)

    # --- menu ------------------------------------------------------------

    def _build_menus(self) -> QMenu:
        menu = QMenu(self)
        menu.setMinimumWidth(260)

        # Export action lives on the toolbar too; reuse it here so the
        # shortcut + enabled state stay in sync.
        menu.addAction(self._export_act)
        # Back to the last commit in one go. No shortcut: it can't be undone.
        self._discard_act = QAction(
            "&Discard uncommitted changes to this document…", self
        )
        self._discard_act.setToolTip(
            "Put this document back to its last commit: edits are undone and "
            "objects added since then are deleted"
        )
        self._discard_act.setEnabled(False)
        self._discard_act.triggered.connect(self._discard_document_changes)
        menu.addAction(self._discard_act)
        # The action only shows in this menu, so its git status check runs
        # when the menu opens rather than on every selection change.
        menu.aboutToShow.connect(self._refresh_discard_action)
        close_act = QAction("&Close", self)
        close_act.setShortcut(QKeySequence.StandardKey.Close)
        close_act.triggered.connect(self.close)
        # Window-rooted action so the shortcut fires without a menu bar.
        self.addAction(close_act)
        menu.addAction(close_act)

        menu.addSection("Edit")
        # Window-wide; a text field with focus keeps Ctrl+Z for its own text.
        self._undo_act = QAction("&Undo", self)
        self._undo_act.setShortcuts(QKeySequence.StandardKey.Undo)
        self._undo_act.triggered.connect(lambda: self._replay_step(undo=True))
        self.addAction(self._undo_act)
        menu.addAction(self._undo_act)
        self._redo_act = QAction("&Redo", self)
        self._redo_act.setShortcuts(QKeySequence.StandardKey.Redo)
        self._redo_act.triggered.connect(lambda: self._replay_step(undo=False))
        self.addAction(self._redo_act)
        menu.addAction(self._redo_act)
        self._refresh_undo_actions()

        find_act = QAction("&Find…", self)
        find_act.setShortcut(QKeySequence(QKeySequence.StandardKey.Find))
        find_act.triggered.connect(self.grid_pane.focus_filter)
        self.addAction(find_act)
        menu.addAction(find_act)

        adv_filter_act = QAction("&Advanced filter…", self)
        adv_filter_act.setShortcut(QKeySequence("Ctrl+Shift+F"))
        adv_filter_act.triggered.connect(self._open_filter_dialog)
        self.addAction(adv_filter_act)
        menu.addAction(adv_filter_act)

        menu.addSection("View")
        self._reload_act = QAction("Re&load from disk", self)
        self._reload_act.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self._reload_act.setToolTip(
            "Re-read this document (and its parents) from disk"
        )
        self._reload_act.triggered.connect(self._reload_from_disk)
        self.addAction(self._reload_act)
        menu.addAction(self._reload_act)

        self._read_only_act = QAction("&Read-only", self, checkable=True)
        self._read_only_act.setChecked(self._read_only)
        self._read_only_act.setToolTip(
            "Block all edits. Toggle off to allow editing this document."
        )
        self._read_only_act.toggled.connect(self._set_read_only)
        self.addAction(self._read_only_act)
        menu.addAction(self._read_only_act)
        menu.addSeparator()
        self._theme_actions = build_theme_menu(menu, self)

        menu.addSection("Help")
        about_act = QAction("&About Mellon…", self)
        about_act.triggered.connect(lambda: show_about(self))
        menu.addAction(about_act)

        return menu

    def _install_hamburger(self) -> None:
        tb = self._toolbar
        ham = QToolButton(self)
        ham.setText("Menu")
        ham.setIcon(icons.icon("menu"))
        ham.setToolTip("Menu")
        # MenuButtonPopup matches the Views/Filters buttons: Qt's split-button
        # chevron sits beside the icon instead of being overlaid on top of it.
        ham.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        ham.setMenu(self._main_menu)
        ham.clicked.connect(ham.showMenu)
        ham.setToolButtonStyle(tb.toolButtonStyle())
        ham.setAutoRaise(True)
        # Insert at leftmost position, with a separator after it.
        existing = tb.actions()
        if existing:
            first = existing[0]
            tb.insertWidget(first, ham)
            tb.insertSeparator(first)
        else:
            tb.addWidget(ham)
            tb.addSeparator()
        self._hamburger_btn = ham

    def _build_toolbar(self) -> None:
        tb = QToolBar("Outline", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(24, 24))
        self.addToolBar(tb)
        self._toolbar = tb

        def act(text: str, shortcut, slot, *, tip: str, icon_name: str) -> QAction:
            a = QAction(icons.icon(icon_name), text, self)
            if shortcut is not None:
                a.setShortcut(shortcut)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            return a

        self._act_new_sibling = act(
            "New", QKeySequence("Ctrl+N"), self._new_sibling,
            tip="New sibling object (Ctrl+N)", icon_name="new_sibling",
        )
        self._act_new_child = act(
            "Child", QKeySequence("Ctrl+Shift+N"), self._new_child,
            tip="New child object (Ctrl+Shift+N)", icon_name="new_child",
        )
        self._act_indent = act(
            "Indent", QKeySequence("Ctrl+]"), self._indent,
            tip="Indent — make the selection a child of its previous sibling (Ctrl+])",
            icon_name="indent",
        )
        self._act_outdent = act(
            "Outdent", QKeySequence("Ctrl+["), self._outdent,
            tip="Outdent — promote the selection one level (Ctrl+[)",
            icon_name="outdent",
        )
        self._act_move_up = act(
            "Move up", QKeySequence("Alt+Up"), self._move_up,
            tip="Move up (Alt+↑)", icon_name="move_up",
        )
        self._act_move_down = act(
            "Move down", QKeySequence("Alt+Down"), self._move_down,
            tip="Move down (Alt+↓)", icon_name="move_down",
        )
        self._act_delete = act(
            "Delete", QKeySequence("Del"), self._delete_selected,
            tip="Delete object (Del)", icon_name="delete",
        )
        # These sit on the outline pane's toolbar; registering them on the
        # window too keeps their shortcuts working while that pane is hidden.
        self.addActions([
            self._act_new_sibling, self._act_new_child, self._act_indent,
            self._act_outdent, self._act_move_up, self._act_move_down,
            self._act_delete,
        ])
        self._commit_act = act(
            "Commit", None, self._commit_current_doc,
            tip="Commit this document's changes, or tag the current commit (Ctrl+S)",
            icon_name="commit",
        )
        self._commit_act.setShortcuts(
            [QKeySequence.StandardKey.Save, QKeySequence("Ctrl+K")]
        )
        self._export_act = act(
            "Export", QKeySequence("Ctrl+E"), self._export_current_doc,
            tip="Export this document to PDF (Ctrl+E)",
            icon_name="export",
        )
        self._validate_act = act(
            "Validate", None, self._validate_current_doc,
            tip="Validate this document against the schema",
            icon_name="review",
        )
        self._props_act = act(
            "Properties", None, self._show_document_properties,
            tip="Document properties…", icon_name="properties",
        )
        self._act_add_link = act(
            "Link…", QKeySequence("Ctrl+L"), self._add_link,
            tip="Add outgoing link (Ctrl+L)", icon_name="add_link",
        )
        self._act_trace = act(
            "Trace", QKeySequence("Ctrl+T"), self._show_trace,
            tip="Show up/down trace (Ctrl+T)", icon_name="trace",
        )
        # Keyboard accelerator for the Figure dropdown's primary action. The
        # button itself (built below) carries the attach/replace/clear/view
        # menu; this action just makes Ctrl+G attach/replace the selection.
        self._act_figure = act(
            "Figure…", QKeySequence("Ctrl+G"), self._figure_attach,
            tip="Attach or replace this object's figure (Ctrl+G)",
            icon_name="figure",
        )
        self.addAction(self._act_figure)

        tb.addAction(self._validate_act)
        tb.addAction(self._commit_act)
        tb.addAction(self._export_act)
        tb.addAction(self._props_act)
        tb.addSeparator()
        tb.addAction(self._act_add_link)
        tb.addAction(self._act_trace)
        self._figure_button = self._build_figure_button()
        tb.addWidget(self._figure_button)
        self._table_button = self._build_table_button()
        tb.addWidget(self._table_button)
        self._views_button = self._build_views_button()
        tb.addWidget(self._views_button)
        self._filters_button = self._build_filters_button()
        tb.addWidget(self._filters_button)

        spacer = QWidget(self)
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(spacer)

        self._toggle_left_btn = self._build_pane_toggle_button(
            icon_base="pane_left",
            tooltip="Show/hide outline pane",
            on_toggled=self._set_outline_pane_visible,
        )
        tb.addWidget(self._toggle_left_btn)
        self._toggle_right_btn = self._build_pane_toggle_button(
            icon_base="pane_right",
            tooltip="Show/hide inspector pane",
            on_toggled=self._set_right_pane_visible,
        )
        tb.addWidget(self._toggle_right_btn)

        self.outline_pane.set_edit_actions([
            self._act_new_sibling,
            self._act_new_child,
            None,
            self._act_indent,
            self._act_outdent,
            self._act_move_up,
            self._act_move_down,
            None,
            self._act_delete,
        ])

        # Theme changes invalidate the icon cache; re-fetch tinted QIcons.
        ThemeManager.instance().theme_changed.connect(self._reapply_toolbar_icons)

    def _reapply_toolbar_icons(self, *_args) -> None:
        self._act_new_sibling.setIcon(icons.icon("new_sibling"))
        self._act_new_child.setIcon(icons.icon("new_child"))
        self._act_indent.setIcon(icons.icon("indent"))
        self._act_outdent.setIcon(icons.icon("outdent"))
        self._act_move_up.setIcon(icons.icon("move_up"))
        self._act_move_down.setIcon(icons.icon("move_down"))
        self._act_delete.setIcon(icons.icon("delete"))
        self._act_add_link.setIcon(icons.icon("add_link"))
        self._act_trace.setIcon(icons.icon("trace"))
        self._act_figure.setIcon(icons.icon("figure"))
        self._figure_button.setIcon(icons.icon("figure"))
        self._table_button.setIcon(icons.icon("table"))
        self._commit_act.setIcon(icons.icon("commit"))
        self._export_act.setIcon(icons.icon("export"))
        self._validate_act.setIcon(icons.icon("review"))
        self._props_act.setIcon(icons.icon("properties"))
        if hasattr(self, "_hamburger_btn"):
            self._hamburger_btn.setIcon(icons.icon("menu"))
        self._toggle_left_btn.setIcon(
            icons.toggle_icon("pane_left_off", "pane_left_on")
        )
        self._toggle_right_btn.setIcon(
            icons.toggle_icon("pane_right_off", "pane_right_on")
        )

    def _build_pane_toggle_button(
        self,
        *,
        icon_base: str,
        tooltip: str,
        on_toggled,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("PaneToggleButton")
        button.setIcon(icons.toggle_icon(f"{icon_base}_off", f"{icon_base}_on"))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setCheckable(True)
        button.setChecked(True)
        button.setToolTip(tooltip)
        button.toggled.connect(on_toggled)
        return button

    # --- action-state refresh ------------------------------------------

    def _refresh_action_states(self, *_args) -> None:
        doc_id = self.controller.current_doc_id()
        obj_id = self.controller.current_obj_id()
        has_doc = doc_id is not None
        has_sel = has_doc and obj_id is not None
        writable = not self._read_only

        self._act_new_sibling.setEnabled(has_doc and writable)
        self._act_new_child.setEnabled(has_sel and writable)
        self._act_delete.setEnabled(has_sel and writable)
        self._act_add_link.setEnabled(has_sel and writable)
        self._act_trace.setEnabled(has_sel)
        self._act_figure.setEnabled(has_sel and writable)
        # Button stays enabled with a selection (even read-only) so its
        # "View full size" item works; the menu gates attach/clear per state.
        self._figure_button.setEnabled(has_sel)
        # Table button: enabled with a writable selection; the menu populates
        # Insert vs the structural ops based on whether the object has a table.
        self._table_button.setEnabled(has_sel and writable)
        self._validate_act.setEnabled(has_doc)
        self._props_act.setEnabled(has_doc and writable)
        # Export does not mutate the doc, so it works in read-only mode too.
        # One at a time per window.
        self._export_act.setEnabled(has_doc and not self.is_exporting())

        # Also with nothing to commit: the dialog can then tag the current
        # commit.
        self._commit_act.setEnabled(has_doc and writable)

        outline_ok = has_sel and writable
        if outline_ok:
            try:
                depth = self.controller.outline_depth(doc_id, obj_id)
                prev = self.controller.outline_previous_sibling(doc_id, obj_id)
                nxt = self.controller.outline_next_sibling(doc_id, obj_id)
            except ServiceError:
                # The document no longer loads (stale banner up, showing the
                # last good copy): nothing to move until it is reloaded.
                outline_ok = False
        if outline_ok:
            self._act_indent.setEnabled(prev is not None)
            self._act_outdent.setEnabled(depth > 0)
            self._act_move_up.setEnabled(prev is not None)
            self._act_move_down.setEnabled(nxt is not None)
        else:
            for a in (
                self._act_indent, self._act_outdent,
                self._act_move_up, self._act_move_down,
            ):
                a.setEnabled(False)

        self._refresh_undo_actions()

        # Enable "Ack selected" on the banner only when the selection has
        # at least one ackable suspect link AND we're not in read-only mode.
        has_ackable = False
        if has_sel and writable:
            statuses = self.controller.suspects_for_object(doc_id, obj_id)
            has_ackable = any(s.current_hash is not None for s in statuses)
        self.grid_pane.banner.set_ack_selected_enabled(has_ackable)

    # --- read-only toggle ----------------------------------------------

    def _set_read_only(self, on: bool) -> None:
        on = bool(on)
        if self._read_only == on:
            return
        self._read_only = on
        self.grid_pane.set_read_only(on)
        self.right_pane.set_read_only(on)
        # Reflect the new state in the View-menu checkbox without retriggering
        # this slot from `toggled`.
        if self._read_only_act.isChecked() != on:
            blocked = self._read_only_act.blockSignals(True)
            self._read_only_act.setChecked(on)
            self._read_only_act.blockSignals(blocked)
        self._refresh_action_states()
        self.read_only_changed.emit(on)

    # --- pane visibility -----------------------------------------------

    def _set_outline_pane_visible(self, visible: bool) -> None:
        self._set_pane_visible(
            visible,
            pane=self.outline_pane,
            split_index=0,
            width_key=f"{_OUTLINE_WIDTH_KEY}/{self._doc_id}",
            default_width=_OUTLINE_DEFAULT_WIDTH,
        )

    def _set_right_pane_visible(self, visible: bool) -> None:
        self._set_pane_visible(
            visible,
            pane=self.right_pane,
            split_index=2,
            width_key=f"{_INSPECTOR_WIDTH_KEY}/{self._doc_id}",
            default_width=_INSPECTOR_DEFAULT_WIDTH,
        )

    def _set_pane_visible(
        self,
        visible: bool,
        *,
        pane: QWidget,
        split_index: int,
        width_key: str,
        default_width: int,
    ) -> None:
        sizes = self._hsplit.sizes()
        if len(sizes) != 3:
            pane.setVisible(visible)
            return
        # Hide/show otherwise lets the splitter reapportion the toggled
        # pane's space to BOTH neighbors. We pin the opposite side pane's
        # width here so the grid absorbs the change instead.
        other_index = 2 if split_index == 0 else 0
        other_pane = self.right_pane if other_index == 2 else self.outline_pane
        other_visible = not other_pane.isHidden()
        other_width = sizes[other_index] if other_visible else 0
        s = make_settings()
        if not visible:
            if sizes[split_index] > 0:
                s.setValue(width_key, sizes[split_index])
            pane.setVisible(False)
            if not other_visible:
                return
            new_sizes = self._hsplit.sizes()
            if len(new_sizes) != 3:
                return
            total = sum(new_sizes)
            new_sizes[split_index] = 0
            new_sizes[other_index] = other_width
            new_sizes[1] = max(0, total - other_width)
            self._hsplit.setSizes(new_sizes)
            return
        target = _coerce_int(s.value(width_key), default_width)
        pane.setVisible(True)
        new_sizes = self._hsplit.sizes()
        if len(new_sizes) != 3:
            return
        total = sum(new_sizes)
        new_sizes[split_index] = target
        if other_visible:
            new_sizes[other_index] = other_width
        new_sizes[1] = max(120, total - new_sizes[split_index] - new_sizes[other_index])
        self._hsplit.setSizes(new_sizes)

    # --- mutation handlers ---------------------------------------------

    def flush_pending_edits(self) -> None:
        """Commit an open grid cell editor and pending Inspector text.

        Shortcuts and toolbar buttons don't take focus, so the editors'
        focus-out commits never fire before an action reloads the document —
        and the reload would silently drop the typed text.
        """
        self.grid_pane.commit_open_editor()
        self.inspector.commit_pending()

    def _prepare_edit(self) -> bool:
        """Preamble for every mutating action; False means don't run it.

        Refused while read-only, and while the "Changed on disk" banner is up
        (pending text is left in its editor rather than written over the
        external change). Otherwise pending text is committed first.
        """
        if self._read_only:
            return False
        if self.grid_pane.is_stale():
            self._status.showMessage(STALE_EDIT_MESSAGE, 6000)
            return False
        self.flush_pending_edits()
        return True

    # --- undo / redo ----------------------------------------------------

    def _on_files_changed(self, doc_id: str, label: str, changes: Journal) -> None:
        """Service change listener: an edit to this document is an undo step."""
        if doc_id != self._doc_id:
            return
        self._undo_steps.append((label, changes))
        self._redo_steps.clear()
        self._refresh_undo_actions()

    def _clear_undo(self) -> None:
        self._undo_steps.clear()
        self._redo_steps.clear()
        self._refresh_undo_actions()

    def _refresh_undo_actions(self) -> None:
        usable = not self._read_only and not self.grid_pane.is_stale()
        for act, steps, verb in (
            (self._undo_act, self._undo_steps, "Undo"),
            (self._redo_act, self._redo_steps, "Redo"),
        ):
            act.setEnabled(usable and bool(steps))
            act.setText(f"&{verb} {steps[-1][0]}" if steps else f"&{verb}")

    def _replay_step(self, *, undo: bool) -> None:
        """Undo the newest step (or redo the newest undone one): the files it
        changed go back to their bytes before (after) it. Refused, and the
        history cleared, when a file changed since."""
        if not self._prepare_edit():
            return
        # The flush may have written pending text: that is the newest step
        # now (and a redo has nothing left to redo).
        source = self._undo_steps if undo else self._redo_steps
        target = self._redo_steps if undo else self._undo_steps
        if not source:
            return
        label, changes = source.pop()
        svc = self.controller.require_service()
        verb = "undo" if undo else "redo"
        try:
            if undo:
                svc.undo_changes(changes)
            else:
                svc.redo_changes(changes)
        except ServiceError as e:
            self._clear_undo()
            self._status.showMessage(f"Can't {verb} {label}.", 8000)
            QMessageBox.warning(
                self,
                f"Can't {verb}",
                f"{e}\n\nThe undo history of this window is cleared.",
            )
            return
        target.append((label, changes))
        self.controller.reload_document(self._doc_id)
        folders = {p.parent.name for p in changes.paths}
        if "views" in folders:
            self.controller.views_changed.emit(self._doc_id)
        if "filters" in folders:
            self.controller.filters_changed.emit(self._doc_id)
        if "figures" in folders:
            self.grid_pane.refresh_figures()
        self._refresh_action_states()
        self._status.showMessage(
            f"{'Undid' if undo else 'Redid'} {label}.", 4000
        )

    def _new_sibling(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        if doc_id is None:
            return
        sel = self.controller.current_obj_id()
        parent_id = (
            self.controller.outline_parent_id(doc_id, sel) if sel is not None else None
        )
        svc = self.controller.require_service()
        try:
            result = svc.create_object(doc_id, parent_id=parent_id)
            new_id = result.value
            if sel is not None:
                svc.move_object(doc_id, new_id, after=sel)
        except ServiceError as e:
            self._status.showMessage(f"New sibling failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, new_id)
        self._status.showMessage(f"Created {new_id}", 3000)

    def _new_child(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        try:
            result = self.controller.require_service().create_object(
                doc_id, parent_id=sel
            )
        except ServiceError as e:
            self._status.showMessage(f"New child failed: {e}", 6000)
            return
        new_id = result.value
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, new_id)
        self._status.showMessage(f"Created {new_id} under {sel}", 3000)

    def _new_top_level(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        if doc_id is None:
            return
        try:
            result = self.controller.require_service().create_object(
                doc_id, parent_id=None
            )
        except ServiceError as e:
            self._status.showMessage(f"Create failed: {e}", 6000)
            return
        new_id = result.value
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, new_id)
        self._status.showMessage(f"Created {new_id}", 3000)

    def _indent(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        prev = self.controller.outline_previous_sibling(doc_id, sel)
        if prev is None:
            self._status.showMessage("No previous sibling to indent under.", 3000)
            return
        try:
            self.controller.require_service().move_object(
                doc_id, sel, parent_id=prev
            )
        except ServiceError as e:
            self._status.showMessage(f"Indent failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, sel)

    def _outdent(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        depth = self.controller.outline_depth(doc_id, sel)
        if depth <= 0:
            self._status.showMessage("Already at the top level.", 3000)
            return
        parent = self.controller.outline_parent_id(doc_id, sel)
        grandparent = (
            self.controller.outline_parent_id(doc_id, parent)
            if parent is not None
            else None
        )
        svc = self.controller.require_service()
        try:
            if grandparent is None:
                svc.move_object(doc_id, sel, to_top=True, after=parent)
            else:
                svc.move_object(doc_id, sel, parent_id=grandparent, after=parent)
        except ServiceError as e:
            self._status.showMessage(f"Outdent failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, sel)

    def _move_up(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        prev = self.controller.outline_previous_sibling(doc_id, sel)
        if prev is None:
            return
        try:
            self.controller.require_service().move_object(doc_id, sel, before=prev)
        except ServiceError as e:
            self._status.showMessage(f"Move up failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, sel)

    def _move_down(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        nxt = self.controller.outline_next_sibling(doc_id, sel)
        if nxt is None:
            return
        try:
            self.controller.require_service().move_object(doc_id, sel, after=nxt)
        except ServiceError as e:
            self._status.showMessage(f"Move down failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, sel)

    def _confirm_delete(self, obj_id: str) -> bool:
        # Always asked, though Ctrl+Z brings the object back: a delete can
        # also strip links in other documents.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Delete object")
        box.setText(f"Delete {obj_id}?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    def _delete_selected(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        svc = self.controller.require_service()

        if self.controller.outline_has_children(doc_id, sel):
            QMessageBox.warning(
                self,
                "Cannot delete object",
                f"'{sel}' has sub-objects in the outline.\n\n"
                "Re-parent or delete the sub-objects first, then try again.",
            )
            return

        if not self._confirm_delete(sel):
            self._status.showMessage("Delete cancelled.", 3000)
            return

        try:
            result = svc.delete_object(doc_id, sel, force=False)
        except ServiceError as e:
            self._status.showMessage(f"Delete failed: {e}", 8000)
            return

        report = result.report
        if report.performed:
            self.controller.reload_document(doc_id)
            self._status.showMessage(f"Deleted {sel}", 3000)
            return

        sources = ", ".join(f"{sd}/{sid}" for sd, sid, _ in report.incoming)
        msg = (
            f"Delete {sel}? This will also remove {len(report.incoming)} "
            f"incoming link(s) from {sources}."
        )
        choice = QMessageBox.question(
            self,
            "Delete object",
            msg,
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Ok:
            self._status.showMessage("Delete cancelled.", 3000)
            return

        try:
            result = svc.delete_object(doc_id, sel, force=True)
        except ServiceError as e:
            self._status.showMessage(f"Delete failed: {e}", 8000)
            return
        for did in result.affected_docs:
            self.controller.reload_document(did)
        self._status.showMessage(
            f"Deleted {sel} ({len(result.report.incoming)} incoming link(s) stripped).",
            5000,
        )

    def _add_link(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        dlg = LinkPicker(self.controller, doc_id, sel, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        result = dlg.selected()
        if result is None:
            self._status.showMessage("No target selected.", 3000)
            return
        target_doc, target_id, link_type = result
        try:
            self.controller.require_service().add_link(
                doc_id, sel, target_doc, target_id, link_type
            )
        except ServiceError as e:
            self._status.showMessage(f"Add link failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self.controller.select_object(doc_id, sel)
        self._status.showMessage(
            f"Linked {sel} --[{link_type}]--> {target_doc}/{target_id}", 4000
        )

    def _show_trace(self) -> None:
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        dlg = TraceView(self.controller, doc_id, sel, self)
        dlg.exec()

    # --- figures --------------------------------------------------------

    def _figure_attach(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        self.grid_pane.attach_figure(doc_id, sel)

    def _figure_clear(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        self.grid_pane.clear_figure(doc_id, sel)

    def _figure_view(self) -> None:
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        self.grid_pane.view_figure(doc_id, sel)

    def _figure_drawio(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        self.grid_pane.edit_figure_in_drawio(doc_id, sel)

    # --- suspect review -------------------------------------------------

    def _ack_selected(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        sel = self.controller.current_obj_id()
        if doc_id is None or sel is None:
            return
        statuses = self.controller.suspects_for_object(doc_id, sel)
        ackable = [s for s in statuses if s.current_hash is not None]
        if not ackable:
            self._status.showMessage(f"No acknowledgeable suspects on {sel}.", 3000)
            return
        try:
            result = self.controller.require_service().ack_all(doc_id, ackable)
        except ServiceError as e:
            self._status.showMessage(f"Ack failed: {e}", 6000)
            return
        self.controller.reload_document(doc_id)
        self._status.showMessage(
            f"Acknowledged {len(result.value)} link(s) on {sel}.", 4000
        )

    # --- controller handlers -------------------------------------------

    def _on_repo_loaded(self, root: str) -> None:
        self._status.showMessage(f"Loaded from {root}")

    def _on_repo_load_failed(self, root: str, message: str) -> None:
        self._status.showMessage(f"Failed to open {root}")
        QMessageBox.warning(self, "Could not open repository", message)

    def _on_document_selected(self, doc_id: str) -> None:
        doc = self.controller.document(doc_id)
        title = doc.metadata.title or doc_id
        self._status.showMessage(
            f"{doc_id} — {title}  ({len(doc.objects)} objects, "
            f"{sum(len(v) for v in doc.links.values())} outgoing links)"
        )

    def _on_edit_succeeded(self, doc_id: str, obj_id: str, field: str) -> None:
        self._status.showMessage(f"{doc_id}/{obj_id}.{field} updated", 4000)

    def _on_edit_failed(
        self, doc_id: str, obj_id: str, field: str, message: str
    ) -> None:
        self._status.showMessage(
            f"Edit failed on {doc_id}/{obj_id}.{field}: {message}", 8000
        )

    def _clear_all_filters(self) -> None:
        # `grid_pane.clear_all_filters` calls `clear_active_filter` which
        # emits `active_filter_changed(None)` — `_on_active_filter_changed`
        # clears the QSettings entry for us.
        self.grid_pane.clear_all_filters()

    def _build_figure_button(self) -> QToolButton:
        button = QToolButton(self)
        button.setText("Figure")
        button.setIcon(icons.icon("figure"))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        # InstantPopup: a single click opens the menu — no redundant
        # split-button chevron. The leftover menu-indicator glyph is hidden
        # via a narrow QSS rule so it reads as a plain toolbar button.
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        button.setToolTip(
            "Edit in draw.io, attach, replace, clear, or view this object's "
            "figure (Ctrl+G to attach)"
        )
        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(self._populate_figure_menu)
        button.setMenu(menu)
        self._figure_menu = menu
        return button

    def _populate_figure_menu(self) -> None:
        self._figure_menu.clear()
        doc_id = self.controller.current_doc_id()
        obj_id = self.controller.current_obj_id()
        has_sel = doc_id is not None and obj_id is not None
        writable = not self._read_only
        figure_path = self.grid_pane.current_figure_path() if has_sel else None

        drawio_label, drawio_blocked = drawio_action_text(figure_path)
        drawio_act = QAction(drawio_label, self._figure_menu)
        drawio_act.setEnabled(has_sel and writable and drawio_blocked is None)
        if drawio_blocked:
            drawio_act.setToolTip(drawio_blocked)
        drawio_act.triggered.connect(self._figure_drawio)
        self._figure_menu.addAction(drawio_act)

        attach_label = "Replace figure…" if figure_path else "Attach figure…"
        attach_act = QAction(attach_label, self._figure_menu)
        attach_act.setEnabled(has_sel and writable)
        attach_act.triggered.connect(self._figure_attach)
        self._figure_menu.addAction(attach_act)

        clear_act = QAction("Clear figure", self._figure_menu)
        clear_act.setEnabled(bool(figure_path) and writable)
        clear_act.triggered.connect(self._figure_clear)
        self._figure_menu.addAction(clear_act)

        view_act = QAction("View full size", self._figure_menu)
        view_act.setEnabled(bool(figure_path))
        view_act.triggered.connect(self._figure_view)
        self._figure_menu.addAction(view_act)

    # --- tables ---------------------------------------------------------

    def _build_table_button(self) -> QToolButton:
        button = QToolButton(self)
        button.setText("Table")
        button.setIcon(icons.icon("table"))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        # NoFocus so opening this menu never blurs (and commits) an open inline
        # table editor.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip("Insert, remove, and edit the selected object's table")
        menu = QMenu(button)
        menu.aboutToShow.connect(self._populate_table_menu)
        button.setMenu(menu)
        self._table_menu = menu
        return button

    def _on_table_editor_changed(self, editor) -> None:
        self._active_table_editor = editor
        self._refresh_action_states()

    def _live_editor(self):
        """The active inline table editor if it's still a live C++ object.

        Between `closeEditor` and the editor's `destroyed` signal there's a
        brief window where `_active_table_editor` is stale; calling into a
        deleted QWidget would crash, so probe it and drop a dead reference.
        """
        ed = self._active_table_editor
        if ed is None:
            return None
        try:
            ed.isVisible()
        except RuntimeError:
            self._active_table_editor = None
            return None
        return ed

    def _selected_object(self):
        """(doc_id, obj_id, RequirementObject) for the selection, or (None, None, None)."""
        doc_id = self.controller.current_doc_id()
        obj_id = self.controller.current_obj_id()
        if doc_id is None or obj_id is None:
            return None, None, None
        obj = self.controller.document(doc_id).objects.get(obj_id)
        return doc_id, obj_id, obj

    def _populate_table_menu(self) -> None:
        self._table_menu.clear()
        doc_id, obj_id, obj = self._selected_object()
        writable = not self._read_only
        has_table = obj is not None and obj.table is not None
        ed = self._live_editor()

        if not has_table:
            insert_act = QAction("Insert table", self._table_menu)
            insert_act.setEnabled(obj is not None and writable)
            insert_act.triggered.connect(self._table_insert)
            self._table_menu.addAction(insert_act)
            return

        def add(label: str, slot, *, enabled: bool = True, checkable: bool = False,
                checked: bool = False) -> QAction:
            a = QAction(label, self._table_menu)
            a.setEnabled(writable and enabled)
            if checkable:
                a.setCheckable(True)
                a.setChecked(checked)
            a.triggered.connect(slot)
            self._table_menu.addAction(a)
            return a

        add("Add row above", lambda: self._table_op("add_row", above=True))
        add("Add row below", lambda: self._table_op("add_row", above=False))
        add("Delete row", lambda: self._table_op("delete_row"))
        self._table_menu.addSeparator()
        add("Add column left", lambda: self._table_op("add_column", left=True))
        add("Add column right", lambda: self._table_op("add_column", left=False))
        add("Delete column", lambda: self._table_op("delete_column"))
        self._table_menu.addSeparator()
        add(
            "Merge cells",
            lambda: self._table_op("merge_selection"),
            enabled=ed is not None and ed.selection_is_mergeable(),
        )
        add(
            "Split cell",
            lambda: self._table_op("split_selection"),
            enabled=ed is not None and ed.current_is_merged(),
        )
        self._table_menu.addSeparator()
        add(
            "Header row",
            lambda: self._table_op("toggle_header_row"),
            checkable=True,
            checked=ed is not None and ed.current_header(),
        )
        align_menu = self._table_menu.addMenu("Column alignment")
        align_menu.setEnabled(writable)
        cur_align = ed.current_alignment() if ed is not None else "left"
        for name in ("left", "center", "right"):
            a = QAction(name.capitalize(), align_menu)
            a.setCheckable(True)
            a.setChecked(name == cur_align)
            a.triggered.connect(
                lambda _checked=False, n=name: self._table_op("set_column_alignment", n)
            )
            align_menu.addAction(a)
        self._table_menu.addSeparator()
        remove_act = QAction("Remove table", self._table_menu)
        remove_act.setEnabled(writable)
        remove_act.triggered.connect(self._table_remove)
        self._table_menu.addAction(remove_act)

    def _table_insert(self) -> None:
        doc_id, obj_id, _ = self._selected_object()
        if doc_id is None or obj_id is None or not self._prepare_edit():
            return
        self.grid_pane.insert_table(doc_id, obj_id)

    def _table_remove(self) -> None:
        doc_id, obj_id, _ = self._selected_object()
        if doc_id is None or obj_id is None or not self._prepare_edit():
            return
        self.grid_pane.remove_table(doc_id, obj_id)

    def _table_op(self, method: str, *args, **kwargs) -> None:
        """Run a structural op on the inline table editor, opening it first."""
        if self._read_only:
            return
        if self.grid_pane.is_stale():
            self._status.showMessage(STALE_EDIT_MESSAGE, 6000)
            return
        doc_id, obj_id, obj = self._selected_object()
        if obj is None or obj.table is None:
            return
        if self._live_editor() is None:
            # Not the grid flush (that would close the editor this op is
            # aimed at). Inspector text is committed before the editor
            # opens: committed on the focus-out the opening causes, its
            # reload would drop the new editor.
            self.inspector.commit_pending()
            self.grid_pane.begin_table_edit(obj_id)
        ed = self._live_editor()
        if ed is None:
            return
        getattr(ed, method)(*args, **kwargs)

    def _build_views_button(self) -> QToolButton:
        button = QToolButton(self)
        button.setText("Views")
        button.setIcon(icons.icon("views"))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        # MenuButtonPopup draws Qt's native split-button chevron on the right
        # side; clicking anywhere on the button opens the menu (we wire the
        # main-area click below since we have no separate default action).
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.setToolTip("Saved attribute-visibility views")
        menu = QMenu(button)
        menu.aboutToShow.connect(self._populate_views_menu)
        button.setMenu(menu)
        button.clicked.connect(button.showMenu)
        self._views_menu = menu
        return button

    def _populate_views_menu(self) -> None:
        self._views_menu.clear()
        try:
            doc = self.controller.document(self._doc_id)
        except (ServiceError, KeyError):
            doc = None

        active_slug = self.grid_pane.current_active_view_slug()

        if doc is not None and doc.views:
            for view in doc.views:
                act_view = QAction(view.name, self._views_menu)
                act_view.setCheckable(True)
                act_view.setChecked(view.slug == active_slug)
                slug = view.slug
                visible = tuple(view.visible)
                act_view.triggered.connect(
                    lambda _checked=False, s=slug, v=visible: self._apply_view(s, v)
                )
                self._views_menu.addAction(act_view)
            self._views_menu.addSeparator()

        new_act = QAction("New view…", self._views_menu)
        new_act.triggered.connect(self._new_view)
        new_act.setEnabled(not self._read_only)
        self._views_menu.addAction(new_act)

        manage_act = QAction("Manage views…", self._views_menu)
        manage_act.triggered.connect(self._manage_views)
        manage_act.setEnabled(
            not self._read_only and doc is not None and bool(doc.views)
        )
        self._views_menu.addAction(manage_act)

    def _apply_view(self, slug: str, visible: tuple[str, ...]) -> None:
        self.grid_pane.apply_view(slug, visible)
        self._status.showMessage(f"Applied view {slug!r}", 3000)

    def _new_view(self) -> None:
        if self._read_only:
            return
        from .dialogs.new_view_dialog import NewViewDialog

        try:
            doc = self.controller.document(self._doc_id)
        except (ServiceError, KeyError):
            return
        attributes = self.grid_pane.all_user_attribute_names()
        existing = {v.name for v in doc.views}
        dlg = NewViewDialog(
            attributes=attributes,
            pre_checked=self.grid_pane.current_visible_attrs(),
            existing_names=existing,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            slug = self.controller.create_view(
                self._doc_id, dlg.view_name(), tuple(dlg.visible_attrs())
            )
        except (ServiceError, ValueError) as exc:
            QMessageBox.warning(self, "Create view failed", str(exc))
            return
        # Seed the new bucket with the current header layout, then activate
        # the view (which restores those widths after applying visibility).
        self.grid_pane.snapshot_widths_to(slug)
        self._apply_view(slug, tuple(dlg.visible_attrs()))

    def _manage_views(self) -> None:
        if self._read_only:
            return
        from .dialogs.manage_views_dialog import ManageViewsDialog

        dlg = ManageViewsDialog(self.controller, self._doc_id, self)
        dlg.exec()

    # --- saved filters --------------------------------------------------

    _ACTIVE_FILTER_PREFIX = "grid/active_filter"

    def _build_filters_button(self) -> QToolButton:
        button = QToolButton(self)
        button.setText("Filters")
        button.setIcon(icons.icon("suspect_filter"))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        button.setToolTip("Saved DOORS-style filters (Ctrl+Shift+F)")
        menu = QMenu(button)
        menu.aboutToShow.connect(self._populate_filters_menu)
        button.setMenu(menu)
        button.clicked.connect(button.showMenu)
        self._filters_menu = menu
        return button

    def _populate_filters_menu(self) -> None:
        self._filters_menu.clear()
        try:
            doc = self.controller.document(self._doc_id)
        except (ServiceError, KeyError):
            doc = None

        active = self.grid_pane.active_filter()
        active_slug = active.slug if active is not None else None

        none_act = QAction("(none)", self._filters_menu)
        none_act.setCheckable(True)
        none_act.setChecked(active_slug is None)
        none_act.triggered.connect(lambda _checked=False: self.grid_pane.clear_active_filter())
        self._filters_menu.addAction(none_act)

        if doc is not None and doc.filters:
            self._filters_menu.addSeparator()
            for filt in doc.filters:
                act_filter = QAction(filt.name, self._filters_menu)
                act_filter.setCheckable(True)
                act_filter.setChecked(filt.slug == active_slug)
                slug = filt.slug
                act_filter.triggered.connect(
                    lambda _checked=False, s=slug: self._apply_filter_by_slug(s)
                )
                self._filters_menu.addAction(act_filter)

        self._filters_menu.addSeparator()

        new_act = QAction("New filter…", self._filters_menu)
        new_act.triggered.connect(lambda: self._open_filter_dialog(seed=None))
        new_act.setEnabled(not self._read_only)
        self._filters_menu.addAction(new_act)

        edit_act = QAction("Edit current filter…", self._filters_menu)
        edit_act.triggered.connect(self._open_filter_dialog)
        edit_act.setEnabled(not self._read_only and active is not None)
        self._filters_menu.addAction(edit_act)

        manage_act = QAction("Manage filters…", self._filters_menu)
        manage_act.triggered.connect(self._manage_filters)
        manage_act.setEnabled(
            not self._read_only and doc is not None and bool(doc.filters)
        )
        self._filters_menu.addAction(manage_act)

    def _manage_filters(self) -> None:
        if self._read_only:
            return
        from .dialogs.manage_filters_dialog import ManageFiltersDialog

        dlg = ManageFiltersDialog(self.controller, self._doc_id, self)
        dlg.exec()

    def _apply_filter_by_slug(self, slug: str) -> None:
        try:
            doc = self.controller.document(self._doc_id)
        except (ServiceError, KeyError):
            return
        for filt in doc.filters:
            if filt.slug == slug:
                # apply_filter emits active_filter_changed; persistence
                # happens in _on_active_filter_changed.
                self.grid_pane.apply_filter(filt)
                self._status.showMessage(f"Applied filter {filt.name!r}", 3000)
                return

    _SEED_UNSET = object()

    def _open_filter_dialog(self, *_args, seed=_SEED_UNSET) -> None:
        from .dialogs.filter_dialog import FilterDialog

        try:
            doc = self.controller.document(self._doc_id)
        except (ServiceError, KeyError):
            return

        # `seed` distinguishes three intents from the same entry point:
        #   - omitted (Ctrl+Shift+F / pill Edit / Edit current filter…) →
        #     edit the currently-active filter, falling back to fresh
        #   - explicit None (New filter…) → always start fresh
        #   - an actual Filter instance → edit that specific one
        if seed is self._SEED_UNSET:
            seed = self.grid_pane.active_filter()

        # Discover sibling docs so the link-rule target combo can list every
        # known doc id; fall back gracefully if no service.
        try:
            doc_ids = tuple(sorted(self.controller.docs().keys()))
        except Exception:
            doc_ids = (self._doc_id,)

        existing = {f.name for f in doc.filters}

        dlg = FilterDialog(
            attributes=tuple(doc.attributes),
            all_doc_ids=doc_ids,
            existing_filter_names=existing,
            initial=seed,
            parent=self,
            read_only=self._read_only,
        )
        # `seed` lives in a single-cell dict so the OK callback (which can
        # only fire once per dialog instance, but defensive anyway) and the
        # Apply callback share a consistent view of "what filter are we
        # editing right now?"
        state = {"seed": seed}

        def on_apply(f):
            self._dialog_apply(f, state["seed"])

        def on_save_and_apply(f):
            saved = self._dialog_save(f, state["seed"])
            if saved is not None:
                state["seed"] = saved
                # Save itself already applied the persisted filter — the
                # in-memory `f` has no slug, so re-applying it would clobber
                # the pill's slug-bearing summary.
            else:
                # Save failed (read-only / error). Fall back to a transient
                # apply so the user at least sees the rules in effect.
                self._dialog_apply(f, state["seed"])

        dlg.apply_requested.connect(on_apply)
        dlg.save_and_apply_requested.connect(on_save_and_apply)
        dlg.exec()

    def _dialog_apply(self, filt, seed) -> None:
        # Live preview without disk: hand the in-memory Filter to the grid.
        # If the dialog is editing a saved filter, preserve its slug so the
        # pill keeps showing it as that filter.
        if seed is not None and not filt.slug:
            filt = type(filt)(
                slug=seed.slug,
                name=filt.name or seed.name,
                combinator=filt.combinator,
                keep_ancestors=filt.keep_ancestors,
                rules=filt.rules,
            )
        # apply_filter emits active_filter_changed — _on_active_filter_changed
        # mirrors filt.slug into QSettings (or clears the entry when slug=="").
        self.grid_pane.apply_filter(filt)

    def _dialog_save(self, filt, seed):
        """Persist the filter; return the on-disk Filter, or None on failure.

        The dialog launcher updates its mutable seed with the return value so
        that subsequent Save/OK clicks update this filter instead of creating
        a fresh one.
        """
        if self._read_only:
            QMessageBox.warning(
                self, "Read-only", "Read-only mode — filters can't be saved."
            )
            return None
        try:
            if seed is None or not seed.slug:
                new_slug = self.controller.create_filter(
                    self._doc_id, filt.name, filt.combinator,
                    filt.keep_ancestors, filt.rules,
                )
            else:
                # If the display name changed, rename the file first.
                if filt.name != seed.name:
                    new_slug = self.controller.rename_filter(
                        self._doc_id, seed.slug, filt.name
                    )
                else:
                    new_slug = seed.slug
                self.controller.set_filter_rules(
                    self._doc_id, new_slug, filt.combinator,
                    filt.keep_ancestors, filt.rules,
                )
        except (ServiceError, ValueError) as exc:
            QMessageBox.warning(self, "Save filter failed", str(exc))
            return None

        self._status.showMessage(f"Saved filter {filt.name!r}", 3000)
        try:
            doc = self.controller.document(self._doc_id)
        except (ServiceError, KeyError):
            return None
        for f in doc.filters:
            if f.slug == new_slug:
                # apply_filter emits active_filter_changed; QSettings sync
                # happens in _on_active_filter_changed.
                self.grid_pane.apply_filter(f)
                return f
        return None

    def _on_filters_changed(self, doc_id: str) -> None:
        # Rebuild the toolbar menu lazily; nothing to do here unless the
        # current doc is affected and the active filter no longer exists.
        if doc_id != self._doc_id:
            return
        active = self.grid_pane.active_filter()
        if active is None:
            return
        try:
            doc = self.controller.document(doc_id)
        except (ServiceError, KeyError):
            return
        slug_set = {f.slug for f in doc.filters}
        if active.slug and active.slug not in slug_set:
            # clear_active_filter → apply_filter(None) → active_filter_changed
            # → _on_active_filter_changed clears the QSettings entry.
            self.grid_pane.clear_active_filter()

    def _restore_active_filter(self, doc_id: str) -> None:
        # Re-apply whatever filter the user had active for this doc last time.
        slug = self._read_active_filter(doc_id)
        if not slug:
            self.grid_pane.clear_active_filter()
            return
        try:
            doc = self.controller.document(doc_id)
        except (ServiceError, KeyError):
            return
        for f in doc.filters:
            if f.slug == slug:
                self.grid_pane.apply_filter(f)
                return
        # Slug saved but no longer present — apply_filter(None) clears both
        # the in-session state and the QSettings entry via the signal.
        self.grid_pane.clear_active_filter()

    def _read_active_filter(self, doc_id: str) -> str:
        from . import settings as _settings
        s = _settings.make_settings()
        return str(s.value(f"{self._ACTIVE_FILTER_PREFIX}/{doc_id}", "") or "")

    def _persist_active_filter(self, doc_id: str, slug: str) -> None:
        from . import settings as _settings
        s = _settings.make_settings()
        if slug:
            s.setValue(f"{self._ACTIVE_FILTER_PREFIX}/{doc_id}", slug)
        else:
            s.remove(f"{self._ACTIVE_FILTER_PREFIX}/{doc_id}")

    def _on_active_filter_changed(self, filt) -> None:
        """Mirror the grid's active filter into QSettings.

        Called after every `GridPane.apply_filter` so pill `[×]`, `(none)`
        menu, dialog OK, and `Filter ▸ <name>` clicks all keep the
        per-document `grid/active_filter/<doc_id>` entry in sync. None
        clears the entry; a Filter with a slug writes that slug.

        Uses `controller.current_doc_id()` rather than `self._doc_id` so
        filter changes that happen while the grid is showing a sibling doc
        (e.g. via cross-doc navigation) land against the right QSettings
        entry. `_restore_active_filter` is the one path that depends on
        this distinction — it runs `clear_active_filter()` for the doc
        being switched in, and the change must persist for *that* doc, not
        for the window's primary doc.
        """
        doc_id = self.controller.current_doc_id() or self._doc_id
        if not doc_id:
            return
        slug = filt.slug if filt is not None else ""
        self._persist_active_filter(doc_id, slug)

    def _show_document_properties(self) -> None:
        if not self._prepare_edit():
            return
        from .dialogs.document_properties import DocumentPropertiesDialog

        dlg = DocumentPropertiesDialog(self.controller, self._doc_id, self)
        dlg.exec()
        # Refresh window title in case the metadata title changed.
        try:
            doc = self.controller.document(self._doc_id)
        except ServiceError:
            return
        doc_name = doc.metadata.title or self._doc_id
        self.setWindowTitle(f"{doc_name} — Document Explorer — Mellon")

    def _commit_current_doc(self) -> None:
        if not self._prepare_edit():
            return
        doc_id = self.controller.current_doc_id()
        if doc_id is None:
            self._status.showMessage("No document selected.", 4000)
            return
        try:
            errors = self.controller.require_service().validate_document(doc_id)
        except ServiceError as e:
            self._status.showMessage(f"Commit unavailable: {e}", 6000)
            return
        if errors:
            shown = "\n".join(str(err) for err in errors[:10])
            if len(errors) > 10:
                shown += f"\n… and {len(errors) - 10} more"
            QMessageBox.critical(
                self,
                "Cannot commit — validation failed",
                f"{doc_id} has {len(errors)} validation error(s). "
                f"Fix them before committing.\n\n{shown}",
            )
            self._status.showMessage(
                f"{doc_id}: commit blocked by {len(errors)} validation error(s).",
                6000,
            )
            return
        try:
            preview = self.controller.prepare_document_commit(doc_id)
        except ServiceError as e:
            self._status.showMessage(f"Commit unavailable: {e}", 6000)
            return
        dlg = CommitDialog(preview, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._status.showMessage("Commit cancelled.", 3000)
            return
        if dlg.tag_only():
            self._tag_current_commit(doc_id, dlg.tag(), dlg.tag_message())
            return
        message = dlg.message()
        if not message:
            self._status.showMessage("Empty commit message — aborted.", 4000)
            return
        svc = self.controller.require_service()
        meta_updates = (
            ("author", dlg.author(), preview.current_author),
            ("date", dlg.date(), preview.current_date),
        )
        # The metadata has to be on disk to land in this commit (it may be the
        # only change), so it's written first — and the manifest is put back
        # byte-for-byte if the commit doesn't happen, so a failed commit
        # leaves no stray manifest edit behind.
        manifest = self.controller.folder(doc_id) / f"{doc_id}.xml"
        manifest_before = manifest.read_bytes()
        head_before = self._head_id()
        undo_mark = len(self._undo_steps)

        def restore_manifest() -> None:
            # Only while nothing was committed: once HEAD moved, the commit
            # may hold the manifest as it is now, and putting the old bytes
            # back would revert it with the next commit.
            if self._head_id() != head_before:
                return
            if manifest.read_bytes() != manifest_before:
                manifest.write_bytes(manifest_before)
                # The metadata writes are undone by hand: drop their steps.
                del self._undo_steps[undo_mark:]
                self._refresh_undo_actions()
                self.controller.reload_document(doc_id)

        for field, new_val, current in meta_updates:
            if new_val == current:
                continue
            try:
                svc.set_metadata(doc_id, field, new_val)
            except ServiceError as e:
                restore_manifest()
                self._status.showMessage(
                    f"Metadata save failed ({field}): {e}", 8000
                )
                return
        # set_metadata is the one GUI write that doesn't go through
        # reload_document; re-snapshot so it doesn't read as an external edit.
        self.controller.refresh_disk_snapshot()
        try:
            # The issue field is prefilled with the current issue: left as it
            # is (or empty), the service cuts no tag.
            result = self.controller.commit_document(
                doc_id,
                message,
                new_issue=dlg.issue(),
                tag=dlg.tag(),
                tag_message=dlg.tag_message(),
            )
        except ServiceError as e:
            restore_manifest()
            self._status.showMessage(f"Commit failed: {e}", 8000)
            # In full: e.g. a missing git identity comes with the `git
            # config` commands to run, too long for the status bar.
            QMessageBox.critical(self, "Commit failed", str(e))
            return
        except Exception:
            # Not every git failure is a ServiceError (another client holding
            # index.lock can surface as a raw pygit2.GitError): roll back the
            # same way and leave the error to the excepthook.
            restore_manifest()
            raise
        affected = len(preview.affected_req_ids)
        files = len(preview.paths)
        suffix = (
            f" ({affected} requirement(s) affected)" if affected else ""
        )
        tags = [t for t in (result.tag_name, result.repo_tag) if t]
        tag_errors = [e for e in (result.tag_error, result.repo_tag_error) if e]
        if tags:
            label = "tag" if len(tags) == 1 else "tags"
            committed = f"Committed {result.short_hash} + {label} {', '.join(tags)}"
        else:
            committed = f"Committed {result.short_hash}"
        if tag_errors:
            self._status.showMessage(
                f"{committed} — {files} file(s){suffix}. "
                f"Tag failed: {'; '.join(tag_errors)}",
                8000,
            )
        else:
            self._status.showMessage(
                f"{committed} — {files} file(s){suffix}.", 6000
            )
        self._set_read_only(True)

    def _tag_current_commit(self, doc_id: str, name: str, message: str) -> None:
        """The commit dialog's Tag with nothing to commit: tag HEAD."""
        try:
            self.controller.tag_head(doc_id, name, message)
        except ServiceError as e:
            self._status.showMessage(f"Tag failed: {e}", 8000)
            QMessageBox.critical(self, "Tag failed", str(e))
            return
        self._status.showMessage(f"Tagged the current commit {name}.", 6000)
        self._set_read_only(True)

    def _head_id(self) -> str | None:
        """HEAD's commit id in this document's repo (None without one)."""
        try:
            repo, _ = discover_repo(self.controller.folder(self._doc_id))
            return None if repo.head_is_unborn else str(repo.head.target)
        except Exception:  # noqa: BLE001 — no repo, or HEAD unreadable
            return None

    def _refresh_discard_action(self) -> None:
        """Enable "Discard uncommitted changes…" for a writable document that
        has uncommitted changes and a last commit to go back to."""
        enabled = not self._read_only
        if enabled:
            try:
                enabled = self.controller.require_service().has_uncommitted_changes(
                    self._doc_id
                ) and self.controller.in_last_commit(self._doc_id)
            except (ServiceError, RuntimeError):
                enabled = False
        self._discard_act.setEnabled(enabled)

    def _discard_document_changes(self) -> None:
        """Put this document's folder back to its last commit, after asking.

        Allowed while the "Changed on disk" banner is up: the files come back
        from git, not from this window's cached copy, and the reload that
        follows brings the window up to date either way.
        """
        if self._read_only:
            return
        doc_id = self._doc_id
        svc = self.controller.require_service()
        if not svc.has_uncommitted_changes(doc_id):
            self._status.showMessage(f"{doc_id} has no uncommitted changes.", 4000)
            return
        if not self.controller.in_last_commit(doc_id):
            # Checked before asking: the discard itself would refuse.
            self._status.showMessage(
                f"{doc_id} has never been committed, so there is no committed "
                "version to go back to.",
                6000,
            )
            return
        choice = QMessageBox.question(
            self,
            "Discard uncommitted changes",
            f"All edits since the last commit to {doc_id} will be lost, "
            "including new objects.\n\n"
            "Edited objects, links, attributes, document properties, views "
            "and filters go back to their last committed version. Objects, "
            "links, figures, views and filters added since then are deleted. "
            "Other documents are not touched: their links to objects deleted "
            "here are left dangling.\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Discard:
            self._status.showMessage("Discard cancelled.", 3000)
            return
        # Write pending typed text first, so it is discarded with the rest:
        # left pending, the reload's own flush would write it back over the
        # restored files.
        self.flush_pending_edits()
        error: ServiceError | None = None
        try:
            restored = svc.discard_document_changes(doc_id)
        except ServiceError as e:
            error = e
        # Also after a failure: the folder may be partly restored.
        self._reload_from_disk()
        if error is not None:
            self._status.showMessage(f"Discard failed: {error}", 8000)
            QMessageBox.critical(self, "Discard failed", str(error))
            return
        self._status.showMessage(
            f"Discarded uncommitted changes to {doc_id} "
            f"({len(restored)} file(s) back to the last commit).",
            6000,
        )

    def is_exporting(self) -> bool:
        """True while a PDF export started from this window is running."""
        return self._export_signals is not None

    def _link_problems(self, doc_id: str) -> list[LinkStatus]:
        """The document's suspect and broken outgoing links; none when the
        scan can't run (no git)."""
        try:
            scan = self.controller.require_service().scan_suspects(doc_id)
        except ServiceError:
            return []
        return [s for s in scan[doc_id] if s.state is not LinkState.OK]

    def _ask_export_despite_links(
        self, doc_id: str, problems: list[LinkStatus]
    ) -> bool:
        """Warn about suspect / broken links before an export; True to go on."""
        n_broken = sum(s.state is LinkState.BROKEN for s in problems)
        n_suspect = len(problems) - n_broken
        shown = [
            f"{s.source_id} → {s.link.target_doc}/{s.link.target_id}: "
            f"{s.state.value}"
            for s in problems[:_LINK_WARNING_ROWS]
        ]
        if len(problems) > _LINK_WARNING_ROWS:
            shown.append(f"… and {len(problems) - _LINK_WARNING_ROWS} more")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Suspect links")
        box.setText(
            f"{doc_id} has {n_suspect} suspect and {n_broken} broken "
            "link(s). Review them before issuing the document."
        )
        box.setInformativeText("\n".join(shown))
        export = box.addButton("Export anyway", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is export

    def _export_current_doc(self) -> None:
        if self.is_exporting():
            self._status.showMessage("An export is already running.", 4000)
            return
        doc_id = self.controller.current_doc_id()
        if doc_id is None:
            self._status.showMessage("No document selected.", 4000)
            return
        svc = self.controller.require_service()
        # Ctrl+E and the toolbar button don't take focus: write pending typed
        # text first, so the PDF has what the screen shows.
        if not self._read_only and not self.grid_pane.is_stale():
            self.flush_pending_edits()
        try:
            loaded = svc.get_loaded_document(doc_id)
            # The PDF ends with a Traceability section against the parents.
            parents = svc.parent_documents(doc_id)
        except ServiceError as e:
            self._status.showMessage(f"Export unavailable: {e}", 6000)
            return
        # The PDF shows no link states, so suspect links are caught here.
        problems = self._link_problems(doc_id)
        if problems and not self._ask_export_despite_links(doc_id, problems):
            self._status.showMessage("Export cancelled.", 3000)
            return
        rev = svc.current_issue(doc_id) or None

        s = make_settings()
        last_dir = s.value(f"{_EXPORT_DIR_KEY}/{doc_id}") or ""
        default_name = f"{doc_id}.pdf"
        default_path = (
            str(Path(last_dir) / default_name) if last_dir else default_name
        )
        target, _ = QFileDialog.getSaveFileName(
            self, "Export to PDF", default_path, "PDF (*.pdf)"
        )
        if not target:
            self._status.showMessage("Export cancelled.", 3000)
            return
        if not target.lower().endswith(".pdf"):
            target += ".pdf"

        signals = _ExportSignals()
        signals.finished.connect(self._on_export_finished)
        signals.failed.connect(self._on_export_failed)
        signals.crashed.connect(self._on_export_crashed)
        self._export_signals = signals
        self._export_ctx = (doc_id, target)
        self._export_act.setEnabled(False)
        self.setCursor(Qt.CursorShape.BusyCursor)
        self._status.showMessage(f"Exporting {doc_id} to PDF…", 0)
        QThreadPool.globalInstance().start(
            _ExportJob(
                signals, loaded.document, loaded.folder, rev, Path(target),
                parents=parents,
            )
        )

    def _end_export(self) -> tuple[str, str]:
        """Clear the running-export state; return its (doc_id, target)."""
        ctx = self._export_ctx
        signals = self._export_signals
        self._export_ctx = None
        self._export_signals = None
        if signals is not None:
            signals.deleteLater()
        self.unsetCursor()
        self._export_act.setEnabled(self.controller.current_doc_id() is not None)
        assert ctx is not None
        return ctx

    def _on_export_finished(self, size: int, missing: list) -> None:
        doc_id, target = self._end_export()
        make_settings().setValue(
            f"{_EXPORT_DIR_KEY}/{doc_id}", str(Path(target).parent)
        )
        self._status.showMessage(
            f"Exported {doc_id} → {target} ({size} bytes).", 6000
        )
        if missing:
            if find_drawio() is None:
                why = (
                    "PDF export draws figures with draw.io desktop, which Mellon "
                    f"couldn't find. Install it from {DOWNLOAD_URL} (or set "
                    "MELLON_DRAWIO to its path) and export again."
                )
            else:
                why = "draw.io couldn't render them; an empty figure is one reason."
            QMessageBox.information(
                self,
                "Figures not in the PDF",
                f"Exported {doc_id} to {target}.\n\n"
                "The figures of these objects are left out (a placeholder line "
                "marks each):\n\n" + ", ".join(missing) + f"\n\n{why}",
            )

    def _on_export_failed(self, message: str) -> None:
        self._end_export()
        self._status.showMessage("Export failed.", 6000)
        QMessageBox.critical(self, "Export failed", message)

    def _on_export_crashed(self, error: object) -> None:
        self._end_export()
        self._status.showMessage("Export failed.", 6000)
        # Unexpected: leave it to the excepthook (log file + dialog).
        raise error  # type: ignore[misc]

    def _validate_current_doc(self) -> None:
        doc_id = self.controller.current_doc_id()
        if doc_id is None:
            self._status.showMessage("No document selected.", 4000)
            return
        try:
            errors = self.controller.require_service().validate_document(doc_id)
        except ServiceError as e:
            self._status.showMessage(f"Validation unavailable: {e}", 6000)
            return
        if not errors:
            self._status.showMessage(f"{doc_id}: validation passed.", 4000)
            return
        shown = "\n".join(str(err) for err in errors[:10])
        if len(errors) > 10:
            shown += f"\n… and {len(errors) - 10} more"
        QMessageBox.warning(
            self,
            "Validation failed",
            f"{doc_id} has {len(errors)} validation error(s).\n\n{shown}",
        )
        self._status.showMessage(
            f"{doc_id}: {len(errors)} validation error(s).", 6000
        )

    def _refresh_git_cell(self) -> None:
        root = self.controller.root
        self._git_head = None
        if root is None:
            self._git_cell.setText("")
            self._git_cell.setToolTip("")
            return
        try:
            repo, _ = discover_repo(root)
        except GitError:
            self._git_cell.setText("")
            self._git_cell.setToolTip("not inside a git repo")
            return
        branch, short = head_summary(repo)
        self._git_head = short
        if short is None:
            text = "(no commits)"
        elif branch is None:
            text = f"detached @ {short}"
        else:
            text = f"{branch} @ {short}"
        try:
            n = dirty_count(repo)
        except Exception:  # noqa: BLE001
            n = 0
        if n:
            text += f" · {n} dirty"
        self._git_cell.setText(text)
        self._git_cell.setToolTip(
            "HEAD is detached. Commits made here won't be on any branch."
            if branch is None and short is not None
            else f"{n} file(s) with uncommitted changes" if n else "working tree clean"
        )


# Keep old name as an alias so any external code or tests that import MainWindow
# still work during the transition.
MainWindow = DocumentEditorWindow
