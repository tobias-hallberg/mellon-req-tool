"""Modal dialog for editing the saved Filter applied to the grid.

Layout:
    Name: [______________]   Match: [All ▾]   [x] Keep ancestor headings
    -------------------------------------------------------------------
    [attribute ▾] [operator ▾] [value]                            [×]
    [attribute ▾] [operator ▾] [value]                            [×]
    [+ Add rule]
    -------------------------------------------------------------------
    [Apply]                                          [OK] [Cancel]

Buttons:
    Apply  — live-preview without persisting (transient match-set on the grid).
    OK     — persist to `filters/<slug>.xml`, apply, and close.
    Cancel — discard pending edits and close.

The dialog is self-contained — it edits an in-memory `Filter` object and
exposes `result_filter()` so the caller (MainWindow) chooses what to do
with it (apply transient via `GridPane.apply_filter`, persist via
`controller.create_filter` / `set_filter_rules`).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...filter import (
    KINDS,
    LINK_OPS,
    OPERATORS_LABEL,
    valid_ops_for_kind,
)
from ...schema import Attribute, Filter, FilterRule


# Special entries injected at the top of the attribute combobox.
_LINK_OUT = "__link_out__"
_LINK_IN = "__link_in__"


class _RuleRow(QWidget):
    """One editable rule line. Emits remove_requested when the × is clicked."""

    remove_requested = Signal(object)  # passes self
    changed = Signal()  # emitted on any user-visible change

    def __init__(
        self,
        attributes: tuple[Attribute, ...],
        all_doc_ids: tuple[str, ...],
        rule: FilterRule | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._attributes = attributes
        self._all_doc_ids = all_doc_ids

        self.attr_combo = QComboBox()
        self.attr_combo.addItem("(has outgoing link)", _LINK_OUT)
        self.attr_combo.addItem("(has incoming link)", _LINK_IN)
        for attr in attributes:
            label = attr.display_name
            if attr.label and attr.label != attr.name:
                label = f"{attr.label}  [{attr.name}]"
            self.attr_combo.addItem(label, attr.name)

        self.op_combo = QComboBox()
        # Populated via _refresh_ops based on the current attribute.

        # The value widget swaps based on attr kind + op. We host every
        # variant in a QStackedWidget so layout doesn't reflow.
        self.value_stack = QStackedWidget()
        self.value_stack.setMinimumWidth(200)

        # All inner-widget signals carry payload args (str / int) but our
        # `changed` signal is parameter-less. Wrap each connection in a
        # no-arg lambda so PySide doesn't try to forward the payload to
        # `changed.emit()`.
        self._line_value = QLineEdit()
        self._line_value.setPlaceholderText("value")
        self._line_value.textChanged.connect(lambda *_: self.changed.emit())

        self._between_lo = QLineEdit()
        self._between_lo.setPlaceholderText("from")
        self._between_hi = QLineEdit()
        self._between_hi.setPlaceholderText("to")
        self._between_lo.textChanged.connect(lambda *_: self.changed.emit())
        self._between_hi.textChanged.connect(lambda *_: self.changed.emit())
        between_box = QWidget()
        between_layout = QHBoxLayout(between_box)
        between_layout.setContentsMargins(0, 0, 0, 0)
        between_layout.setSpacing(4)
        between_layout.addWidget(self._between_lo)
        between_layout.addWidget(QLabel("…"))
        between_layout.addWidget(self._between_hi)

        self._enum_combo = QComboBox()
        self._enum_combo.currentIndexChanged.connect(lambda *_: self.changed.emit())

        self._doc_combo = QComboBox()
        self._doc_combo.addItem("(any document)", "")
        for doc_id in all_doc_ids:
            self._doc_combo.addItem(doc_id, doc_id)
        self._doc_combo.currentIndexChanged.connect(lambda *_: self.changed.emit())

        self._empty_widget = QWidget()  # nothing to enter (is-true / is-empty / etc.)

        self.value_stack.addWidget(self._line_value)    # 0
        self.value_stack.addWidget(self._between_box_index_holder(between_box))  # 1
        self.value_stack.addWidget(self._enum_combo)    # 2
        self.value_stack.addWidget(self._doc_combo)     # 3
        self.value_stack.addWidget(self._empty_widget)  # 4

        self._between_box = between_box

        self.remove_btn = QToolButton()
        self.remove_btn.setText("×")
        self.remove_btn.setToolTip("Remove this rule")
        self.remove_btn.setFixedWidth(28)
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.attr_combo, 2)
        layout.addWidget(self.op_combo, 2)
        layout.addWidget(self.value_stack, 4)
        layout.addWidget(self.remove_btn)

        self.attr_combo.currentIndexChanged.connect(lambda *_: self._on_attr_changed())
        self.op_combo.currentIndexChanged.connect(lambda *_: self._on_op_changed())

        if rule is not None:
            self._load(rule)
        else:
            self._refresh_ops()
            self._refresh_value_widget()

    @staticmethod
    def _between_box_index_holder(between_box: QWidget) -> QWidget:
        # Tiny helper: the between box has no setText/get accessor so we
        # keep a reference on the row instance separately. We still need the
        # widget to live in the stack — return as-is.
        return between_box

    # --- attribute / op / value plumbing --------------------------------

    def _current_kind(self) -> str:
        """Return the kind code for the currently-selected attribute, or
        a synthetic code for link rules."""
        data = self.attr_combo.currentData()
        if data == _LINK_OUT:
            return "__link_out__"
        if data == _LINK_IN:
            return "__link_in__"
        for attr in self._attributes:
            if attr.name == data:
                return attr.kind
        # `type` is a system enum-ish attribute that isn't in doc.attributes;
        # the dialog doesn't surface it, but treat unknowns as text.
        return "text"

    def _ops_for_current(self) -> tuple[str, ...]:
        kind = self._current_kind()
        if kind in ("__link_out__", "__link_in__"):
            return LINK_OPS
        return valid_ops_for_kind(kind)

    def _refresh_ops(self) -> None:
        was_blocked = self.op_combo.blockSignals(True)
        prev_op = self.op_combo.currentData()
        self.op_combo.clear()
        for op in self._ops_for_current():
            self.op_combo.addItem(OPERATORS_LABEL.get(op, op), op)
        # Try to preserve the operator across attribute changes when valid.
        if prev_op is not None:
            idx = self.op_combo.findData(prev_op)
            if idx >= 0:
                self.op_combo.setCurrentIndex(idx)
        self.op_combo.blockSignals(was_blocked)

    def _on_attr_changed(self) -> None:
        self._refresh_ops()
        self._refresh_value_widget()
        self.changed.emit()

    def _on_op_changed(self) -> None:
        self._refresh_value_widget()
        self.changed.emit()

    def _refresh_value_widget(self) -> None:
        """Pick the right value editor for the current (kind, op)."""
        kind = self._current_kind()
        op = self.op_combo.currentData()

        # Operators that take no value at all.
        if op in ("is-empty", "is-not-empty", "is-true", "is-false",
                  "present", "absent"):
            self.value_stack.setCurrentWidget(self._empty_widget)
            return

        if kind == "__link_out__" or kind == "__link_in__":
            # Link rules don't have a value field; they have a target-doc
            # combobox. The "value" slot in our schema is unused; the
            # target doc id is stored in `attr` instead, but we expose it
            # via the doc_combo widget right here.
            self.value_stack.setCurrentWidget(self._doc_combo)
            return

        if op == "between":
            self.value_stack.setCurrentWidget(self._between_box)
            return

        # Enum attribute → option dropdown.
        if kind == "enum":
            spec = next(
                (a for a in self._attributes if a.name == self.attr_combo.currentData()),
                None,
            )
            self._populate_enum_combo(spec.options if spec else ())
            self.value_stack.setCurrentWidget(self._enum_combo)
            return

        # Everything else falls back to the line-edit. (date / integer /
        # real are all just typed strings; the engine coerces.)
        self.value_stack.setCurrentWidget(self._line_value)

    def _populate_enum_combo(self, options: tuple[str, ...]) -> None:
        was_blocked = self._enum_combo.blockSignals(True)
        prev = self._enum_combo.currentText()
        self._enum_combo.clear()
        for opt in options:
            self._enum_combo.addItem(opt, opt)
        if prev:
            idx = self._enum_combo.findData(prev)
            if idx >= 0:
                self._enum_combo.setCurrentIndex(idx)
        self._enum_combo.blockSignals(was_blocked)

    # --- load / dump ----------------------------------------------------

    def _load(self, rule: FilterRule) -> None:
        # Pick the matching attr-combo entry for the rule's kind+attr.
        if rule.kind == "has-outgoing":
            idx = self.attr_combo.findData(_LINK_OUT)
        elif rule.kind == "has-incoming":
            idx = self.attr_combo.findData(_LINK_IN)
        else:
            idx = self.attr_combo.findData(rule.attr) if rule.attr else 2
        if idx >= 0:
            was_blocked = self.attr_combo.blockSignals(True)
            self.attr_combo.setCurrentIndex(idx)
            self.attr_combo.blockSignals(was_blocked)

        self._refresh_ops()
        if rule.op:
            op_idx = self.op_combo.findData(rule.op)
            if op_idx >= 0:
                was_blocked = self.op_combo.blockSignals(True)
                self.op_combo.setCurrentIndex(op_idx)
                self.op_combo.blockSignals(was_blocked)

        self._refresh_value_widget()

        # Push value into whichever editor is now current.
        target = self.value_stack.currentWidget()
        if target is self._between_box and "|" in (rule.value or ""):
            lo, hi = rule.value.split("|", 1)
            self._between_lo.setText(lo)
            self._between_hi.setText(hi)
        elif target is self._enum_combo:
            ix = self._enum_combo.findData(rule.value)
            if ix >= 0:
                self._enum_combo.setCurrentIndex(ix)
        elif target is self._doc_combo:
            # For link rules, target_doc lives in `attr`, not `value`.
            ix = self._doc_combo.findData(rule.attr or "")
            if ix >= 0:
                self._doc_combo.setCurrentIndex(ix)
        elif target is self._line_value:
            self._line_value.setText(rule.value or "")

    def to_rule(self) -> FilterRule | None:
        """Materialise the row as a `FilterRule`, or None when not selectable."""
        kind_marker = self.attr_combo.currentData()
        op = self.op_combo.currentData()
        if op is None:
            return None

        if kind_marker == _LINK_OUT:
            target_doc = self._doc_combo.currentData() or ""
            return FilterRule(kind="has-outgoing", attr=target_doc, op=op)
        if kind_marker == _LINK_IN:
            target_doc = self._doc_combo.currentData() or ""
            return FilterRule(kind="has-incoming", attr=target_doc, op=op)

        target = self.value_stack.currentWidget()
        if target is self._empty_widget:
            value = ""
        elif target is self._between_box:
            value = f"{self._between_lo.text().strip()}|{self._between_hi.text().strip()}"
        elif target is self._enum_combo:
            value = self._enum_combo.currentData() or ""
        else:
            value = self._line_value.text().strip()

        return FilterRule(
            kind="attr",
            attr=str(kind_marker),
            op=op,
            value=value,
        )


class FilterDialog(QDialog):
    """Edit a saved Filter and apply / persist it.

    The dialog never writes to disk itself — it returns the assembled
    `Filter` to the caller. This keeps the editor → service flow consistent
    with the rest of the GUI (controller writes, dialog asks).
    """

    # Emitted when the user clicks Apply (live preview without closing).
    apply_requested = Signal(object)  # passes the in-memory Filter
    # Emitted when the user clicks OK (persist to filters/<slug>.xml + apply + close).
    save_and_apply_requested = Signal(object)

    def __init__(
        self,
        attributes: tuple[Attribute, ...],
        all_doc_ids: tuple[str, ...],
        existing_filter_names: set[str],
        initial: Filter | None = None,
        parent: QWidget | None = None,
        read_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Advanced filter")
        self.resize(720, 480)
        self._attributes = attributes
        self._all_doc_ids = all_doc_ids
        self._read_only = read_only
        # casefold the names so save-collision detection is case-insensitive.
        self._existing_names = {n.strip().casefold() for n in existing_filter_names}
        # Editing an existing filter? Then its current name is allowed.
        if initial is not None:
            self._existing_names.discard(initial.name.strip().casefold())

        # --- header
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. High-priority functional")
        if initial is not None:
            self._name_edit.setText(initial.name)
        self._combinator_combo = QComboBox()
        self._combinator_combo.addItem("All", "all")
        self._combinator_combo.addItem("Any", "any")
        if initial is not None and initial.combinator == "any":
            self._combinator_combo.setCurrentIndex(1)
        self._keep_ancestors_chk = QCheckBox("Keep ancestor headings visible")
        self._keep_ancestors_chk.setChecked(
            True if initial is None else initial.keep_ancestors
        )

        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Name:"))
        header_row.addWidget(self._name_edit, 2)
        header_row.addSpacing(8)
        header_row.addWidget(QLabel("Match:"))
        header_row.addWidget(self._combinator_combo)
        header_row.addWidget(QLabel(" of the following rules"))
        header_row.addStretch(1)
        header_row.addWidget(self._keep_ancestors_chk)

        # --- rules list (scrollable so it doesn't blow out the dialog)
        self._rules_container = QWidget()
        self._rules_layout = QVBoxLayout(self._rules_container)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.setSpacing(4)
        self._rules_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rules_container)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._add_btn = QPushButton("+ Add rule")
        self._add_btn.clicked.connect(lambda: self._add_row(None))
        self._clear_btn = QPushButton("Clear all")
        self._clear_btn.clicked.connect(self._clear_rows)
        rule_buttons = QHBoxLayout()
        rule_buttons.addWidget(self._add_btn)
        rule_buttons.addWidget(self._clear_btn)
        rule_buttons.addStretch(1)

        # --- footer
        self._buttons = QDialogButtonBox()
        self._apply_btn = self._buttons.addButton(
            "Apply", QDialogButtonBox.ButtonRole.ApplyRole
        )
        self._buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        self._apply_btn.clicked.connect(self._on_apply)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color: palette(mid); font-size: 10pt;")
        self._hint.setTextFormat(Qt.TextFormat.PlainText)

        layout = QVBoxLayout(self)
        layout.addLayout(header_row)
        layout.addWidget(scroll, 1)
        layout.addLayout(rule_buttons)
        layout.addWidget(self._hint)
        layout.addWidget(self._buttons)

        self._name_edit.textChanged.connect(self._revalidate)

        if initial is not None and initial.rules:
            for rule in initial.rules:
                self._add_row(rule)
        else:
            self._add_row(None)

        self._revalidate()
        self._name_edit.setFocus()

    # --- rule list mutation ---------------------------------------------

    def _add_row(self, rule: FilterRule | None) -> None:
        row = _RuleRow(
            attributes=self._attributes,
            all_doc_ids=self._all_doc_ids,
            rule=rule,
            parent=self._rules_container,
        )
        row.remove_requested.connect(self._remove_row)
        # Insert above the trailing stretch.
        self._rules_layout.insertWidget(self._rules_layout.count() - 1, row)

    def _remove_row(self, row: _RuleRow) -> None:
        row.remove_requested.disconnect()
        self._rules_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()

    def _clear_rows(self) -> None:
        rows = self._all_rows()
        for row in rows:
            self._remove_row(row)

    def _all_rows(self) -> list[_RuleRow]:
        out: list[_RuleRow] = []
        for i in range(self._rules_layout.count()):
            item = self._rules_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, _RuleRow):
                out.append(w)
        return out

    # --- assembly + validation ------------------------------------------

    def _revalidate(self) -> None:
        text = self._name_edit.text().strip()
        ok_for_save = bool(text) and text.casefold() not in self._existing_names
        if self._read_only:
            self._hint.setText("Read-only mode — filters can't be saved.")
        elif not text:
            self._hint.setText("Name is required to save the filter.")
        elif text.casefold() in self._existing_names:
            self._hint.setText(f"A filter named {text!r} already exists.")
        else:
            self._hint.setText("")
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setEnabled(ok_for_save and not self._read_only)

    def result_filter(self, slug: str = "") -> Filter:
        """Materialise the dialog state as a `Filter` ready for the engine.

        `slug` is what the caller assigns when persisting. The dialog
        never invents a slug itself — that's the editor's job.
        """
        name = self._name_edit.text().strip()
        rules = []
        for row in self._all_rows():
            rule = row.to_rule()
            if rule is None:
                continue
            rules.append(rule)
        return Filter(
            slug=slug,
            name=name,
            combinator=self._combinator_combo.currentData() or "all",
            keep_ancestors=self._keep_ancestors_chk.isChecked(),
            rules=tuple(rules),
        )

    # --- button handlers ------------------------------------------------

    def _on_apply(self) -> None:
        self.apply_requested.emit(self.result_filter())

    def _on_ok(self) -> None:
        self.save_and_apply_requested.emit(self.result_filter())
        self.accept()
