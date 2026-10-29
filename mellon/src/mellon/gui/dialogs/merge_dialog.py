"""Resolve merge… — settle a git merge that stopped with document conflicts.

Shows what `merge.plan` found. Objects both branches created under one id
are renumbered without asking (the later one yields): one line each. Only
what really needs a hand gets a block: a requirement field both branches
changed (the text is edited into a merged result, an attribute value or a
table/figure is taken from one side), document properties, a view or filter,
and an object deleted on one side but changed on the other. A requirement's
block starts with what it traces to, so the merge can be judged against it.
Everything defaults to mine (and to keeping the object), so Resolve works at
once. The merge commit itself is made in the git client.

Blocks stack vertically and texts wrap, so the dialog reads in a narrow
window.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ... import merge


def _wrapped(text: str, *, rich: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    return label


def _section(title: str) -> QLabel:
    label = QLabel(title.upper())
    label.setObjectName("PaneHeader")
    return label


class MergeDialog(QDialog):
    def __init__(self, root: Path, plan: merge.MergePlan, parent=None) -> None:
        super().__init__(parent)
        self._root = root
        self._plan = plan
        self._resolved: tuple[str, ...] = ()
        self._names = {"ours": plan.into, "theirs": plan.merging}
        self.setWindowTitle("Resolve merge")
        self.resize(560, 680)
        self.setMinimumWidth(380)

        # path -> field -> widget: QPlainTextEdit (text), QComboBox (value),
        # (mine radio, theirs radio) (whole)
        self._fields: dict[str, dict[str, object]] = {}
        # path -> "Keep" checkbox, for objects deleted on one side
        self._keeps: dict[str, QCheckBox] = {}

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(10)
        layout.addWidget(_wrapped(
            f"Merging {plan.merging} into {plan.into}. Mellon settles what "
            "it can; below is what needs your hand. Resolve writes the "
            "result; then make the merge commit in your git client."
        ))

        if plan.renumbered:
            layout.addWidget(_section("Renumbered"))
            for r in plan.renumbered:
                layout.addWidget(_wrapped(
                    f"{r.obj_id} on {self._names[r.side]} was created later "
                    f"under the same id: it becomes <b>{r.new_id}</b>.",
                    rich=True,
                ))
        if plan.by_hand:
            layout.addWidget(_section("Merge by hand"))
            for hand in plan.by_hand:
                layout.addWidget(self._hand_block(hand))
        if not plan.renumbered and not plan.by_hand:
            layout.addWidget(_wrapped("Nothing needs your hand."))
        tail = "Everything else merges automatically."
        if plan.outside:
            tail += (
                " Left for your git client (not in a document folder): "
                + ", ".join(plan.outside)
            )
        layout.addWidget(_wrapped(tail))
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._resolve_btn = self._buttons.addButton(
            "Resolve", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._buttons.accepted.connect(self._resolve)
        self._buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addWidget(self._buttons)

    # --- blocks -------------------------------------------------------------

    def _hand_block(self, hand: merge.HandMerge) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setToolTip(hand.path)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        lay.addWidget(_wrapped(f"<b>{hand.title}</b> · {hand.doc_id}", rich=True))

        if hand.links:
            lay.addWidget(_wrapped("<b>Traces to</b>", rich=True))
            for link in hand.links:
                lay.addWidget(_wrapped(
                    f"{link.link_type} {link.target_id}: {link.text}".strip()
                ))
        if hand.deleted_on is not None:
            keep = QCheckBox(
                f"Keep {hand.title} (deleted on {self._names[hand.deleted_on]})"
            )
            keep.setChecked(True)
            lay.addWidget(keep)
            self._keeps[hand.path] = keep

        fields = self._fields.setdefault(hand.path, {})
        for c in hand.conflicts:
            lay.addWidget(_wrapped(f"<b>{c.label}</b>", rich=True))
            if c.kind == "text":
                fields[c.field] = self._text_field(lay, c)
            elif c.kind == "value":
                combo = QComboBox()
                combo.addItem(f"{c.ours} ({self._names['ours']}, mine)")
                combo.addItem(f"{c.theirs} ({self._names['theirs']})")
                lay.addWidget(combo)
                fields[c.field] = combo
            else:
                group = QButtonGroup(frame)
                mine = QRadioButton(f"Keep mine ({self._names['ours']})")
                theirs = QRadioButton(f"Keep theirs ({self._names['theirs']})")
                for radio in (mine, theirs):
                    group.addButton(radio)
                    lay.addWidget(radio)
                mine.setChecked(True)
                fields[c.field] = (mine, theirs)
        if hand.from_theirs:
            lay.addWidget(_wrapped(
                f"Also merged in from {self._names['theirs']}: "
                + ", ".join(hand.from_theirs)
            ))
        return frame

    def _text_field(self, lay: QVBoxLayout, c: merge.FieldConflict) -> QPlainTextEdit:
        lay.addWidget(_wrapped(f"Mine ({self._names['ours']}): {c.ours}"))
        lay.addWidget(_wrapped(f"Theirs ({self._names['theirs']}): {c.theirs}"))
        result = QPlainTextEdit(c.ours)
        result.setMinimumHeight(60)
        result.setMaximumHeight(110)
        lay.addWidget(result)
        row = QHBoxLayout()
        use_mine = QPushButton("Use mine")
        use_mine.clicked.connect(lambda: result.setPlainText(c.ours))
        use_theirs = QPushButton("Use theirs")
        use_theirs.clicked.connect(lambda: result.setPlainText(c.theirs))
        row.addWidget(use_mine)
        row.addWidget(use_theirs)
        row.addStretch(1)
        lay.addLayout(row)
        return result

    # --- state ------------------------------------------------------------

    def edits(self) -> dict[str, dict[str, str]]:
        """`merge.resolve`'s edits: merged texts and the sides picked."""
        out: dict[str, dict[str, str]] = {}
        for path, fields in self._fields.items():
            for name, widget in fields.items():
                if isinstance(widget, QPlainTextEdit):
                    value = widget.toPlainText()
                elif isinstance(widget, QComboBox):
                    value = "theirs" if widget.currentIndex() == 1 else "ours"
                else:
                    value = "theirs" if widget[1].isChecked() else "ours"
                out.setdefault(path, {})[name] = value
        return out

    def delete(self) -> set[str]:
        """Objects deleted on one side that are to stay deleted."""
        return {path for path, keep in self._keeps.items() if not keep.isChecked()}

    def resolved(self) -> tuple[str, ...]:
        """The paths written or deleted, once Resolve succeeded."""
        return self._resolved

    def _resolve(self) -> None:
        try:
            self._resolved = merge.resolve(
                self._root, edits=self.edits(), delete=self.delete()
            )
        except (merge.MergeError, OSError) as e:
            QMessageBox.critical(self, "Resolve merge failed", str(e))
            return
        self.accept()
