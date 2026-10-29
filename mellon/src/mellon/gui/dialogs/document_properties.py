"""Modal dialog for editing document-level properties.

Three tabs:
    Metadata   — title / author / date. (Issue is tag-managed via the
                 commit dialog; it is intentionally not editable here.)
    Parents    — add or remove parent doc references.
    Attributes — add or delete attributes; add or remove enum options.

Mutations apply immediately to the service. The bottom button is Close, not
OK/Cancel: deferring cascades like delete-attribute until the dialog accepts
would require shadowing object data and is not worth the complexity. Each
change is an undo step in the document's editor window (Ctrl+Z).
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...schema import Document
from ...service import ServiceError
from ..controller import RepoController


_ATTR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class DocumentPropertiesDialog(QDialog):
    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._doc_id = doc_id
        self.setWindowTitle(f"Document properties — {doc_id}")
        self.resize(560, 480)

        tabs = QTabWidget(self)
        self._metadata_tab = MetadataTab(controller, doc_id, self)
        self._parents_tab = ParentsTab(controller, doc_id, self)
        self._attributes_tab = AttributesTab(controller, doc_id, self)
        tabs.addTab(self._metadata_tab, "Metadata")
        tabs.addTab(self._parents_tab, "Parents")
        tabs.addTab(self._attributes_tab, "Attributes")

        # Close emits rejected; `done` asks about unapplied metadata.
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def done(self, result: int) -> None:  # type: ignore[override]
        # Close, Esc and the title-bar X all land here. The Metadata tab only
        # writes on Apply (the other tabs apply immediately), so ask before
        # dropping typed-but-unapplied values.
        tab = self._metadata_tab
        if tab.has_unapplied_changes():
            answer = QMessageBox.question(
                self,
                "Unapplied changes",
                "Apply the metadata changes before closing?",
                QMessageBox.StandardButton.Apply
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Apply,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Apply:
                tab._on_apply()
                if tab.has_unapplied_changes():
                    return  # apply failed; the tab shows the error
        super().done(result)


class MetadataTab(QWidget):
    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._doc_id = doc_id

        doc = controller.document(doc_id)
        meta = doc.metadata
        # `issue` lives as a git tag and is bumped from the commit dialog;
        # it intentionally isn't editable here.
        self._initial = {
            "title": meta.title,
            "author": meta.author,
            "date": meta.date,
        }

        self._title_edit = QLineEdit(meta.title)
        self._author_edit = QLineEdit(meta.author)
        self._date_edit = QLineEdit(meta.date)
        self._date_edit.setPlaceholderText("YYYY-MM-DD")

        form = QFormLayout()
        form.addRow("Title:", self._title_edit)
        form.addRow("Author:", self._author_edit)
        form.addRow("Date:", self._date_edit)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.clicked.connect(self._on_apply)

        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid); font-size: 9pt;")

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._apply_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(btn_row)
        layout.addWidget(self._status)
        layout.addStretch(1)

    def _current_values(self) -> dict[str, str]:
        return {
            "title": self._title_edit.text(),
            "author": self._author_edit.text(),
            "date": self._date_edit.text(),
        }

    def has_unapplied_changes(self) -> bool:
        return self._current_values() != self._initial

    def _on_apply(self) -> None:
        svc = self._controller.require_service()
        current = self._current_values()
        changed: list[str] = []
        for field, value in current.items():
            if value == self._initial[field]:
                continue
            try:
                svc.set_metadata(self._doc_id, field, value)
            except ServiceError as e:
                self._status.setText(f"Failed to set {field}: {e}")
                return
            changed.append(field)
            self._initial[field] = value

        if changed:
            self._controller.reload_document(self._doc_id)
            self._status.setText(f"Updated: {', '.join(changed)}.")
        else:
            self._status.setText("No changes.")


class ParentsTab(QWidget):
    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._doc_id = doc_id

        self._list = QListWidget(self)
        self._add_btn = QPushButton("Add…")
        self._remove_btn = QPushButton("Remove")
        self._add_btn.clicked.connect(self._on_add)
        self._remove_btn.clicked.connect(self._on_remove)
        self._list.itemSelectionChanged.connect(self._refresh_buttons)

        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid); font-size: 9pt;")

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._add_btn)
        btn_row.addWidget(self._remove_btn)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Parent documents:"))
        layout.addWidget(self._list, 1)
        layout.addLayout(btn_row)
        layout.addWidget(self._status)

        self._reload()

    def _reload(self) -> None:
        doc = self._controller.document(self._doc_id)
        self._list.clear()
        for p in doc.parents:
            self._list.addItem(p)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self._remove_btn.setEnabled(self._list.currentItem() is not None)

    def _candidates(self, doc: Document) -> list[str]:
        sibs = sorted(self._controller.docs().keys())
        existing = set(doc.parents)
        return [d for d in sibs if d != self._doc_id and d not in existing]

    def _on_add(self) -> None:
        doc = self._controller.document(self._doc_id)
        candidates = self._candidates(doc)
        if not candidates:
            self._status.setText("No other documents available to add as parent.")
            return
        choice, ok = QInputDialog.getItem(
            self,
            "Add parent",
            "Select parent document:",
            candidates,
            0,
            False,
        )
        if not ok or not choice:
            return
        try:
            self._controller.require_service().add_parent(self._doc_id, choice)
        except ServiceError as e:
            self._status.setText(f"Add failed: {e}")
            return
        self._controller.reload_document(self._doc_id)
        self._reload()
        self._status.setText(f"Added parent {choice}.")

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        parent_doc = item.text()
        try:
            self._controller.require_service().remove_parent(self._doc_id, parent_doc)
        except ServiceError as e:
            self._status.setText(f"Remove failed: {e}")
            return
        self._controller.reload_document(self._doc_id)
        self._reload()
        self._status.setText(f"Removed parent {parent_doc}.")


class AttributesTab(QWidget):
    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._doc_id = doc_id

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["Name", "Kind", "Required", "Options"])
        self._tree.setRootIsDecorated(True)
        self._tree.itemSelectionChanged.connect(self._refresh_buttons)

        self._add_attr_btn = QPushButton("Add attribute…")
        self._del_attr_btn = QPushButton("Delete attribute")
        self._add_opt_btn = QPushButton("Add option…")
        self._rm_opt_btn = QPushButton("Remove option")
        self._add_attr_btn.clicked.connect(self._on_add_attribute)
        self._del_attr_btn.clicked.connect(self._on_delete_attribute)
        self._add_opt_btn.clicked.connect(self._on_add_option)
        self._rm_opt_btn.clicked.connect(self._on_remove_option)

        self._status = QLabel("")
        self._status.setStyleSheet("color: palette(mid); font-size: 9pt;")

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._add_attr_btn)
        btn_row.addWidget(self._del_attr_btn)
        btn_row.addWidget(self._add_opt_btn)
        btn_row.addWidget(self._rm_opt_btn)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tree, 1)
        layout.addLayout(btn_row)
        layout.addWidget(self._status)

        self._reload()

    def _reload(self) -> None:
        doc = self._controller.document(self._doc_id)
        self._tree.clear()
        for attr in doc.attributes:
            top = QTreeWidgetItem([
                attr.display_name,
                attr.kind,
                "yes" if attr.required else "no",
                ", ".join(attr.options) if attr.kind == "enum" else "",
            ])
            top.setData(0, Qt.ItemDataRole.UserRole, ("attr", attr.name))
            for opt in attr.options:
                child = QTreeWidgetItem(["", "option", "", opt])
                child.setData(0, Qt.ItemDataRole.UserRole, ("option", attr.name, opt))
                top.addChild(child)
            self._tree.addTopLevelItem(top)
        self._tree.expandAll()
        for col in range(4):
            self._tree.resizeColumnToContents(col)
        self._refresh_buttons()

    def _selected_attr_name(self) -> str | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return None
        if data[0] == "attr":
            return data[1]
        if data[0] == "option":
            return data[1]
        return None

    def _selected_option(self) -> tuple[str, str] | None:
        item = self._tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is not None and data[0] == "option":
            return (data[1], data[2])
        return None

    def _refresh_buttons(self) -> None:
        attr_name = self._selected_attr_name()
        doc = self._controller.document(self._doc_id)
        attr = doc.attribute(attr_name) if attr_name else None
        is_enum = attr is not None and attr.kind == "enum"
        self._del_attr_btn.setEnabled(attr is not None)
        self._add_opt_btn.setEnabled(is_enum)
        self._rm_opt_btn.setEnabled(self._selected_option() is not None)

    def _on_add_attribute(self) -> None:
        doc = self._controller.document(self._doc_id)
        existing = {a.name for a in doc.attributes}
        dlg = AddAttributeDialog(existing, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._controller.require_service().add_attribute(
                self._doc_id, dlg.name(), dlg.kind(), dlg.required(), dlg.options(), dlg.label()
            )
        except ServiceError as e:
            self._status.setText(f"Add failed: {e}")
            return
        self._controller.reload_document(self._doc_id)
        self._reload()
        self._status.setText(f"Added attribute {dlg.name()}.")

    def _on_delete_attribute(self) -> None:
        attr_name = self._selected_attr_name()
        if attr_name is None:
            return
        doc = self._controller.document(self._doc_id)
        n_objects = sum(1 for o in doc.objects.values() if attr_name in o.attrs)
        n_rules = sum(1 for r in doc.rules if attr_name in r.directives)
        msg = (
            f"Delete attribute {attr_name!r}?\n\n"
            f"This will strip the value from {n_objects} object(s) "
            f"and {n_rules} rule directive(s)."
        )
        confirm = QMessageBox.question(
            self,
            "Delete attribute",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._controller.require_service().delete_attribute(self._doc_id, attr_name)
        except ServiceError as e:
            self._status.setText(f"Delete failed: {e}")
            return
        self._controller.reload_document(self._doc_id)
        self._reload()
        self._status.setText(f"Deleted attribute {attr_name}.")

    def _on_add_option(self) -> None:
        attr_name = self._selected_attr_name()
        if attr_name is None:
            return
        doc = self._controller.document(self._doc_id)
        attr = doc.attribute(attr_name)
        if attr is None or attr.kind != "enum":
            return
        text, ok = QInputDialog.getText(
            self, "Add option", f"New option for {attr_name}:"
        )
        if not ok:
            return
        text = text.strip()
        if not text:
            self._status.setText("Option cannot be empty.")
            return
        try:
            self._controller.require_service().add_attr_option(
                self._doc_id, attr_name, text
            )
        except ServiceError as e:
            self._status.setText(f"Add option failed: {e}")
            return
        self._controller.reload_document(self._doc_id)
        self._reload()
        self._status.setText(f"Added option {text} to {attr_name}.")

    def _on_remove_option(self) -> None:
        sel = self._selected_option()
        if sel is None:
            return
        attr_name, option = sel
        doc = self._controller.document(self._doc_id)
        n_objects = sum(
            1
            for o in doc.objects.values()
            if o.attrs.get(attr_name) == option
        )
        msg = (
            f"Remove option {option!r} from {attr_name!r}?\n\n"
            f"{n_objects} object(s) currently use this value; their value "
            f"will be cleared (left blank for the validator to flag)."
        )
        confirm = QMessageBox.question(
            self,
            "Remove option",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._controller.require_service().remove_attr_option(
                self._doc_id, attr_name, option
            )
        except ServiceError as e:
            self._status.setText(f"Remove option failed: {e}")
            return
        self._controller.reload_document(self._doc_id)
        self._reload()
        self._status.setText(f"Removed option {option} from {attr_name}.")


class AddAttributeDialog(QDialog):
    def __init__(self, existing_names: set[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add attribute")
        self.resize(420, 320)
        self._existing = existing_names

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. priority")

        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("Human-readable display name (optional)")

        self._kind_combo = QComboBox()
        self._kind_combo.addItems(
            ["enum", "text", "integer", "real", "boolean", "date"]
        )
        self._kind_combo.currentTextChanged.connect(self._on_kind_changed)

        self._required_check = QCheckBox("Required")

        self._options_list = QListWidget()
        self._add_opt_btn = QPushButton("Add option…")
        self._rm_opt_btn = QPushButton("Remove option")
        self._add_opt_btn.clicked.connect(self._on_add_option)
        self._rm_opt_btn.clicked.connect(self._on_remove_option)

        opt_btn_row = QHBoxLayout()
        opt_btn_row.addWidget(self._add_opt_btn)
        opt_btn_row.addWidget(self._rm_opt_btn)
        opt_btn_row.addStretch(1)

        form = QFormLayout()
        form.addRow("Name:", self._name_edit)
        form.addRow("Label:", self._label_edit)
        form.addRow("Kind:", self._kind_combo)
        form.addRow("", self._required_check)
        form.addRow("Options:", self._options_list)
        form.addRow("", opt_btn_row)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: palette(mid); font-size: 9pt;")

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addWidget(self._buttons)

        self._name_edit.textChanged.connect(self._revalidate)
        self._on_kind_changed(self._kind_combo.currentText())
        self._revalidate()
        self._name_edit.setFocus()

    def _on_kind_changed(self, kind: str) -> None:
        is_enum = kind == "enum"
        self._options_list.setEnabled(is_enum)
        self._add_opt_btn.setEnabled(is_enum)
        self._rm_opt_btn.setEnabled(is_enum)
        if not is_enum:
            self._options_list.clear()
        self._revalidate()

    def _revalidate(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._hint.setText("")
            self._ok_btn.setEnabled(False)
            return
        if not _ATTR_NAME_RE.match(name):
            self._hint.setText("Use letters, digits, hyphens, underscores only.")
            self._ok_btn.setEnabled(False)
            return
        if name in self._existing:
            self._hint.setText(f"Attribute {name!r} already exists.")
            self._ok_btn.setEnabled(False)
            return
        self._hint.setText("")
        self._ok_btn.setEnabled(True)

    def _on_add_option(self) -> None:
        text, ok = QInputDialog.getText(self, "Add option", "Option value:")
        if not ok:
            return
        text = text.strip()
        if not text:
            return
        existing = {self._options_list.item(i).text() for i in range(self._options_list.count())}
        if text in existing:
            return
        self._options_list.addItem(QListWidgetItem(text))

    def _on_remove_option(self) -> None:
        item = self._options_list.currentItem()
        if item is None:
            return
        self._options_list.takeItem(self._options_list.row(item))

    def name(self) -> str:
        return self._name_edit.text().strip()

    def kind(self) -> str:
        return self._kind_combo.currentText()

    def required(self) -> bool:
        return self._required_check.isChecked()

    def label(self) -> str:
        return self._label_edit.text().strip()

    def options(self) -> tuple[str, ...]:
        if self.kind() != "enum":
            return ()
        return tuple(
            self._options_list.item(i).text()
            for i in range(self._options_list.count())
        )
