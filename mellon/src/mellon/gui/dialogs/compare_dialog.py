"""Compare dialog: what changed in one document between two versions.

A version is an issue, a branch, a commit, the latest commit or the working
copy (`diff.Version`). The dialog lists every changed object in outline
order with its change (Added, Deleted, Modified, Moved, Link reviewed), and
shows the selected object's changed fields at From and at To.

The comparison itself is `DocumentService.compare_document`, run on the
thread pool so the editor stays responsive. It only reads, so the dialog is
available in read-only mode too. Non-modal: double-clicking a row selects
the object in the Document Editor and leaves the dialog open.

Specified in MELLON-SRS, under "Compare dialog".
"""

from __future__ import annotations

import html
from typing import Callable

from PySide6.QtCore import QByteArray, QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QTextDocument
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...diff import (
    LATEST,
    WORKING_COPY,
    Change,
    DocumentDiff,
    ObjectChange,
    ObjectState,
    Position,
    Version,
    word_diff,
)
from ...drawio import qt_svg
from ...schema import Link, Table
from ...service import ServiceError
from ..controller import RepoController
from ..theme import ThemeManager


_CHANGE_ROLE = Qt.ItemDataRole.UserRole + 1
_FIGURE_MAX_WIDTH = 320
_FIGURE_MAX_HEIGHT = 360

# The token each change is coloured with in the list.
_CHANGE_TOKENS = {
    Change.ADDED: "ok",
    Change.DELETED: "err",
    Change.MODIFIED: "warn",
    Change.MOVED: "blue",
    Change.LINK_REVIEWED: "fg_2",
}


# --- background job -------------------------------------------------------


class _CompareSignals(QObject):
    finished = Signal(int, object)  # request number, DocumentDiff
    failed = Signal(int, str)  # request number, message for the user
    crashed = Signal(int, object)  # request number, exception re-raised on the GUI thread


class _CompareJob(QRunnable):
    def __init__(
        self,
        signals: _CompareSignals,
        compare: Callable[[], DocumentDiff],
        request: int,
    ) -> None:
        super().__init__()
        self._signals = signals
        self._compare = compare
        self._request = request

    def run(self) -> None:
        try:
            diff = self._compare()
        except ServiceError as e:
            self._emit("failed", self._request, str(e))
            return
        except Exception as e:  # noqa: BLE001 — reported on the GUI thread
            self._emit("crashed", self._request, e)
            return
        self._emit("finished", self._request, diff)

    def _emit(self, signal: str, *args: object) -> None:
        # The dialog may be closed (and its signals deleted) mid-compare.
        try:
            getattr(self._signals, signal).emit(*args)
        except RuntimeError:
            pass


# --- HTML for the details pane ------------------------------------------


