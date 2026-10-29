"""Inspector tab content for the right pane.

Editable widgets are always shown when the window isn't read-only. Each field
auto-commits as it loses focus (`editingFinished` for QLineEdit, focus-out
event for the description QTextEdit) or when its value changes (QComboBox).
On read-only the same fields render as plain QLabels.

The pane-header bar and collapsed rail live on the parent `RightPane` — this
widget is just the tab content (header row + scrollable form + links).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...schema import attribute_display_name
from ...service import ServiceError
from ...suspects import LinkState
from ...validator import effective_directives
from .. import icons
from ..controller import RepoController
from ..models.objects_grid import STALE_EDIT_MESSAGE
from ..theme import ThemeManager


class Inspector(QWidget):
    EMPTY_MESSAGE = "Select a requirement to view details."

    def __init__(self, controller: RepoController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._read_only = False
        # True while the window's "Changed on disk" banner is up; edits are
        # refused until the user reloads.
        self._stale = False
        self._current_doc_id: str | None = None
        self._current_obj_id: str | None = None
        # Last-committed values, used to skip no-op writes when an edit signal
        # fires without an actual change.
        self._attr_widgets: dict[str, QWidget] = {}
        self._original_attrs: dict[str, str] = {}
        self._original_description: str = ""

        # Ignored horizontal policy so content never drives the splitter width.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(0)

        self._id_label = QLabel()
        self._id_label.setStyleSheet("font-weight: bold; font-size: 10pt;")
        self._id_label.setWordWrap(False)
        self._breadcrumb = QLabel()
        self._breadcrumb.setStyleSheet("color: palette(mid);")
        self._breadcrumb.setWordWrap(True)
        self._empty = QLabel(self.EMPTY_MESSAGE)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color: palette(mid); padding: 24px;")

        self._header = QWidget()
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(self._id_label)
        header_layout.addStretch(1)

        self._attrs_form = QFormLayout()
        # Default QFormLayout adds platform margins around the rows; zero them
        # so the labels line up with the rest of the column (id label,
        # breadcrumb, "Description", description widget).
        self._attrs_form.setContentsMargins(0, 0, 0, 0)
        self._attrs_wrap = QWidget()
        self._attrs_wrap.setLayout(self._attrs_form)

        self._description = QTextEdit()
        self._description.setMinimumHeight(180)
        self._description.setMinimumWidth(0)
        self._description.setAcceptRichText(False)
        self._description.setTabChangesFocus(True)
        # Auto-commit when the description loses focus.
        self._description.installEventFilter(self)

        self._outgoing_label = QLabel("Outgoing links:")
        self._outgoing_label.setStyleSheet("font-weight: 600; margin-top: 8px;")
        self._outgoing = QListWidget()
        self._outgoing.setMinimumWidth(0)
        self._outgoing.setResizeMode(QListView.ResizeMode.Adjust)
        self._outgoing.itemActivated.connect(self._on_link_activated)
        self._outgoing.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._outgoing.customContextMenuRequested.connect(self._on_outgoing_menu)

        self._incoming_label = QLabel("Incoming links:")
        self._incoming_label.setStyleSheet("font-weight: 600; margin-top: 8px;")
        self._incoming = QListWidget()
        self._incoming.setMinimumWidth(0)
        self._incoming.setResizeMode(QListView.ResizeMode.Adjust)
        self._incoming.itemActivated.connect(self._on_link_activated)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._empty)
        layout.addWidget(self._header)
        layout.addWidget(self._breadcrumb)
        layout.addWidget(self._attrs_wrap)
        layout.addSpacing(12)
        layout.addWidget(QLabel("Description:"))
        layout.addWidget(self._description)
        layout.addWidget(self._outgoing_label)
        layout.addWidget(self._outgoing)
        layout.addWidget(self._incoming_label)
        layout.addWidget(self._incoming)
        layout.addStretch(1)

        self._set_empty_state(True)

        controller.repo_loaded.connect(lambda *_: self._set_empty_state(True))
        controller.object_selected.connect(self._on_selection)
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, *_args) -> None:
        # Link-row Ack buttons are rebuilt by `_repopulate`, picking up the
        # new tint there.
        self._repopulate()

    # --- empty state ---------------------------------------------------

    def _set_empty_state(self, empty: bool) -> None:
        self._empty.setVisible(empty)
        for w in (
            self._header,
            self._breadcrumb,
            self._attrs_wrap,
            self._description,
            self._outgoing_label,
            self._outgoing,
            self._incoming_label,
            self._incoming,
        ):
            w.setVisible(not empty)

    # --- selection + repopulation --------------------------------------

    def _on_selection(self, doc_id: str, obj_id: str) -> None:
        self._current_doc_id = doc_id
        self._current_obj_id = obj_id
        self._repopulate()

    def _repopulate(self) -> None:
        if self._current_doc_id is None or self._current_obj_id is None:
            return
        doc_id = self._current_doc_id
        obj_id = self._current_obj_id
        try:
            view = self._controller.object_view(doc_id, obj_id)
        except ServiceError:
            # The document no longer loads (the window's stale banner is up):
            # keep showing the last good copy. Edits are refused until a
            # reload succeeds, which repopulates in the current mode.
            return
        self._set_empty_state(False)

        self._id_label.setText(obj_id)
        self._breadcrumb.setText(
            " › ".join(view.outline_path) if view.outline_path else ""
        )

        self._original_attrs = dict(view.object.attrs)
        self._original_description = view.object.description or ""

        self._populate_attrs(doc_id, view.object.attrs)
        self._populate_description(view.object.description or "")
        self._lock_fields_while_stale()

        # Links are read-only in slice 2.
        self._populate_links(view)

    def refresh_links(self) -> None:
        """Re-read the shown object's link states (after HEAD moved) without
        rebuilding the form, so typed-but-uncommitted text survives."""
        if self._current_doc_id is None or self._current_obj_id is None:
            return
        try:
            view = self._controller.object_view(
                self._current_doc_id, self._current_obj_id
            )
        except (ServiceError, KeyError):
            return  # doc no longer loads / object gone: the next reload says so
        self._populate_links(view)

    def _populate_attrs(self, doc_id: str, attrs: dict[str, str]) -> None:
        self._clear_form(self._attrs_form)
        self._attr_widgets = {}
        doc = self._controller.document(doc_id)

        obj = doc.objects.get(self._current_obj_id) if self._current_obj_id else None
        has_link = bool(doc.links.get(self._current_obj_id)) if self._current_obj_id else False
        directives = effective_directives(doc.rules, obj, has_link) if obj else {}

        for name, value in attrs.items():
            status = directives.get(name, "optional")
            # Hide forbidden attrs that already have no value — keep visible
            # ones with a note so the user can clear them.
            if status == "forbidden" and not value.strip():
                continue
            schema = doc.attribute(name)
            display = attribute_display_name(name, schema)
            label_text = display
            if status == "required" and not value.strip():
                label_text = f"* {display}"
            elif status == "forbidden":
                label_text = f"{display} (must be empty)"
            label_text = f"{label_text}:"

            if self._read_only:
                self._attrs_form.addRow(label_text, QLabel(value))
                continue

            if schema is not None and schema.kind == "enum":
                cb = QComboBox()
                cb.addItems(list(schema.options))
                # An empty value (or one not among the options) shows as no
                # selection, not as the first option it doesn't hold — which
                # also couldn't be picked then, as picking it is no change.
                cb.setCurrentIndex(cb.findText(value))
                # Connect after setCurrentIndex so the initial population
                # doesn't trigger a spurious commit.
                cb.currentTextChanged.connect(
                    lambda _text, n=name, w=cb: self._commit_attr(n, w)
                )
                self._attrs_form.addRow(label_text, cb)
                self._attr_widgets[name] = cb
            else:
                le = QLineEdit(value)
                le.editingFinished.connect(
                    lambda n=name, w=le: self._commit_attr(n, w)
                )
                self._attrs_form.addRow(label_text, le)
                self._attr_widgets[name] = le

    def _populate_description(self, text: str) -> None:
        self._description.setPlainText(text)
        self._description.setReadOnly(self._read_only)

    def _populate_links(self, view) -> None:
        self._outgoing.clear()
        if view.outgoing:
            for status in view.outgoing:
                self._add_outgoing_row(status)
        else:
            self._outgoing.addItem(QListWidgetItem("(none)"))

        self._incoming.clear()
        if view.incoming:
            for src_doc, src_id, link_type in view.incoming:
                label = f"{link_type}  ←  {src_doc}/{src_id}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole + 1, (src_doc, src_id))
                self._incoming.addItem(item)
        else:
            self._incoming.addItem(QListWidgetItem("(none)"))

    def _add_outgoing_row(self, status) -> None:
        """Build a compound row widget for one outgoing link.

        Layout: [link label + state tag] ......... [Ack] button (when suspect).
        The QListWidgetItem still carries the target payload so double-click
        navigation + the context menu keep working.
        """
        link = status.link
        state = status.state.value

        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(6)

        tag = state.upper()
        tokens = ThemeManager.instance().current_tokens()
        if state == "ok":
            tag_color = tokens["ok"]
        elif state == "suspect":
            tag_color = tokens["warn"]
        else:
            tag_color = tokens["err"]
        label = QLabel(
            f'{link.type}  →  {link.target_doc}/{link.target_id}  '
            f'<span style="color:{tag_color}; font-weight:600;">[{tag}]</span>'
        )
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        if status.reason and state != "ok":
            label.setToolTip(status.reason)
        layout.addWidget(label, 1)

        if (
            status.state is LinkState.SUSPECT
            and status.current_hash is not None
            and not self._read_only
        ):
            btn = QPushButton(icons.icon("ack"), "Ack")
            btn.setToolTip("Acknowledge this suspect link.")
            btn.clicked.connect(
                lambda _checked=False, s=status: self._acknowledge_link(s)
            )
            layout.addWidget(btn)

        item = QListWidgetItem()
        item.setSizeHint(row.sizeHint())
        item.setData(
            Qt.ItemDataRole.UserRole + 1,
            (link.target_doc, link.target_id),
        )
        self._outgoing.addItem(item)
        self._outgoing.setItemWidget(item, row)

    def _acknowledge_link(self, status) -> None:
        """Write @ack=<current_hash> on the single suspect link."""
        doc_id = self._current_doc_id
        obj_id = self._current_obj_id
        if doc_id is None or obj_id is None or self._refuse_if_stale("ack"):
            return
        try:
            result = self._controller.require_service().ack_all(doc_id, [status])
        except ServiceError as e:
            self._controller.edit_failed.emit(doc_id, obj_id, "ack", str(e))
            return
        if not result.value:
            self._controller.edit_failed.emit(
                doc_id, obj_id, "ack",
                "link was not in a suspect state or has no current hash",
            )
            return
        self._controller.edit_succeeded.emit(doc_id, obj_id, "ack")
        self._controller.reload_document(doc_id)

    def _on_link_activated(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        target_doc, target_id = payload
        self._controller.select_object(target_doc, target_id)

    def _on_outgoing_menu(self, pos: QPoint) -> None:
        item = self._outgoing.itemAt(pos)
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole + 1)
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        target_doc, target_id = payload

        menu = QMenu(self._outgoing)
        jump = QAction(f"Jump to {target_doc}/{target_id}", menu)
        jump.triggered.connect(
            lambda: self._controller.select_object(target_doc, target_id)
        )
        menu.addAction(jump)
        if not self._read_only:
            remove = QAction(f"Remove link to {target_doc}/{target_id}", menu)
            remove.triggered.connect(
                lambda: self._remove_outgoing_link(target_doc, target_id)
            )
            menu.addSeparator()
            menu.addAction(remove)
        menu.exec(self._outgoing.mapToGlobal(pos))

    def _remove_outgoing_link(self, target_doc: str, target_id: str) -> None:
        doc_id = self._current_doc_id
        obj_id = self._current_obj_id
        if doc_id is None or obj_id is None or self._refuse_if_stale("link"):
            return
        try:
            result = self._controller.require_service().remove_link(
                doc_id, obj_id, target_doc, target_id
            )
        except ServiceError as e:
            self._controller.edit_failed.emit(doc_id, obj_id, "link", str(e))
            return
        if not result.value:
            self._controller.edit_failed.emit(
                doc_id, obj_id, "link",
                f"no link to {target_doc}/{target_id}",
            )
            return
        self._controller.edit_succeeded.emit(doc_id, obj_id, "link")
        self._controller.reload_document(doc_id)

    def set_read_only(self, on: bool) -> None:
        """Enter/leave read-only mode.

        In read-only mode the form renders QLabels instead of editable
        widgets; toggling rebuilds the form. Any text the user had typed
        but not yet committed is discarded — same as the existing
        implicit-cancel-on-selection-change pattern.
        """
        on = bool(on)
        if self._read_only == on:
            return
        self._read_only = on
        if self._current_doc_id is not None and self._current_obj_id is not None:
            self._repopulate()

    def set_stale(self, on: bool) -> None:
        """Refuse edits while the "Changed on disk" banner is showing.

        The fields are locked meanwhile, as the grid's cells are: text typed
        there could only be refused, and the reload would then drop it.
        """
        self._stale = bool(on)
        self._lock_fields_while_stale()

    def _lock_fields_while_stale(self) -> None:
        locked = self._read_only or self._stale
        self._description.setReadOnly(locked)
        for widget in self._attr_widgets.values():
            if isinstance(widget, QLineEdit):
                widget.setReadOnly(locked)
            elif isinstance(widget, QComboBox):
                widget.setEnabled(not locked)

    def _refuse_if_stale(self, field: str) -> bool:
        if not self._stale:
            return False
        self._controller.edit_failed.emit(
            self._current_doc_id, self._current_obj_id, field, STALE_EDIT_MESSAGE
        )
        return True

    # --- auto-commit ---------------------------------------------------

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._description and event.type() == QEvent.Type.FocusOut:
            self._commit_description()
        return super().eventFilter(obj, event)

    def commit_pending(self) -> None:
        """Commit typed-but-uncommitted text in the free-text fields and the
        description. Their commits fire on focus-out, which never happens when
        an action is triggered by a shortcut or toolbar button."""
        for name, widget in list(self._attr_widgets.items()):
            if isinstance(widget, QLineEdit):
                self._commit_attr(name, widget)
        self._commit_description()

    def _commit_attr(self, name: str, widget: QWidget) -> None:
        if (
            self._read_only
            or self._current_doc_id is None
            or self._current_obj_id is None
        ):
            return
        try:
            if isinstance(widget, QComboBox):
                new_value = widget.currentText()
            elif isinstance(widget, QLineEdit):
                new_value = widget.text()
            else:
                return
        except RuntimeError:
            # Widget was destroyed (e.g. read-only toggled mid-edit).
            return
        if new_value == self._original_attrs.get(name, ""):
            return
        if self._refuse_if_stale(name):
            if isinstance(widget, QComboBox):
                # Show the value on disk, not the refused pick. (A line
                # edit keeps its typed text instead.)
                widget.blockSignals(True)
                widget.setCurrentIndex(
                    widget.findText(self._original_attrs.get(name, ""))
                )
                widget.blockSignals(False)
            return
        doc_id = self._current_doc_id
        obj_id = self._current_obj_id
        try:
            self._controller.require_service().set_attribute(
                doc_id, obj_id, name, new_value
            )
        except ServiceError as e:
            self._controller.edit_failed.emit(doc_id, obj_id, name, str(e))
            return
        self._original_attrs[name] = new_value
        self._controller.edit_succeeded.emit(doc_id, obj_id, name)
        # Reload on the next event-loop pass: the reload rebuilds this form,
        # which would delete the combo / line edit while it is still emitting
        # the signal that got us here (a use-after-free crash).
        QTimer.singleShot(0, self, lambda: self._controller.reload_document(doc_id))

    def _commit_description(self) -> None:
        if (
            self._read_only
            or self._current_doc_id is None
            or self._current_obj_id is None
        ):
            return
        new_desc = self._description.toPlainText()
        if new_desc == self._original_description:
            return
        if self._refuse_if_stale("description"):
            return
        doc_id = self._current_doc_id
        obj_id = self._current_obj_id
        try:
            self._controller.require_service().set_description(
                doc_id, obj_id, new_desc
            )
        except ServiceError as e:
            self._controller.edit_failed.emit(doc_id, obj_id, "description", str(e))
            return
        self._original_description = new_desc
        self._controller.edit_succeeded.emit(doc_id, obj_id, "description")
        self._controller.reload_document(doc_id)

    # --- helpers -------------------------------------------------------

    @staticmethod
    def _clear_form(form: QFormLayout) -> None:
        while form.rowCount():
            form.removeRow(0)
