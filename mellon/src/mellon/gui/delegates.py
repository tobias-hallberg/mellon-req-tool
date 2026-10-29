"""Item delegates for the objects grid.

An `AttrDelegate` provides a kind-aware editor (combobox for enum/boolean,
spinbox for integer/real, date picker for date) whose configuration comes
from the document's manifest schema. A `MultiLineTextDelegate` provides a
`QPlainTextEdit` editor for the description column so users can type
multiple paragraphs inline. Other text columns fall through to the default
`QLineEdit` editor.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QByteArray,
    QDate,
    QEvent,
    QPersistentModelIndex,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QTextDocument,
    QTextTable,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QApplication,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFrame,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTextEdit,
)

import html as _html

from ..drawio import qt_svg
from ..schema import Table
from .controller import RepoController
from .models.objects_grid import (
    OBJ_DIRTY_ROLE,
    OBJ_FIGURE_PATH_ROLE,
    OBJ_LINK_STATES_ROLE,
    OBJ_TABLE_ROLE,
)
from .theme import ThemeManager
from .widgets.inline_table import InlineTableWidget


_INT_MIN = -(2**31)
_INT_MAX = 2**31 - 1

# QTableView::item { padding: 4px 8px } — kept as module-level constants so
# the description editor and `MultiLineTextDelegate.sizeHint` agree on the
# wrap budget (otherwise the cell paint and the editor would wrap at slightly
# different widths and the row would visibly reflow on edit-open).
_GRID_CELL_PADDING_H = 8
_GRID_CELL_PADDING_V = 4


class AttrDelegate(QStyledItemDelegate):
    """Editor that adapts to the column attribute's `kind` in the current doc.

    - `enum`     -> `QComboBox` populated from the attribute's options
    - `boolean`  -> `QComboBox` with `true`/`false`
    - `integer`  -> `QSpinBox`
    - `real`     -> `QDoubleSpinBox`
    - `date`     -> `QDateEdit` with calendar popup, ISO format
    - anything else (text, missing schema) -> Qt default line-edit

    Opening an editor and leaving it must not write anything: combos carry an
    empty entry so an empty cell stays empty, and the spin/date editors only
    write once the user has changed the value or confirmed it with Enter (the
    0 / today they show for an empty cell is just a starting point).
    """

    def __init__(self, controller: RepoController, attr_name: str, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._attr_name = attr_name

    def createEditor(self, parent, option, index):
        attr = self._attribute_for_current_doc()
        if attr is None:
            return super().createEditor(parent, option, index)
        if attr.kind == "enum":
            cb = QComboBox(parent)
            cb.addItems(["", *attr.options])
            QTimer.singleShot(0, cb.showPopup)
            return cb
        if attr.kind == "boolean":
            cb = QComboBox(parent)
            cb.addItems(["", "true", "false"])
            QTimer.singleShot(0, cb.showPopup)
            return cb
        if attr.kind == "integer":
            sp = QSpinBox(parent)
            sp.setRange(_INT_MIN, _INT_MAX)
            _track_touched(sp, sp.valueChanged)
            return sp
        if attr.kind == "real":
            dsp = QDoubleSpinBox(parent)
            dsp.setRange(-1e308, 1e308)
            dsp.setDecimals(4)
            _track_touched(dsp, dsp.valueChanged)
            return dsp
        if attr.kind == "date":
            de = QDateEdit(parent)
            de.setDisplayFormat("yyyy-MM-dd")
            de.setCalendarPopup(True)
            _track_touched(de, de.dateChanged)
            # Picking the date already shown (e.g. today) counts as a choice.
            _track_touched(de, de.calendarWidget().clicked)
            return de
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        value = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if isinstance(editor, QComboBox):
            i = editor.findText(value)
            if i < 0:
                # A value outside the options (hand edit, merge): list it so
                # opening and leaving the cell doesn't rewrite it.
                editor.addItem(value)
                i = editor.count() - 1
            editor.setCurrentIndex(i)
            return
        if isinstance(editor, QSpinBox):
            try:
                editor.setValue(int(value, 10))
            except (TypeError, ValueError):
                editor.setValue(0)
            editor._touched = False
            return
        if isinstance(editor, QDoubleSpinBox):
            try:
                editor.setValue(float(value))
            except (TypeError, ValueError):
                editor.setValue(0.0)
            editor._touched = False
            return
        if isinstance(editor, QDateEdit):
            qd = QDate.fromString(value, "yyyy-MM-dd") if value else QDate()
            editor.setDate(qd if qd.isValid() else QDate.currentDate())
            editor._touched = False
            return
        super().setEditorData(editor, index)

    def eventFilter(self, editor, event):  # noqa: N802 (Qt override)
        # Enter confirms the shown value even when it wasn't changed.
        if event.type() == QEvent.Type.KeyPress and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            editor._touched = True
        return super().eventFilter(editor, event)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)
            return
        if not getattr(editor, "_touched", True):
            return  # untouched spin/date editor: leave the cell as it was
        if isinstance(editor, QSpinBox):
            model.setData(index, str(editor.value()), Qt.ItemDataRole.EditRole)
            return
        if isinstance(editor, QDoubleSpinBox):
            model.setData(index, str(editor.value()), Qt.ItemDataRole.EditRole)
            return
        if isinstance(editor, QDateEdit):
            model.setData(
                index,
                editor.date().toString("yyyy-MM-dd"),
                Qt.ItemDataRole.EditRole,
            )
            return
        super().setModelData(editor, model, index)

    # --- helpers -------------------------------------------------------

    def _attribute_for_current_doc(self):
        doc_id = self._controller.current_doc_id()
        if doc_id is None:
            return None
        doc = self._controller.document(doc_id)
        return doc.attribute(self._attr_name)


def _track_touched(editor, signal) -> None:
    """Mark `editor` as touched when `signal` fires (see `AttrDelegate`)."""
    signal.connect(lambda *_: setattr(editor, "_touched", True))


# Back-compat alias: tests and any external callers that still import
# `EnumDelegate` get the unified delegate.
EnumDelegate = AttrDelegate


class IdColumnDelegate(QStyledItemDelegate):
    """Render the ID column as a coloured pill: blue when the object's
    on-disk file matches HEAD, yellow when it has working-tree changes.

    The pill fills the cell with a 2 px inset so row selection still shows
    as a frame around it. Border is a darker shade of the same hue.
    """

    _PILL_INSET = 2.5  # half-pixel offsets keep the 1 px stroke crisp
    _PILL_RADIUS = 3.0

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        f = QFont(option.font)
        f.setFamilies(["JetBrains Mono", "Consolas", "Menlo", "monospace"])
        option.font = f

    def paint(self, painter, option, index):
        is_dirty = bool(index.data(OBJ_DIRTY_ROLE))
        tokens = ThemeManager.instance().current_tokens()
        if is_dirty:
            fill = QColor(tokens["warn_bg"])
            border = QColor(tokens["warn"])
        else:
            fill = QColor(tokens["info_bg"])
            border = QColor(tokens["blue"])
        text_color = QColor(tokens["fg_0"])

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(
                option.rect,
                option.palette.brush(QPalette.ColorRole.Highlight),
            )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(
            self._PILL_INSET,
            self._PILL_INSET,
            -self._PILL_INSET,
            -self._PILL_INSET,
        )
        painter.setBrush(fill)
        pen = QPen(border)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, self._PILL_RADIUS, self._PILL_RADIUS)

        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            f = QFont(option.font)
            f.setFamilies(["JetBrains Mono", "Consolas", "Menlo", "monospace"])
            painter.setFont(f)
            painter.setPen(text_color)
            inner = option.rect.adjusted(
                _GRID_CELL_PADDING_H, 0, -_GRID_CELL_PADDING_H, 0
            )
            painter.drawText(
                inner,
                int(
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                ),
                str(text),
            )
        painter.restore()


class StateGutterDelegate(QStyledItemDelegate):
    """Paint crisp anti-aliased link-state markers in the gutter column.

    An object can have outgoing links, incoming links, or both. Each direction
    is rendered as a filled triangle pointing outward from the object:
    `◀` on the left for incoming, `▶` on the right for outgoing. Color picks
    up the worst state in that direction — green (ok), yellow (suspect),
    or red (broken, which drops the arrow shape for a solid disc so the
    "something is wrong" case stays visually distinct).
    """

    _SIZE = 9  # marker bounding-box side
    _HALF_V_SPACING = 6  # vertical distance from cell centre to each marker

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        states = index.data(OBJ_LINK_STATES_ROLE)
        if not states:
            return
        out_state, in_state = states
        if out_state == "none" and in_state == "none":
            return

        tokens = ThemeManager.instance().current_tokens()
        colors = {
            "ok": QColor(tokens["ok"]),
            "suspect": QColor(tokens["warn"]),
            "broken": QColor(tokens["err"]),
        }

        rect = option.rect
        cx = rect.center().x() + 0.5
        cy = rect.center().y() + 0.5

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        # Outgoing on top, incoming below — the two markers are stacked
        # vertically and each keeps its own horizontal-pointing arrow shape.
        if out_state != "none":
            self._draw_marker(
                painter, cx, cy - self._HALF_V_SPACING, out_state, colors, pointing_left=False,
            )
        if in_state != "none":
            self._draw_marker(
                painter, cx, cy + self._HALF_V_SPACING, in_state, colors, pointing_left=True,
            )
        painter.restore()

    def _draw_marker(self, painter, cx, cy, state, colors, *, pointing_left):
        painter.setBrush(colors[state])
        half = self._SIZE / 2
        if state == "broken":
            # Solid disc — no direction cue, but "broken" is a hard-error flag.
            painter.drawEllipse(QPointF(cx, cy), half - 0.5, half - 0.5)
            return
        if pointing_left:
            poly = QPolygonF([
                QPointF(cx + half, cy - half),
                QPointF(cx + half, cy + half),
                QPointF(cx - half, cy),
            ])
        else:
            poly = QPolygonF([
                QPointF(cx - half, cy - half),
                QPointF(cx - half, cy + half),
                QPointF(cx + half, cy),
            ])
        painter.drawPolygon(poly)


class _GridDescriptionEditor(QTextEdit):
    """In-cell multi-line editor for the description column.

    Uses QTextEdit (not QPlainTextEdit) because its QTextDocumentLayout reports
    a correct word-wrapped height via `document().size().height()` — the
    plain-text layout reports `blockCount × lineHeight` and ignores wrap, so
    the editor used to collapse to one line on open.

    Enter inserts a newline (default). Ctrl+Enter commits. Esc cancels. Tab
    leaves the editor so grid navigation still works.
    """

    commit_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        # Override the global `QTextEdit { border, padding, border-radius }`
        # rule from theme.py — its 1px border + 3px/6px padding would inset the
        # text differently from the painted cell. Viewport margins absorb the
        # cell-padding budget instead.
        self.setObjectName("GridDescriptionEditor")
        self.setStyleSheet(
            "QTextEdit#GridDescriptionEditor {"
            " border: 0; padding: 0; border-radius: 0;"
            "}"
        )
        self.document().setDocumentMargin(0)
        # On top of the QSS cell padding, Qt's CE_ItemViewItem inserts a small
        # style-driven inset before drawing the painted text — horizontal is
        # `PM_FocusFrameHMargin + 1`, vertical is `PM_FocusFrameVMargin - 1`
        # (the -1 compensates for QTextDocument's first-line leading, which
        # the painted cell's style rendering doesn't apply). Match it so the
        # editor's first character lands on the same pixel as the painted text.
        s = self.style()
        h_extra = s.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin) + 1
        v_extra = s.pixelMetric(QStyle.PixelMetric.PM_FocusFrameVMargin) - 1
        self.setViewportMargins(
            _GRID_CELL_PADDING_H + h_extra,
            _GRID_CELL_PADDING_V + v_extra,
            _GRID_CELL_PADDING_H + h_extra,
            _GRID_CELL_PADDING_V + v_extra,
        )

    def content_height(self) -> int:
        """Pixel height needed to show the full document without scrolling."""
        vp_w = self.viewport().width()
        if vp_w <= 0:
            vp_w = max(self.width(), 1)
        doc = self.document()
        doc.setTextWidth(vp_w)
        # +1 guards against off-by-one clipping on the last wrapped line.
        return int(doc.size().height()) + 1

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if mods & Qt.KeyboardModifier.ControlModifier:
                self.commit_requested.emit()
                event.accept()
                return
            # Plain Enter falls through to QTextEdit — inserts newline.
        elif key == Qt.Key.Key_Escape:
            self.cancel_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


_FIGURE_GAP = 6  # pixels between the description text and the thumbnail
_FIGURE_MAX_HEIGHT = 240  # cap on rendered thumbnail height
_FIGURE_CACHE_LIMIT = 64  # bounded LRU-ish cap on the per-delegate pixmap cache
_TABLE_CACHE_LIMIT = 64  # bounded cap on the grid-preview pixmap cache


def _table_to_html(table: Table, caption: str, header_bg: str) -> str:
    """Build an HTML rendering of `table` for the read-only grid preview.

    QTextDocument's rich-text engine handles colspan/rowspan, borders, per-cell
    alignment, and wrapping natively, so the painted preview reuses the same
    QTextDocument machinery the description text already uses. Covered grid
    slots are simply omitted (HTML colspan/rowspan fills them), mirroring the
    on-disk model. Colors come from inline attributes + a default stylesheet so
    the preview is themed regardless of the view palette.
    """
    ncols = len(table.columns)
    aligns = [c.align for c in table.columns]
    parts: list[str] = []
    if caption:
        parts.append(f"<p><b>{_html.escape(caption)}</b></p>")
    parts.append('<table border="1" cellspacing="0" cellpadding="4" width="100%">')
    carry = [0] * ncols
    for row in table.rows:
        parts.append("<tr>")
        c = 0
        cell_iter = iter(row.cells)
        while c < ncols:
            if carry[c] > 0:
                carry[c] -= 1
                c += 1
                continue
            try:
                cell = next(cell_iter)
            except StopIteration:
                break
            cs = min(max(cell.colspan, 1), ncols - c)
            rs = max(cell.rowspan, 1)
            attrs = f' align="{aligns[c]}"'
            if cs != 1:
                attrs += f' colspan="{cs}"'
            if rs != 1:
                attrs += f' rowspan="{rs}"'
            if row.header:
                attrs += f' bgcolor="{header_bg}"'
            content = _html.escape(cell.text).replace("\n", "<br>")
            if row.header and content:
                content = f"<b>{content}</b>"
            parts.append(f"<td{attrs}>{content or '&nbsp;'}</td>")
            if rs > 1:
                for k in range(c, c + cs):
                    carry[k] = rs - 1
            c += cs
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _table_document(table: Table, caption: str, width: int, font: QFont) -> QTextDocument:
    """A QTextDocument rendering of `table`, themed via a default stylesheet."""
    tokens = ThemeManager.instance().current_tokens()
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(font)
    doc.setDefaultStyleSheet(
        f"td {{ color: {tokens['fg_0']}; border-color: {tokens['border_2']}; }} "
        f"p {{ color: {tokens['fg_0']}; margin: 0 0 4px 0; }}"
    )
    doc.setHtml(_table_to_html(table, caption, tokens["bg_3"]))
    doc.setTextWidth(width)
    return doc


class MultiLineTextDelegate(QStyledItemDelegate):
    """Use a QTextEdit for cell editing so descriptions can hold multiple
    paragraphs.

    The editor overlays the cell seamlessly (no frame, matching font) and the
    underlying row grows as the user types a new line. Ctrl+Enter commits,
    Esc cancels.

    When the model exposes `OBJ_FIGURE_PATH_ROLE` for a row, the delegate
    also renders the SVG thumbnail beneath the wrapped description text and
    grows the row accordingly. The thumbnail width tracks the column width,
    capped by a max height so very tall figures don't dominate the grid.

    When `OBJ_TABLE_ROLE` is set, the row paints a read-only table preview and
    its editor is an `InlineTableWidget`. `tableEditorOpened`/`tableEditorClosed`
    let the window track the live editor so the toolbar "Table" menu can drive
    it.
    """

    tableEditorOpened = Signal(object)
    tableEditorClosed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # (figure_path, mtime_ns, target_width) -> rendered pixmap. Keys
        # include mtime so an in-place figure replacement invalidates the
        # cache without an explicit clear.
        self._pixmap_cache: dict[tuple[str, int, int], QPixmap] = {}
        # Table grid preview: rendered from the SAME widget the editor uses, so
        # view and edit share one engine. Keyed on (table, wrap_w, theme tint);
        # the tint defeats stale renders after a theme switch.
        self._table_cache: dict[tuple, tuple[QPixmap, int]] = {}
        self._preview_table: InlineTableWidget | None = None

    def _thumbnail(self, figure_path: str, target_width: int) -> QPixmap | None:
        """Render `figure_path` to a `QPixmap` sized to fit `target_width`.

        Returns None if the file is missing or not a valid SVG. Caches by
        (path, mtime, width) so scrolling and repaint storms don't re-render.
        The SVG goes through `qt_svg` first so draw.io figures keep their
        colours and labels, and is painted on white: draw.io diagrams assume a
        white page and would vanish against the dark theme.
        """
        from pathlib import Path as _Path
        try:
            mtime_ns = _Path(figure_path).stat().st_mtime_ns
        except OSError:
            return None
        key = (figure_path, mtime_ns, target_width)
        cached = self._pixmap_cache.get(key)
        if cached is not None:
            return cached

        try:
            data = _Path(figure_path).read_bytes()
        except OSError:
            return None
        renderer = QSvgRenderer(QByteArray(qt_svg(data)))
        if not renderer.isValid():
            return None
        default_size = renderer.defaultSize()
        # A blank draw.io figure is 1×1 px until something is drawn in it;
        # no thumbnail rather than a row-wide empty square.
        if default_size.width() <= 1 or default_size.height() <= 1:
            return None

        aspect = default_size.height() / default_size.width()
        w = max(target_width, 1)
        h = max(int(round(w * aspect)), 1)
        if h > _FIGURE_MAX_HEIGHT:
            h = _FIGURE_MAX_HEIGHT
            w = max(int(round(h / aspect)), 1)

        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.white)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter)
        painter.end()

        if len(self._pixmap_cache) >= _FIGURE_CACHE_LIMIT:
            self._pixmap_cache.clear()
        self._pixmap_cache[key] = pm
        return pm

    def sizeHint(self, option, index):
        table = index.data(OBJ_TABLE_ROLE)
        if table is not None:
            wrap_w = max(option.rect.width() - 2 * _GRID_CELL_PADDING_H, 1)
            caption = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            caption_offset = self._caption_offset(table, caption, wrap_w, option.font)
            _pm, grid_h = self._grid_render(table, wrap_w, option.font)
            return QSize(
                option.rect.width(),
                caption_offset + grid_h + 2 * _GRID_CELL_PADDING_V,
            )
        # Measure the wrapped text the same way `_GridDescriptionEditor` does
        # so the row is tall enough to show every line without clipping. Qt's
        # default sizeHint underestimates wrapped multi-line plain text, which
        # leaves the painted cell short — the trailing line then ellipsises.
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        figure_path = index.data(OBJ_FIGURE_PATH_ROLE)
        if not text and not figure_path:
            return super().sizeHint(option, index)
        wrap_w = max(option.rect.width() - 2 * _GRID_CELL_PADDING_H, 1)
        text_h = 0
        if text:
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(option.font)
            doc.setPlainText(text)
            doc.setTextWidth(wrap_w)
            text_h = int(doc.size().height())
        # +1 guards against an off-by-one clip on the last wrapped line, same
        # as `_GridDescriptionEditor.content_height`.
        h = text_h + 2 * _GRID_CELL_PADDING_V + 1
        if figure_path:
            pm = self._thumbnail(str(figure_path), wrap_w)
            if pm is not None:
                h += pm.height() + _FIGURE_GAP
        return QSize(option.rect.width(), h)

    def paint(self, painter, option, index):
        table = index.data(OBJ_TABLE_ROLE)
        if table is not None:
            self._paint_table(painter, option, index, table)
            return

        figure_path = index.data(OBJ_FIGURE_PATH_ROLE)
        if not figure_path:
            super().paint(painter, option, index)
            return

        wrap_w = max(option.rect.width() - 2 * _GRID_CELL_PADDING_H, 1)
        pm = self._thumbnail(str(figure_path), wrap_w)
        if pm is None:
            super().paint(painter, option, index)
            return

        # Step 1: paint the cell background (selection / hover / alt-row) using
        # the style. We strip the text from the option so the style doesn't
        # also draw it — we paint text manually below to control its top
        # alignment relative to the thumbnail.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget
        )

        # Step 2: paint description text via QTextDocument so wrapping matches
        # `sizeHint` and the editor exactly. Top-aligned within the cell.
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        text_h = 0
        if text:
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(option.font)
            doc.setPlainText(text)
            doc.setTextWidth(wrap_w)
            text_h = int(doc.size().height())

            painter.save()
            painter.translate(
                option.rect.left() + _GRID_CELL_PADDING_H,
                option.rect.top() + _GRID_CELL_PADDING_V,
            )
            ctx = QAbstractTextDocumentLayout.PaintContext()
            ctx.palette = option.palette
            text_color = option.palette.color(
                QPalette.ColorRole.HighlightedText
                if option.state & QStyle.StateFlag.State_Selected
                else QPalette.ColorRole.Text
            )
            ctx.palette.setColor(QPalette.ColorRole.Text, text_color)
            ctx.clip = QRectF(0, 0, wrap_w, text_h)
            doc.documentLayout().draw(painter, ctx)
            painter.restore()

        # Step 3: paint the thumbnail beneath the text.
        gap = _FIGURE_GAP if text_h > 0 else 0
        thumb_x = option.rect.left() + _GRID_CELL_PADDING_H
        thumb_y = option.rect.top() + _GRID_CELL_PADDING_V + text_h + gap
        painter.drawPixmap(thumb_x, thumb_y, pm)

    def _paint_table(self, painter, option, index, table) -> None:
        # Background (selection / hover / alt-row) via the style.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = option.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget
        )

        wrap_w = max(option.rect.width() - 2 * _GRID_CELL_PADDING_H, 1)
        caption = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        left = option.rect.left() + _GRID_CELL_PADDING_H
        top = option.rect.top() + _GRID_CELL_PADDING_V
        caption_offset = self._caption_offset(table, caption, wrap_w, option.font)

        # Caption: the same QTextDocument render used in edit, clipped so only
        # the caption (not the HTML table) shows. The grid is drawn below it.
        if caption_offset > 0:
            doc = _table_document(table, caption, wrap_w, option.font)
            painter.save()
            painter.translate(left, top)
            ctx = QAbstractTextDocumentLayout.PaintContext()
            ctx.palette = option.palette
            ctx.clip = QRectF(0, 0, wrap_w, caption_offset)
            doc.documentLayout().draw(painter, ctx)
            painter.restore()

        # Grid: a snapshot of the SAME widget the editor uses — one engine, so
        # view and edit are identical.
        pm, _grid_h = self._grid_render(table, wrap_w, option.font)
        painter.drawPixmap(left, top + caption_offset, pm)

    def figure_rect(self, rect, font, index) -> QRect | None:
        """Return the thumbnail's bounding rect within `rect` (cell viewport
        coords), or None when the row has no rendered figure.

        Mirrors the geometry in `paint()` step 3 so callers can hit-test a
        click against the thumbnail (e.g. double-click to open it full-size).
        """
        figure_path = index.data(OBJ_FIGURE_PATH_ROLE)
        if not figure_path:
            return None
        wrap_w = max(rect.width() - 2 * _GRID_CELL_PADDING_H, 1)
        pm = self._thumbnail(str(figure_path), wrap_w)
        if pm is None:
            return None
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        text_h = 0
        if text:
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(font)
            doc.setPlainText(text)
            doc.setTextWidth(wrap_w)
            text_h = int(doc.size().height())
        gap = _FIGURE_GAP if text_h > 0 else 0
        thumb_x = rect.left() + _GRID_CELL_PADDING_H
        thumb_y = rect.top() + _GRID_CELL_PADDING_V + text_h + gap
        return QRect(thumb_x, thumb_y, pm.width(), pm.height())

    def createEditor(self, parent, option, index):
        if index.data(OBJ_TABLE_ROLE) is not None:
            ed = InlineTableWidget(parent)
            # Match the view font so the editor's measurements line up with the
            # preview's (which `sizeHint` measures with `option.font`). The
            # caption is painted by the delegate (not part of the editor), so
            # there's no caption font to match here.
            ed.setFont(option.font)
            ed.table.setFont(option.font)
            ed.commit_requested.connect(lambda: self._commit_and_close(ed))
            ed.cancel_requested.connect(
                lambda: self.closeEditor.emit(
                    ed, QAbstractItemDelegate.EndEditHint.RevertModelCache
                )
            )
            ed._persistent_index = QPersistentModelIndex(index)
            # Re-fit geometry + columns + row height to match the preview after
            # any structural edit (add row, merge, …) rebuilds the grid.
            ed.geometry_changed.connect(lambda: self._relayout_table_editor(ed))
            # Let the window track the live editor for the toolbar Table menu.
            ed.destroyed.connect(lambda *_: self.tableEditorClosed.emit())
            self.tableEditorOpened.emit(ed)
            return ed
        ed = _GridDescriptionEditor(parent)
        # Match the cell's font so wrapped line counts agree pixel-for-pixel.
        # `setFont` alone isn't enough: QStyleSheetStyle silently reverts the
        # widget font (because the global QSS rule matches QTextEdit), but it
        # leaves the document's default font alone — and that's what controls
        # the actual rendered text in a QTextEdit.
        ed.setFont(option.font)
        ed.document().setDefaultFont(option.font)
        ed.commit_requested.connect(lambda: self._commit_and_close(ed))
        ed.cancel_requested.connect(
            lambda: self.closeEditor.emit(
                ed, QAbstractItemDelegate.EndEditHint.RevertModelCache
            )
        )
        ed._persistent_index = QPersistentModelIndex(index)
        ed.document().contentsChanged.connect(lambda: self._grow(ed))
        return ed

    def _commit_and_close(self, editor) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(
            editor, QAbstractItemDelegate.EndEditHint.SubmitModelCache
        )

    def setEditorData(self, editor, index) -> None:
        if isinstance(editor, InlineTableWidget):
            value = index.data(Qt.ItemDataRole.EditRole)
            caption = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            editor.set_table(
                value if isinstance(value, Table) else Table(), caption=caption
            )
            return
        value = index.data(Qt.ItemDataRole.EditRole) or ""
        editor.setPlainText(str(value))

    def setModelData(self, editor, model, index) -> None:
        if isinstance(editor, InlineTableWidget):
            model.setData(index, editor.to_table(), Qt.ItemDataRole.EditRole)
            return
        model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index) -> None:
        if isinstance(editor, InlineTableWidget):
            # The table editor overlays the painted preview exactly — same
            # padded content rect, same column fill, same row height. The view
            # hands us the correct cell rect in `option.rect`; stash it so the
            # post-structural-edit re-layout (no `option`) reuses the same
            # left/width (only the height changes when rows are added).
            editor._cell_rect = QRect(option.rect)
            self._relayout_table_editor(editor)
            return
        # Set width first from the cell rect, then measure content height at
        # that width, then set final geometry. Doing it synchronously (rather
        # than deferring via QTimer) avoids a one-frame "collapsed" editor
        # flash when editing opens.
        rect = QRect(option.rect)
        editor.setGeometry(rect)
        self._grow(editor)

    def _table_content_height(self, table, caption, wrap_w, font) -> int:
        """Pixel height of the rendered table (caption + grid) at `wrap_w`.

        Single source of truth for the table cell's height — used by both
        `sizeHint` (the preview's row height) and `_relayout_table_editor` (the
        live editor), so view and edit never disagree vertically.
        """
        return int(_table_document(table, caption, wrap_w, font).size().height()) + 1

    def _table_column_widths(self, table, caption, wrap_w, font) -> list[int] | None:
        """The per-column pixel widths the preview's QTextDocument computed.

        Lets the editor use the SAME widths the preview renders, so columns line
        up and text wraps identically (matching row heights). Returns None if the
        table can't be located or a column is only ever spanned (caller falls
        back to content scaling).
        """
        doc = _table_document(table, caption, wrap_w, font)
        qtable = None
        for frame in doc.rootFrame().childFrames():
            if isinstance(frame, QTextTable):
                qtable = frame
                break
        if qtable is None or qtable.columns() != len(table.columns):
            return None
        layout = doc.documentLayout()
        ncols = qtable.columns()
        lefts: list[float | None] = [None] * ncols
        for r in range(qtable.rows()):
            for c in range(ncols):
                if lefts[c] is not None:
                    continue
                cell = qtable.cellAt(r, c)
                if cell.column() == c and cell.columnSpan() == 1:
                    block = cell.firstCursorPosition().block()
                    lefts[c] = layout.blockBoundingRect(block).left()
            if all(x is not None for x in lefts):
                break
        if any(x is None for x in lefts):
            return None
        table_right = layout.frameBoundingRect(qtable).right()
        widths: list[int] = []
        for i in range(ncols):
            right = lefts[i + 1] if i + 1 < ncols else table_right
            widths.append(max(int(round(right - lefts[i])), 1))
        widths[-1] = max(widths[-1] + wrap_w - sum(widths), 1)  # sum to wrap_w
        return widths

    def _caption_offset(self, table, caption, wrap_w, font) -> int:
        """Height of the painted caption region (caption text + gap) at `wrap_w`.

        Measured from the QTextDocument that paints the caption, so the grid
        starts exactly where the caption ends. 0 when there's no caption.
        """
        if not caption:
            return 0
        full_h = self._table_content_height(table, caption, wrap_w, font)
        table_h = self._table_content_height(table, "", wrap_w, font)
        return max(full_h - table_h, 0)

    def _ensure_preview_table(self):
        """The reusable, read-only `InlineTableWidget` used to render previews.

        Rendering the preview from the SAME widget the editor uses is what makes
        view and edit identical — one rendering engine.
        """
        if self._preview_table is None:
            # PARENTLESS on purpose: a QTableView paints its child widgets, so a
            # preview parented to the view shows up at the top-left as a stray
            # duplicate table. With no parent it's never in the view's hierarchy
            # — only ever `grab()`bed. Theming still applies via the global app
            # stylesheet + the widget's own ThemeManager token reads.
            pv = InlineTableWidget(None, read_only=True)
            pv.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            self._preview_table = pv
        return self._preview_table

    def _grid_render(self, table, wrap_w, font):
        """Render `table`'s grid (no caption) to a `(QPixmap, grid_h)`, cached.

        Configures the reusable preview widget exactly like the editor (same
        balanced column widths, same font) and grabs it, so the preview pixmap
        and the live editor are pixel-identical at any DPI.
        """
        tokens = ThemeManager.instance().current_tokens()
        tint = (tokens["fg_0"], tokens["border_2"], tokens["bg_3"])
        key = (table, wrap_w, tint)
        cached = self._table_cache.get(key)
        if cached is not None:
            return cached

        pv = self._ensure_preview_table()
        pv.setFont(font)
        pv.table.setFont(font)
        pv.set_table(table)
        try:
            col_widths = self._table_column_widths(table, "", wrap_w, font)
        except Exception:
            col_widths = None
        pv.apply_layout(wrap_w, col_widths)
        grid_h = pv.content_height()
        pv.resize(wrap_w, grid_h)
        pm = pv.grab()

        if len(self._table_cache) >= _TABLE_CACHE_LIMIT:
            self._table_cache.clear()
        self._table_cache[key] = (pm, grid_h)
        return pm, grid_h

    def _relayout_table_editor(self, editor) -> None:
        """Size the inline table editor to overlay the painted preview's GRID.

        The caption is painted by the delegate (one QTextDocument render path
        for view and edit), so the editor covers only the grid region *below*
        it: it starts at the caption's bottom (`caption_offset`) and is as tall
        as the table alone. Columns fill the width with the preview's exact
        widths. The view row height stays `full_h + 2*pad` so it matches
        `sizeHint`.
        """
        tv = self._table_from_editor(editor)
        idx = getattr(editor, "_persistent_index", None)
        cell = getattr(editor, "_cell_rect", None)
        if tv is None or idx is None or not idx.isValid() or cell is None:
            return
        if cell.width() <= 0:
            return
        wrap_w = max(cell.width() - 2 * _GRID_CELL_PADDING_H, 1)
        table = editor.to_table()
        font = editor.font()
        try:
            col_widths = self._table_column_widths(table, "", wrap_w, font)
        except Exception:
            col_widths = None
        editor.apply_layout(wrap_w, col_widths)
        # Caption is painted by the delegate; the editor covers only the grid
        # region below it. Grid height comes from the editor widget itself —
        # the same value the preview pixmap uses (same widget + widths + font),
        # so there's no jump between view and edit.
        caption_offset = self._caption_offset(table, editor.caption(), wrap_w, font)
        grid_h = editor.content_height()
        row_h = caption_offset + grid_h + 2 * _GRID_CELL_PADDING_V
        if tv.rowHeight(idx.row()) != row_h:
            tv.setRowHeight(idx.row(), row_h)
        editor.setGeometry(
            cell.left() + _GRID_CELL_PADDING_H,
            cell.top() + _GRID_CELL_PADDING_V + caption_offset,
            wrap_w,
            grid_h,
        )

    def _grow(self, editor) -> None:
        """Resize the editor and its underlying table row to fit content."""
        # Skip if the editor hasn't been laid out yet. `setPlainText` fires
        # `contentsChanged` during `setEditorData`, which happens before the
        # view assigns geometry — measuring at width 0 would wrap every char
        # and briefly blow up the row.
        if editor.width() <= 0:
            return
        editor_h = editor.content_height()
        table = self._table_from_editor(editor)
        idx = getattr(editor, "_persistent_index", None)

        # The row must stay at least as tall as whatever other column already
        # required (e.g. a 3-line rationale). `sizeHintForRow` gives the
        # view's wrap-aware hint across all cells in the row, based on the
        # currently-stored model data. Taking the max with the live editor
        # content height means:
        #   - editor grows → row grows
        #   - editor shrinks below sibling cells → row stays tall
        row_h = editor_h
        if table is not None and idx is not None and idx.isValid():
            row = idx.row()
            natural_h = table.sizeHintForRow(row)
            row_h = max(editor_h, natural_h)
            if table.rowHeight(row) != row_h:
                table.setRowHeight(row, row_h)

        geo = editor.geometry()
        if geo.height() != row_h:
            geo.setHeight(row_h)
            editor.setGeometry(geo)

    @staticmethod
    def _table_from_editor(editor) -> QTableView | None:
        # Qt parents an item-view editor to the view's viewport; the viewport's
        # parent is the QTableView itself.
        vp = editor.parentWidget()
        if vp is None:
            return None
        parent = vp.parentWidget()
        return parent if isinstance(parent, QTableView) else None