def _mix(a: str, b: str, t: float) -> str:
    """Colour `t` of the way from `a` to `b`."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    ).name()


def _text_html(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def words_html(text_a: str, text_b: str, side: str, tokens: dict[str, str]) -> str:
    """`text_a` (side "a") or `text_b` (side "b") as HTML, with the words
    deleted from A struck through, or the words inserted in B underlined,
    on a tinted background."""
    deleted = _mix(tokens["bg_2"], tokens["err"], 0.3)
    inserted = _mix(tokens["bg_2"], tokens["ok"], 0.3)
    out: list[str] = []
    for op, text in word_diff(text_a, text_b):
        if op == "equal":
            out.append(_text_html(text))
        elif op == "delete" and side == "a":
            out.append(
                f'<span style="background-color:{deleted}; '
                f'text-decoration:line-through">{_text_html(text)}</span>'
            )
        elif op == "insert" and side == "b":
            out.append(
                f'<span style="background-color:{inserted}; '
                f'text-decoration:underline">{_text_html(text)}</span>'
            )
    return "".join(out)


def table_html(table: Table | None) -> str:
    if table is None:
        return "<i>(no table)</i>"
    rows = []
    for row in table.rows:
        tag = "th" if row.header else "td"
        cells = []
        for cell in row.cells:
            span = ""
            if cell.colspan > 1:
                span += f' colspan="{cell.colspan}"'
            if cell.rowspan > 1:
                span += f' rowspan="{cell.rowspan}"'
            cells.append(f"<{tag}{span}>{_text_html(cell.text)}</{tag}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<table border="1" cellspacing="0" cellpadding="3">'
        + "".join(rows)
        + "</table>"
    )


def position_text(pos: Position | None) -> str:
    if pos is None:
        return "(outline unreadable)"
    if not pos.in_outline:
        return "not in the outline"
    where = f"under {pos.parent_id}" if pos.parent_id else "top level"
    return f"{where}, {'after ' + pos.after_id if pos.after_id else 'first'}"


def link_text(link: Link | object) -> str:
    return f"{link.type} → {link.target_doc} / {link.target_id}"  # type: ignore[attr-defined]


def attr_value_html(value: str | None) -> str:
    if value is None:
        return "<i>(not set)</i>"
    if value == "":
        return "<i>(empty)</i>"
    return _text_html(value)


class _Html:
    """Builds the details HTML for one change. Figures become image
    resources named in `images`, for the caller to add to the document."""

    def __init__(self, diff: DocumentDiff, tokens: dict[str, str]) -> None:
        self.diff = diff
        self.tokens = tokens
        self.images: dict[str, bytes] = {}

    def figure(self, name: str, svg: bytes | None) -> str:
        if svg is None:
            return "<i>(no figure)</i>"
        self.images[name] = svg
        return f'<img src="{name}">'

    def page(self, change: ObjectChange) -> str:
        t = self.tokens
        kinds = ", ".join(str(k) for k in change.kinds)
        parts = [
            f"<h3>{html.escape(change.obj_id)} — {html.escape(kinds)}</h3>",
        ]
        err_a, err_b = change.errors
        for side, err in (("From", err_a), ("To", err_b)):
            if err:
                parts.append(
                    f'<p style="color:{t["err"]}">Can\'t read at {side}: '
                    f"{html.escape(err)}</p>"
                )
        if change.before is None or change.after is None:
            parts.append(self._whole(change))
        else:
            parts.append(self._fields(change))
        return "".join(parts)

    def _whole(self, change: ObjectChange) -> str:
        """An added or deleted object: all its fields where it exists."""
        added = change.before is None
        state: ObjectState = change.after if added else change.before  # type: ignore[assignment]
        side, short = ("To", self.diff.short_b) if added else ("From", self.diff.short_a)
        rows: list[tuple[str, str]] = []
        obj = state.obj
        if obj is not None:
            for name in self._attr_order(obj.attrs):
                rows.append((self.diff.label(name), attr_value_html(obj.attrs[name])))
            rows.append(("Description", _text_html(obj.description) or "<i>(empty)</i>"))
            if obj.table is not None:
                rows.append(("Table", table_html(obj.table)))
        if state.figure is not None:
            rows.append(("Figure", self.figure("fig-whole", state.figure)))
        if state.links:
            rows.append((
                "Outgoing links",
                "<br>".join(html.escape(link_text(l)) for l in state.links),
            ))
        rows.append(("Outline position", html.escape(position_text(state.position))))
        body = "".join(
            f"<tr><td><b>{html.escape(k)}</b></td><td>{v}</td></tr>" for k, v in rows
        )
        return (
            f"<p>As it is at {side} ({html.escape(short)}).</p>"
            f'<table cellspacing="0" cellpadding="4">{body}</table>'
        )

    def _attr_order(self, attrs: dict[str, str]) -> list[str]:
        declared = [n for n in self.diff.labels if n in attrs]
        return declared + sorted(n for n in attrs if n not in self.diff.labels)

    def _fields(self, change: ObjectChange) -> str:
        """An object at both versions: each changed field at From and To."""
        assert change.before is not None and change.after is not None
        rows: list[tuple[str, str, str]] = []
        delta = change.delta
        if delta is not None:
            for a in delta.attrs:
                rows.append((
                    self.diff.label(a.name),
                    attr_value_html(a.value_a),
                    attr_value_html(a.value_b),
                ))
            if delta.description is not None:
                d = delta.description
                rows.append((
                    "Description",
                    words_html(d.text_a, d.text_b, "a", self.tokens),
                    words_html(d.text_a, d.text_b, "b", self.tokens),
                ))
            if delta.table is not None:
                rows.append((
                    "Table",
                    table_html(delta.table.table_a),
                    table_html(delta.table.table_b),
                ))
            if delta.figure is not None:
                rows.append((
                    "Figure",
                    self.figure("fig-a", delta.figure.svg_a),
                    self.figure("fig-b", delta.figure.svg_b),
                ))
        if Change.MOVED in change.kinds:
            rows.append((
                "Outline position",
                html.escape(position_text(change.before.position)),
                html.escape(position_text(change.after.position)),
            ))
        parts = []
        if rows:
            head = (
                "<tr><th align=left>Field</th>"
                f"<th align=left>From ({html.escape(self.diff.short_a)})</th>"
                f"<th align=left>To ({html.escape(self.diff.short_b)})</th></tr>"
            )
            body = "".join(
                f'<tr><td valign="top"><b>{html.escape(k)}</b></td>'
                f'<td valign="top">{a}</td><td valign="top">{b}</td></tr>'
                for k, a, b in rows
            )
            parts.append(
                f'<table width="100%" cellspacing="0" cellpadding="4">{head}{body}</table>'
            )
        if delta is not None and not delta.links.is_empty:
            items = [
                f"<li>Added: {html.escape(link_text(l))}</li>" for l in delta.links.added
            ] + [
                f"<li>Removed: {html.escape(link_text(l))}</li>" for l in delta.links.removed
            ] + [
                f"<li>Reviewed: {html.escape(link_text(l))} "
                f"(ack {html.escape(l.ack_a or 'none')} → {html.escape(l.ack_b or 'none')})</li>"
                for l in delta.links.ack_changed
            ]
            parts.append("<p><b>Outgoing links</b></p><ul>" + "".join(items) + "</ul>")
        if not parts:
            parts.append("<p><i>No field changes.</i></p>")
        return "".join(parts)


def figure_image(svg: bytes) -> QImage | None:
    """An SVG figure drawn on white (draw.io assumes a white page), scaled
    down to fit the details pane."""
    renderer = QSvgRenderer(QByteArray(qt_svg(svg)))
    if not renderer.isValid():
        return None
    size = renderer.defaultSize()
    if size.width() <= 1 or size.height() <= 1:
        return None
    scale = min(1.0, _FIGURE_MAX_WIDTH / size.width(), _FIGURE_MAX_HEIGHT / size.height())
    w, h = max(1, round(size.width() * scale)), max(1, round(size.height() * scale))
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return image


def changes_text(diff: DocumentDiff, changes: list[ObjectChange], header: str) -> str:
    """Plain text for the clipboard: a header line, the counts, then one
    tab-separated line per change (id, change, text)."""
    lines = [header, counts_text(diff)]
    for c in changes:
        lines.append(f"{c.obj_id}\t{change_label(c)}\t{c.text}")
    return "\n".join(lines) + "\n"


def counts_text(diff: DocumentDiff) -> str:
    counts = diff.counts()
    parts = [
        f"{counts[Change.ADDED]} added",
        f"{counts[Change.DELETED]} deleted",
        f"{counts[Change.MODIFIED]} modified",
    ]
    if counts[Change.MOVED]:
        parts.append(f"{counts[Change.MOVED]} moved")
    if counts[Change.LINK_REVIEWED]:
        parts.append(f"{counts[Change.LINK_REVIEWED]} link reviewed")
    return ", ".join(parts)


def change_label(change: ObjectChange) -> str:
    text = ", ".join(str(k) for k in change.kinds)
    err_a, err_b = change.errors
    unreadable = [side for side, err in (("From", err_a), ("To", err_b)) if err]
    if unreadable:
        text += f" (unreadable at {' and '.join(unreadable)})"
    return text


# --- the dialog -----------------------------------------------------------


class CompareDialog(QDialog):
    """Compare one document between two versions. Open it with `start`."""

    PROMPT = "Choose a version to compare from."

    def __init__(
        self,
        controller: RepoController,
        doc_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._controller = controller
        self._doc_id = doc_id
        self.diff: DocumentDiff | None = None
        # From and To as shown for `diff`, and for the comparison running.
        self._labels: tuple[str, str] = ("", "")
        self._pending_labels: tuple[str, str] = ("", "")
        self._request = 0
        self._busy = False
        self._versions: list[Version] = []
        self.setWindowTitle(f"Compare — {doc_id}")
        self.resize(1100, 680)

        self._signals = _CompareSignals(self)
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._signals.crashed.connect(self._on_crashed)

        # --- version pickers ---------------------------------------------
        self.from_combo = self._version_combo()
        self.from_combo.lineEdit().setPlaceholderText("Choose a version")
        self.to_combo = self._version_combo()
        self.swap_button = QPushButton("Swap")
        self.swap_button.setToolTip("Swap From and To")
        self.swap_button.clicked.connect(self.swap)
        self.compare_button = QPushButton("Compare")
        self.compare_button.clicked.connect(self.compare)

        pick = QHBoxLayout()
        pick.addWidget(QLabel("From:"))
        pick.addWidget(self.from_combo, 1)
        pick.addWidget(self.swap_button)
        pick.addWidget(QLabel("To:"))
        pick.addWidget(self.to_combo, 1)
        pick.addWidget(self.compare_button)

        # Errors (and the jump hint); the previous result stays below.
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setVisible(False)

        # --- summary, filters, copy ------------------------------------------
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-weight: 600;")
        self.filter_boxes: dict[Change, QCheckBox] = {}
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Show:"))
        for kind in Change:
            box = QCheckBox(str(kind))
            box.setChecked(True)
            box.toggled.connect(self._apply_filters)
            self.filter_boxes[kind] = box
            filters.addWidget(box)
        filters.addStretch(1)
        self.copy_button = QPushButton("Copy list")
        self.copy_button.setToolTip("Copy the listed changes as plain text")
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        self.copy_button.setEnabled(False)
        filters.addWidget(self.copy_button)

        # --- list + details --------------------------------------------------
        self.list = QTreeWidget()
        self.list.setColumnCount(3)
        self.list.setHeaderLabels(["ID", "Change", "Text"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemDoubleClicked.connect(self._on_double_clicked)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.list)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)

        self.empty_label = QLabel(self.PROMPT)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: palette(mid); padding: 24px;")
        self._stack = QStackedWidget()
        self._stack.addWidget(self.empty_label)  # 0: prompt / no differences
        self._stack.addWidget(split)  # 1: results

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        # Enter in a version box compares (`returnPressed`); no button may
        # take it as the dialog's default, least of all Close.
        for button in (
            self.swap_button, self.compare_button, self.copy_button, *buttons.buttons()
        ):
            button.setAutoDefault(False)
            button.setDefault(False)

        layout = QVBoxLayout(self)
        layout.addLayout(pick)
        layout.addWidget(self.message_label)
        layout.addWidget(self.summary_label)
        layout.addLayout(filters)
        layout.addWidget(self._stack, 1)
        layout.addWidget(buttons)

        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    def _version_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMinimumContentsLength(28)
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        combo.activated.connect(lambda *_: self.compare())
        combo.lineEdit().returnPressed.connect(self.compare)
        return combo

    # --- public API -------------------------------------------------------

    def start(self, from_ref: str | None = None, to_ref: str | None = None) -> None:
        """Fill the version pickers and compare. Without refs, From and To
        are the document's defaults (`diff.default_versions`); with no From
        to compare from, the dialog prompts for one."""
        svc = self._controller.require_service()
        try:
            self._versions = svc.compare_versions(self._doc_id)
            default_from, default_to = svc.default_compare_versions(self._doc_id)
        except ServiceError as e:
            self._versions = [Version(WORKING_COPY, "Working copy (uncommitted edits)", "working-copy")]
            default_from, default_to = None, WORKING_COPY
            self._show_message(f"Can't read this document's history: {e}")
        for combo in (self.from_combo, self.to_combo):
            blocked = combo.blockSignals(True)
            combo.clear()
            for v in self._versions:
                combo.addItem(v.label, v.ref)
            combo.blockSignals(blocked)
        self._set_ref(self.from_combo, from_ref if from_ref is not None else default_from)
        self._set_ref(self.to_combo, to_ref if to_ref is not None else default_to)
        self.compare()

    def is_busy(self) -> bool:
        return self._busy

    def ref(self, combo: QComboBox) -> str | None:
        """The version `combo` names: the chosen entry's ref, or the typed
        text (a commit hash, tag or branch name). None when empty."""
        text = combo.currentText().strip()
        if not text:
            return None
        index = combo.findText(text, Qt.MatchFlag.MatchExactly)
        if index >= 0:
            return combo.itemData(index)
        return text

    def swap(self) -> None:
        a = self._combo_state(self.from_combo)
        b = self._combo_state(self.to_combo)
        self._restore_combo_state(self.from_combo, b)
        self._restore_combo_state(self.to_combo, a)
        self.compare()

    def compare(self) -> None:
        """Compare the chosen versions, in the background. Without a From
        version, prompt for one instead."""
        ref_a, ref_b = self.ref(self.from_combo), self.ref(self.to_combo)
        if ref_a is None or ref_b is None:
            if self.diff is None:
                self.empty_label.setText(self.PROMPT)
                self._stack.setCurrentIndex(0)
            else:
                self._show_message(self.PROMPT)
            return
        self._request += 1
        self._busy = True
        # Shown with the result; a failure keeps the previous result's labels.
        self._pending_labels = (
            self.from_combo.currentText().strip(),
            self.to_combo.currentText().strip(),
        )
        self.compare_button.setEnabled(False)
        self.summary_label.setText("Comparing…")
        self.setCursor(Qt.CursorShape.BusyCursor)
        svc = self._controller.require_service()
        doc_id = self._doc_id
        QThreadPool.globalInstance().start(
            _CompareJob(
                self._signals,
                lambda: svc.compare_document(doc_id, ref_a, ref_b),
                self._request,
            )
        )

    def visible_changes(self) -> list[ObjectChange]:
        out = []
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if not item.isHidden():
                out.append(self._change_of(item))
        return out

    def copy_to_clipboard(self) -> str:
        if self.diff is None:
            return ""
        text = changes_text(self.diff, self.visible_changes(), self._header())
        QGuiApplication.clipboard().setText(text)
        return text

    # --- results -------------------------------------------------------------

    def _on_finished(self, request: int, diff: DocumentDiff) -> None:
        if request != self._request:
            return  # a newer comparison is running
        self._end_busy()
        self.message_label.setVisible(False)
        self.diff = diff
        self._labels = self._pending_labels
        self._populate()
        problems = [
            f"Can't read the outline at {side}: {err}. Moved objects aren't shown."
            for side, err in (("From", diff.outline_error_a), ("To", diff.outline_error_b))
            if err
        ]
        if problems:
            self._show_message("\n".join(problems))

    def _on_failed(self, request: int, message: str) -> None:
        if request != self._request:
            return
        self._end_busy()
        # Keep the previous result; say what went wrong above it.
        self._show_message(f"Couldn't compare: {message}")
        if self.diff is not None:
            self.summary_label.setText(self._summary())
        else:
            self.summary_label.clear()

    def _on_crashed(self, request: int, error: object) -> None:
        if request == self._request:
            self._end_busy()
        raise error  # type: ignore[misc]  # the excepthook logs and reports it

    def _end_busy(self) -> None:
        self._busy = False
        self.compare_button.setEnabled(True)
        self.unsetCursor()

    def _populate(self) -> None:
        diff = self.diff
        assert diff is not None
        tokens = ThemeManager.instance().current_tokens()
        self.list.clear()
        self.details.clear()
        for index, change in enumerate(diff.changes):
            item = QTreeWidgetItem([change.obj_id, change_label(change), change.text])
            item.setData(0, _CHANGE_ROLE, index)
            item.setToolTip(2, change.text)
            item.setForeground(1, QColor(tokens[_CHANGE_TOKENS[change.kinds[0]]]))
            self.list.addTopLevelItem(item)
        self.list.resizeColumnToContents(0)
        self.list.resizeColumnToContents(1)
        self.summary_label.setText(self._summary())
        self.copy_button.setEnabled(bool(diff.changes))
        if diff.changes:
            self._stack.setCurrentIndex(1)
            self._apply_filters()
        else:
            self.empty_label.setText(
                f"No differences: {self._doc_id} is the same at "
                f"{self._labels[0]} and {self._labels[1]}."
            )
            self._stack.setCurrentIndex(0)

    def _header(self) -> str:
        return f"{self._doc_id}: {self._labels[0]} → {self._labels[1]}"

    def _summary(self) -> str:
        assert self.diff is not None
        return f"{self._header()}: {counts_text(self.diff)}"

    def _apply_filters(self, *_args) -> None:
        shown = {k for k, box in self.filter_boxes.items() if box.isChecked()}
        first_visible = None
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            hidden = not shown.intersection(self._change_of(item).kinds)
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = item
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.setCurrentItem(first_visible)
            if first_visible is None:
                self.details.clear()

    def _change_of(self, item: QTreeWidgetItem) -> ObjectChange:
        assert self.diff is not None
        return self.diff.changes[item.data(0, _CHANGE_ROLE)]

    def _on_current_changed(self, item: QTreeWidgetItem | None, *_args) -> None:
        if item is None or self.diff is None:
            return
        self._show_details(self._change_of(item))

    def _show_details(self, change: ObjectChange) -> None:
        assert self.diff is not None
        builder = _Html(self.diff, ThemeManager.instance().current_tokens())
        page = builder.page(change)
        doc = QTextDocument(self.details)
        for name, svg in builder.images.items():
            image = figure_image(svg)
            if image is None:
                page = page.replace(f'<img src="{name}">', "<i>(figure can't be drawn)</i>")
            else:
                doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)
        doc.setHtml(page)
        # The browser deletes the document this replaces: it is its parent.
        self.details.setDocument(doc)

    def _on_double_clicked(self, item: QTreeWidgetItem, *_args) -> None:
        obj_id = self._change_of(item).obj_id
        try:
            exists = obj_id in self._controller.document(self._doc_id).objects
        except (KeyError, ServiceError):
            exists = False
        if not exists:
            self._show_message(f"{obj_id} isn't in the open document.")
            return
        self.message_label.setVisible(False)
        self._controller.select_object(self._doc_id, obj_id)

    def _on_theme_changed(self, *_args) -> None:
        if self.diff is None:
            return
        current = self.list.currentItem()
        obj_id = self._change_of(current).obj_id if current is not None else None
        self._populate()  # the colours come from the theme
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if item.text(0) == obj_id and not item.isHidden():
                self.list.setCurrentItem(item)
                break

    # --- helpers ----------------------------------------------------------

    def _show_message(self, text: str) -> None:
        tokens = ThemeManager.instance().current_tokens()
        self.message_label.setStyleSheet(f"color: {tokens['err']};")
        self.message_label.setText(text)
        self.message_label.setVisible(True)

    def _set_ref(self, combo: QComboBox, ref: str | None) -> None:
        """Choose `ref` in `combo`: its entry, the commit entry it is a
        prefix of (a short hash), or else as typed text."""
        blocked = combo.blockSignals(True)
        try:
            if ref is None:
                combo.setCurrentIndex(-1)
                combo.setEditText("")
                return
            index = combo.findData(ref)
            if index < 0 and len(ref) >= 4:
                for i, v in enumerate(self._versions):
                    if v.kind == "commit" and v.ref.startswith(ref):
                        index = i
                        break
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentIndex(-1)
                combo.setEditText(ref)
        finally:
            combo.blockSignals(blocked)

    @staticmethod
    def _combo_state(combo: QComboBox) -> tuple[int, str]:
        return combo.currentIndex(), combo.currentText()

    @staticmethod
    def _restore_combo_state(combo: QComboBox, state: tuple[int, str]) -> None:
        index, text = state
        blocked = combo.blockSignals(True)
        if index >= 0 and combo.itemText(index) == text:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(-1)
            combo.setEditText(text)
        combo.blockSignals(blocked)


__all__ = ["CompareDialog", "LATEST", "WORKING_COPY"]
