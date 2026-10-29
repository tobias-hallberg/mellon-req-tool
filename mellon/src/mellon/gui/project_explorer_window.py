"""ProjectExplorerWindow — the main/primary window of the application.

One instance per repo. Closing it quits the app. Double-clicking a document
opens (or focuses) a DocumentEditorWindow for that document.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import merge
from ..git_ops import GitError, dirty_count, discover_repo, head_summary
from ..repo import doc_folder_names
from ..service import ServiceError
from . import branding, icons
from .controller import RepoController, describe_load_error
from .dialogs.about_dialog import show_about
from .dialogs.merge_dialog import MergeDialog
from .dialogs.new_document import NewDocumentDialog
from .main_window import DocumentEditorWindow
from .models.project_tree import ProjectTreeModel
from .settings import make_settings
from .theme import ThemeManager, build_theme_menu


_DOC_ID_ROLE = Qt.ItemDataRole.UserRole + 1
_GEOMETRY_KEY = "project_explorer/geometry"


class MergeBanner(QWidget):
    """Amber banner while a git merge is in progress: how many files still
    conflict, and a button to resolve them (`resolve_clicked`)."""

    resolve_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Same scoped-stylesheet pattern as the editor's banners.
        self.setObjectName("MergeBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._label = QLabel()
        self._label.setObjectName("MergeBannerLabel")
        self._label.setWordWrap(True)
        self._button = QPushButton("Resolve merge…")
        self._button.clicked.connect(self.resolve_clicked.emit)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._button)
        self._apply_theme_tokens(ThemeManager.instance().current_tokens())
        ThemeManager.instance().theme_changed.connect(self._apply_theme_tokens)
        self.setVisible(False)

    def show_state(self, conflicts: tuple[str, ...] | None) -> None:
        """`merge.pending`'s answer: None hides the banner."""
        if conflicts is None:
            self.setVisible(False)
            return
        if conflicts:
            self._label.setText(
                f"<b>Merge in progress.</b> {len(conflicts)} file(s) conflict."
            )
        else:
            self._label.setText(
                "<b>Merge in progress.</b> No conflicts left: make the merge "
                "commit in your git client."
            )
        self._button.setVisible(bool(conflicts))
        self.setVisible(True)

    def _apply_theme_tokens(self, tokens: dict) -> None:
        self.setStyleSheet(
            f"QWidget#MergeBanner {{ background: {tokens['warn_bg']}; "
            f"border: 1px solid {tokens['warn']}; border-radius: 3px; }}"
            f"QLabel#MergeBannerLabel {{ background: transparent; "
            f"border: none; color: {tokens['fg_0']}; font-size: 12px; }}"
        )


class ProjectExplorerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Project Explorer — Mellon")
        self.setWindowIcon(branding.window_icon())
        self.resize(340, 820)

        self.controller = RepoController(self)
        self._editor_windows: dict[str, DocumentEditorWindow] = {}
        # Top-level doc-folder names seen at the last load/refresh; compared
        # on window activation to pick up docs created or deleted externally.
        self._known_doc_folders: frozenset[str] | None = None

        # --- Documents section ------------------------------------------
        self._project_model = ProjectTreeModel(self.controller)
        self._project_view = QTreeView()
        self._project_view.setModel(self._project_model)
        self._project_view.setHeaderHidden(True)
        self._project_view.setRootIsDecorated(False)
        self._project_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._project_view.activated.connect(self._on_doc_activated)

        docs_widget = QWidget()
        docs_layout = QVBoxLayout(docs_widget)
        docs_layout.setContentsMargins(0, 0, 0, 0)
        docs_layout.setSpacing(0)
        docs_label = QLabel("DOCUMENTS")
        docs_label.setObjectName("PaneHeader")
        docs_layout.addWidget(docs_label)
        docs_layout.addWidget(self._project_view)

        # --- Open section -----------------------------------------------
        self._open_list = QListWidget()
        self._open_list.activated.connect(self._on_open_item_activated)

        open_widget = QWidget()
        open_layout = QVBoxLayout(open_widget)
        open_layout.setContentsMargins(0, 0, 0, 0)
        open_layout.setSpacing(0)
        open_label = QLabel("OPEN")
        open_label.setObjectName("PaneHeader")
        open_layout.addWidget(open_label)
        open_layout.addWidget(self._open_list)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(docs_widget)
        splitter.addWidget(open_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        self.merge_banner = MergeBanner(self)
        self.merge_banner.resolve_clicked.connect(self._resolve_merge)
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.merge_banner)
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        # --- Status bar -------------------------------------------------
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("Open a repository to get started — File → Open Repo…")
        self._git_cell = QLabel()
        self._git_cell.setStyleSheet("padding: 0 6px; color: palette(mid);")
        self._status.addPermanentWidget(self._git_cell)

        # --- Menu + Toolbar --------------------------------------------
        self._main_menu = self._build_menus()
        self._build_toolbar()
        self._install_hamburger()

        # --- Controller wiring ------------------------------------------
        self.controller.repo_loaded.connect(self._on_repo_loaded)
        self.controller.repo_load_failed.connect(self._on_repo_load_failed)
        self.controller.repo_loaded.connect(lambda *_: self._refresh_git_cell())
        self.controller.repo_loaded.connect(lambda *_: self._act_new_document.setEnabled(True))
        # A refused folder can leave the previous repo open.
        self.controller.repo_load_failed.connect(
            lambda *_: self._act_new_document.setEnabled(self.controller.service is not None)
        )

        # --- Restore last geometry --------------------------------------
        geom = make_settings().value(_GEOMETRY_KEY)
        if isinstance(geom, (bytes, bytearray, QByteArray)):
            self.restoreGeometry(geom)

    # --- toolbar --------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = QToolBar("Project", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(24, 24))
        self.addToolBar(tb)
        self._toolbar = tb

        self._act_new_document = QAction(icons.icon("new_document"), "Document", self)
        self._act_new_document.setShortcut(QKeySequence("Ctrl+N"))
        self._act_new_document.setToolTip("New document (Ctrl+N)")
        self._act_new_document.triggered.connect(self._new_document)
        self._act_new_document.setEnabled(False)
        tb.addAction(self._act_new_document)

        ThemeManager.instance().theme_changed.connect(self._reapply_toolbar_icons)

    def _reapply_toolbar_icons(self, *_args) -> None:
        self._act_new_document.setIcon(icons.icon("new_document"))
        if hasattr(self, "_hamburger_btn"):
            self._hamburger_btn.setIcon(icons.icon("menu"))

    # --- menu -----------------------------------------------------------

    def _build_menus(self) -> QMenu:
        menu = QMenu(self)
        menu.setMinimumWidth(260)

        open_act = QAction("&Open Repository…", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self._prompt_open_repo)
        # Window-rooted action so the shortcut fires without a menu bar.
        self.addAction(open_act)
        menu.addAction(open_act)

        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        self.addAction(quit_act)
        menu.addAction(quit_act)

        menu.addSection("View")
        self._refresh_act = QAction("&Refresh", self)
        self._refresh_act.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self._refresh_act.setToolTip("Re-read every document from disk")
        self._refresh_act.triggered.connect(self._refresh_documents)
        self.addAction(self._refresh_act)
        menu.addAction(self._refresh_act)

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
        existing = tb.actions()
        if existing:
            first = existing[0]
            tb.insertWidget(first, ham)
            tb.insertSeparator(first)
        else:
            tb.addWidget(ham)
            tb.addSeparator()
        self._hamburger_btn = ham

    # --- disk freshness ---------------------------------------------------

    def event(self, e: QEvent) -> bool:  # type: ignore[override]
        # Coming back to the explorer: pick up documents another process
        # created or deleted, and refresh the git status cell.
        if (
            e.type() == QEvent.Type.WindowActivate
            and getattr(self, "controller", None) is not None
        ):
            self._check_doc_list_freshness()
            self._refresh_git_cell()
            self._refresh_merge_banner()
        return super().event(e)

    # --- merges -----------------------------------------------------------

    def _refresh_merge_banner(self) -> None:
        root = self.controller.root
        self.merge_banner.show_state(merge.pending(root) if root else None)

    def _resolve_merge(self) -> None:
        root = self.controller.root
        if root is None:
            return
        try:
            plan = merge.plan(root)
        except merge.MergeError as e:
            self._refresh_merge_banner()
            QMessageBox.warning(self, "Resolve merge", str(e))
            return
        dlg = MergeDialog(root, plan, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._refresh_documents()
        self._status.showMessage(
            f"Resolved {len(dlg.resolved())} file(s). Make the merge commit "
            "in your git client.",
            10000,
        )

    def _check_doc_list_freshness(self) -> None:
        root = self.controller.root
        if root is None or self._known_doc_folders is None:
            return
        fresh = doc_folder_names(root)
        if fresh == self._known_doc_folders:
            return
        svc = self.controller.service
        if svc is not None and svc.cache is not None:
            svc.cache.invalidate_all()
        self._project_model.refresh()
        self._known_doc_folders = fresh
        self._status.showMessage("Document list refreshed.", 4000)

    def _refresh_documents(self) -> None:
        """F5: drop the shared cache and re-list every document from disk,
        e.g. after a pull, or to re-check documents that failed to load."""
        root = self.controller.root
        svc = self.controller.service
        if root is None or svc is None:
            return
        if svc.cache is not None:
            svc.cache.invalidate_all()
        self._project_model.refresh()
        self._known_doc_folders = doc_folder_names(root)
        self._refresh_git_cell()
        self._refresh_merge_banner()
        self._status.showMessage(
            f"Refreshed: {self._load_summary()}.", 6000
        )

    def _load_summary(self) -> str:
        text = f"{len(self.controller.docs())} document(s)"
        failed = len(self.controller.load_errors())
        if failed:
            text += f", {failed} failed to load"
        return text

    # --- close ----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        make_settings().setValue(_GEOMETRY_KEY, self.saveGeometry())
        for w in list(self._editor_windows.values()):
            w.close()
        event.accept()

    # --- public API -----------------------------------------------------

    def open_repo(self, path: Path) -> bool:
        return self.controller.open_repo(path)

    # --- document opening -----------------------------------------------

    def _open_document(self, doc_id: str) -> None:
        if doc_id in self._editor_windows:
            w = self._editor_windows[doc_id]
            w.raise_()
            w.activateWindow()
            return

        service = self.controller.require_service()
        try:
            service.get_document(doc_id)
        except ServiceError as e:
            # Broke on disk since the list was built (e.g. a pull with a
            # conflict): re-list from disk, so the document shows as failed
            # instead of vanishing, and report it instead of opening an editor.
            if service.cache is not None:
                service.cache.invalidate_all()
            err = self.controller.load_error(doc_id)
            self._project_model.refresh()
            self._show_load_error(
                doc_id, describe_load_error(err) if err is not None else str(e)
            )
            return
        w = DocumentEditorWindow(doc_id, service)
        w.window_closed.connect(self._on_editor_closed)
        self._editor_windows[doc_id] = w
        w.show()

        doc = self.controller.document(doc_id)
        label = doc.metadata.title or doc_id
        display = f"{doc_id} — {label}" if label != doc_id else doc_id
        item = QListWidgetItem(display)
        item.setData(_DOC_ID_ROLE, doc_id)
        self._open_list.addItem(item)

    def _on_editor_closed(self, doc_id: str) -> None:
        self._editor_windows.pop(doc_id, None)
        for i in range(self._open_list.count()):
            item = self._open_list.item(i)
            if item is not None and item.data(_DOC_ID_ROLE) == doc_id:
                self._open_list.takeItem(i)
                break

    # --- view event handlers --------------------------------------------

    def _on_doc_activated(self, index) -> None:
        doc_id = ProjectTreeModel.doc_id_from_index(index)
        if doc_id is None:
            return
        err = ProjectTreeModel.load_error_from_index(index)
        if err is not None:
            self._show_load_error(doc_id, describe_load_error(err))
            return
        self._open_document(doc_id)

    def _show_load_error(self, doc_id: str, details: str) -> None:
        QMessageBox.warning(
            self,
            "Document failed to load",
            f"{doc_id} could not be loaded:\n\n{details}\n\n"
            "Fix the file (for a merge conflict, use Resolve merge… on the "
            "banner, or your git client), then press F5 to refresh.",
        )

    def _on_open_item_activated(self, index) -> None:
        item = self._open_list.item(index.row())
        if item is None:
            return
        doc_id = item.data(_DOC_ID_ROLE)
        if doc_id in self._editor_windows:
            w = self._editor_windows[doc_id]
            w.raise_()
            w.activateWindow()

    def _new_document(self) -> None:
        if self.controller.service is None:
            return
        dlg = NewDocumentDialog(self.controller, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        doc_id = dlg.doc_id()
        try:
            self.controller.require_service().create_document(doc_id, title=dlg.title())
        except ServiceError as e:
            QMessageBox.warning(self, "Create document failed", str(e))
            return
        self._project_model.refresh()
        if self.controller.root is not None:
            self._known_doc_folders = doc_folder_names(self.controller.root)
        self._status.showMessage(f"Created document {doc_id}", 3000)
        self._open_document(doc_id)

    def _prompt_open_repo(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Open mellon repository",
            str(self.controller.root or Path.cwd()),
        )
        if path:
            self.open_repo(Path(path))

    # --- controller handlers --------------------------------------------

    def _on_repo_loaded(self, root: str) -> None:
        # Editors keep the service they were opened with, so a window from
        # the previous repo would edit and commit there. Close those (each
        # close also drops its OPEN entry) so the doc id isn't reused.
        for w in list(self._editor_windows.values()):
            if w.controller.root != Path(root):
                w.close()
        self._known_doc_folders = doc_folder_names(root)
        self.setWindowTitle(f"{Path(root).name} — Project Explorer — Mellon")
        self._status.showMessage(f"Loaded {self._load_summary()} from {root}")
        self._refresh_merge_banner()

    def _on_repo_load_failed(self, root: str, message: str) -> None:
        current = self.controller.root
        if current is not None:
            # Refused before loading (e.g. a subfolder of the clone was
            # picked): the repo that was open stays open.
            self._status.showMessage(f"Did not open {root}; {current} is still open.")
        else:
            self.setWindowTitle("Project Explorer — Mellon")
            # No repo is open now; don't keep listing the previous one's docs.
            self._project_model.refresh()
            self._status.showMessage(f"Failed to open {root}")
        QMessageBox.warning(self, "Could not open repository", message)

    def _refresh_git_cell(self) -> None:
        root = self.controller.root
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
