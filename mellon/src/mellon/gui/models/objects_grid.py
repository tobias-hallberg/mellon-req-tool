"""Objects-grid model: the DOORS-style flat table.

Rows are the current document's objects in outline pre-order.

Columns are split into **system attributes** (always present, fixed positions
0..3) and **user attributes** (declared per-document in the manifest).

  System: state (gutter), id, description, type
  User:   every attribute in `doc.attributes` except `type`, in declared order

The state, id, and description columns are not real attributes — they draw
values from `obj.id` and `obj.description`. The `type` system column reads
from `obj.attrs["type"]` like any user-attribute column; it's "system" only
in the sense that it always has a fixed position and label.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ...schema import Document, Table, attribute_display_name
from ...service import ServiceError
from ..controller import RepoController


SYSTEM_COLUMNS: tuple[str, ...] = ("state", "id", "heading_or_text", "type")
# Labels for the "virtual" grid columns that don't map to real attributes.
# The "type" column reads from `obj.attrs["type"]`, so its label comes from
# `attribute_display_name("type", ...)` like any other attribute.
SYSTEM_LABELS: dict[str, str] = {
    "state": "",
    "id": "ID",
    "heading_or_text": "Description",
}
_NEVER_HIDEABLE: frozenset[str] = frozenset(SYSTEM_COLUMNS)

OBJ_ID_ROLE = Qt.ItemDataRole.UserRole + 1
OBJ_STATE_ROLE = Qt.ItemDataRole.UserRole + 2
# Per-direction link state: tuple `(outgoing, incoming)` with values in
# {"none", "ok", "suspect", "broken"}. Drives the two-triangle gutter marker.
OBJ_LINK_STATES_ROLE = Qt.ItemDataRole.UserRole + 3
# Bool: object's `objects/<id>.xml` or `links/<id>.xml` differs from HEAD.
# Drives the blue/yellow id-column pill.
OBJ_DIRTY_ROLE = Qt.ItemDataRole.UserRole + 4
# Absolute path (str) to the object's SVG figure when one is attached, else
# None. Only meaningful on the description column; the delegate consumes this
# to render an inline thumbnail beneath the description text.
OBJ_FIGURE_PATH_ROLE = Qt.ItemDataRole.UserRole + 5
# The object's `Table` when it carries one, else None. Only meaningful on the
# description column; the delegate paints it as an inline grid (preview) and
# opens an editable grid editor for it. The description doubles as the caption.
OBJ_TABLE_ROLE = Qt.ItemDataRole.UserRole + 6

# The state gutter is painted by `StateGutterDelegate` (see delegates.py); the
# model reports the per-direction states via `OBJ_LINK_STATES_ROLE` and keeps
# `OBJ_STATE_ROLE` as the outgoing-only summary the suspect filter reads.

_STATE_LABELS = {"ok": "OK", "suspect": "suspect", "broken": "broken"}

# Shown when an edit is refused because the document changed on disk under
# the window (the "Changed on disk" banner is up). Writing now could overwrite
# the external change with the window's cached view.
STALE_EDIT_MESSAGE = "Changed on disk. Reload first (F5)."


@dataclass(frozen=True)
class _Row:
    obj_id: str
    heading_or_text: str


def _columns_for_doc(doc: Document | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (column-names, header-labels) for `doc`.

    System columns first; manifest attributes follow in declared order, with
    `type` skipped because it already occupies a system slot. Headers use
    `attribute_display_name` so the manifest's `<attribute label=...>` wins
    where set, with system defaults filling in for omitted labels.
    """
    cols = list(SYSTEM_COLUMNS)
    labels: list[str] = []
    for c in SYSTEM_COLUMNS:
        if c in SYSTEM_LABELS:
            labels.append(SYSTEM_LABELS[c])
        else:
            labels.append(
                attribute_display_name(c, doc.attribute(c) if doc else None)
            )
    if doc is not None:
        for attr in doc.attributes:
            if attr.name == "type":
                continue
            cols.append(attr.name)
            labels.append(attribute_display_name(attr.name, attr))
    return tuple(cols), tuple(labels)


class ObjectsGridModel(QAbstractTableModel):
    def __init__(self, controller: RepoController) -> None:
        super().__init__()
        self._controller = controller
        self._rows: list[_Row] = []
        self._doc_id: str | None = None
        self._link_states: dict[str, tuple[str, str]] = {}
        self._dirty_ids: frozenset[str] = frozenset()
        self._read_only = False
        self._stale = False
        # The document as of the last rebuild, and whether it has since
        # stopped loading (see `_document`).
        self._shown_doc: Document | None = None
        self._unloadable = False
        self._columns, self._labels = _columns_for_doc(None)
        controller.document_selected.connect(self._rebuild)

    def set_read_only(self, on: bool) -> None:
        """Strip ItemIsEditable from `flags()` and refuse `setData` writes
        while read-only is on."""
        self._read_only = bool(on)

    def set_stale(self, on: bool) -> None:
        """Strip ItemIsEditable and refuse `setData` writes while the
        "Changed on disk" banner is up."""
        self._stale = bool(on)

    # --- column introspection ------------------------------------------

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    def column_name(self, index: int) -> str | None:
        if 0 <= index < len(self._columns):
            return self._columns[index]
        return None

    def column_index(self, name: str) -> int:
        """Return the position of column `name`, or -1 if not in the schema."""
        try:
            return self._columns.index(name)
        except ValueError:
            return -1

    def is_user_attribute_column(self, index: int) -> bool:
        """True for indices that belong to a manifest-declared user attribute."""
        return (
            0 <= index < len(self._columns)
            and self._columns[index] not in _NEVER_HIDEABLE
        )

    # --- Qt model protocol ----------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._labels):
                return self._labels[section]
            return None
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = self._columns[index.column()]
        if role == OBJ_ID_ROLE:
            return row.obj_id
        if role == OBJ_STATE_ROLE:
            return self._state_for(row.obj_id)
        if role == OBJ_LINK_STATES_ROLE:
            return self._link_states.get(row.obj_id, ("none", "none"))
        if role == OBJ_DIRTY_ROLE:
            return row.obj_id in self._dirty_ids
        if role == OBJ_FIGURE_PATH_ROLE:
            if col != "heading_or_text" or self._unloadable:
                return None
            doc = self._document()
            obj = doc.objects.get(row.obj_id) if doc else None
            if obj is None or obj.figure_path is None:
                return None
            try:
                folder = self._controller.folder(self._doc_id)
            except (ServiceError, KeyError):
                return None
            return str(folder / obj.figure_path)
        if role == OBJ_TABLE_ROLE:
            if col != "heading_or_text":
                return None
            doc = self._document()
            obj = doc.objects.get(row.obj_id) if doc else None
            return obj.table if obj is not None else None
        # EditRole returns the raw value (no indent on id, no truncation) so
        # an editor widget sees the real string.
        if role == Qt.ItemDataRole.EditRole:
            if col in ("state", "id"):
                return row.obj_id
            if col == "heading_or_text":
                doc = self._document()
                obj = doc.objects.get(row.obj_id) if doc else None
                if obj is None:
                    return ""
                # A table object's editor is the inline grid; hand it the Table
                # (the description-string editor never opens for these rows).
                if obj.table is not None:
                    return obj.table
                return obj.description
            doc = self._document()
            obj = doc.objects.get(row.obj_id) if doc else None
            return obj.attrs.get(col, "") if obj else ""

        # State gutter: painted by `StateGutterDelegate`; model only reports
        # the state (for the delegate) and a tooltip.
        if col == "state":
            if role == Qt.ItemDataRole.ToolTipRole:
                out, inc = self._link_states.get(row.obj_id, ("none", "none"))
                parts: list[str] = []
                if out != "none":
                    parts.append(f"Outgoing links: {_STATE_LABELS[out]}")
                if inc != "none":
                    parts.append(f"Incoming links: {_STATE_LABELS[inc]}")
                return "\n".join(parts) if parts else None
            return None

        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if col == "id":
            return row.obj_id
        if col == "heading_or_text":
            return row.heading_or_text
        # Attribute lookup
        doc = self._document()
        if doc is None:
            return ""
        obj = doc.objects.get(row.obj_id)
        if obj is None:
            return ""
        return obj.attrs.get(col, "")

    def _document(self) -> Document | None:
        """The shown document, read live from the controller's cache.

        When it no longer loads (deleted, or broken on disk, e.g. by a pull
        with a merge conflict) the copy from the last rebuild stays on screen
        behind the stale banner: painting must not raise, nor re-parse the
        broken files for every cell. The next rebuild re-reads it.
        """
        if self._doc_id is None:
            return None
        if not self._unloadable:
            try:
                return self._controller.document(self._doc_id)
            except (ServiceError, RuntimeError):
                self._unloadable = True
        return self._shown_doc

    def _state_for(self, obj_id: str) -> str:
        if self._doc_id is None:
            return "ok"
        return self._controller.object_worst_state(self._doc_id, obj_id)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = super().flags(index)
        col = self._columns[index.column()]
        if col in ("state", "id"):
            return base  # gutter is cosmetic; object id is immutable
        if self._read_only or self._stale:
            # While stale, no editor opens: its text could only be refused.
            return base
        if self._document() is None or self._unloadable:
            return base  # nothing on disk to write the edit to
        return base | Qt.ItemFlag.ItemIsEditable

    def setData(
        self,
        index: QModelIndex,
        value,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        if self._doc_id is None:
            return False
        row = self._rows[index.row()]
        col = self._columns[index.column()]
        if col in ("state", "id"):
            return False
        # An editor that was opened and left without a change commits the
        # value it was given; don't rewrite the file (or reload) for that.
        if not isinstance(value, Table):
            value = str(value or "")
        if value == self.data(index, Qt.ItemDataRole.EditRole):
            return True
        if self._read_only:
            return False
        if self._stale:
            self._controller.edit_failed.emit(
                self._doc_id, row.obj_id, col, STALE_EDIT_MESSAGE
            )
            return False

        svc = self._controller.require_service()
        try:
            if isinstance(value, Table):
                # Committed by the inline table editor (description column).
                svc.set_table(self._doc_id, row.obj_id, value)
            elif col == "heading_or_text":
                svc.set_description(self._doc_id, row.obj_id, value)
            else:
                svc.set_attribute(self._doc_id, row.obj_id, col, value)
        except ServiceError as e:
            self._controller.edit_failed.emit(
                self._doc_id, row.obj_id, col, str(e)
            )
            return False

        self._controller.edit_succeeded.emit(self._doc_id, row.obj_id, col)
        # Re-load the doc and rebuild models. This also emits dataChanged
        # effects via the full reset in _rebuild.
        self._controller.reload_document(self._doc_id)
        return True

    # --- population -----------------------------------------------------

    def _rebuild(self, doc_id: str) -> None:
        self.beginResetModel()
        self._doc_id = doc_id
        doc = self._controller.document(doc_id)
        self._shown_doc = doc
        self._unloadable = False
        self._columns, self._labels = _columns_for_doc(doc)
        self._rows = list(_walk(doc))
        self._link_states = self._controller.build_link_states(doc_id)
        self._dirty_ids = self._controller.build_dirty_ids(doc_id)
        self.endResetModel()

    def refresh_link_states(self) -> None:
        """Recompute per-object link states and repaint the state gutter.

        Lighter than `_rebuild`. Used after a commit and when HEAD moved:
        revising a parent document flips its children's links to SUSPECT
        without touching the children's files. Cheap when HEAD hasn't moved,
        as the service caches suspect scans per HEAD.
        """
        if self._doc_id is None or not self._rows or self._unloadable:
            return
        states = self._controller.build_link_states(self._doc_id)
        if states == self._link_states:
            return
        self._link_states = states
        try:
            col = self._columns.index("state")
        except ValueError:
            return
        self.dataChanged.emit(
            self.index(0, col),
            self.index(len(self._rows) - 1, col),
            [OBJ_LINK_STATES_ROLE, OBJ_STATE_ROLE, Qt.ItemDataRole.ToolTipRole],
        )

    def refresh_dirty_states(self) -> None:
        """Recompute per-object dirty state and repaint the id column.

        Lighter than `_rebuild` — no model reset, so scroll position and
        selection survive. Used after a commit, when every object's dirty
        bit may have flipped from True to False.
        """
        if self._doc_id is None or not self._rows:
            return
        self._dirty_ids = self._controller.build_dirty_ids(self._doc_id)
        try:
            id_col = self._columns.index("id")
        except ValueError:
            return
        top = self.index(0, id_col)
        bottom = self.index(len(self._rows) - 1, id_col)
        self.dataChanged.emit(top, bottom, [OBJ_DIRTY_ROLE, Qt.ItemDataRole.DisplayRole])

    # --- helpers for views ---------------------------------------------

    def obj_id_at(self, row: int) -> str | None:
        if 0 <= row < len(self._rows):
            return self._rows[row].obj_id
        return None

    def row_for_obj_id(self, obj_id: str) -> int:
        for i, r in enumerate(self._rows):
            if r.obj_id == obj_id:
                return i
        return -1


def _walk(doc: Document):
    """Pre-order walk of `doc.outline` yielding `_Row`s."""

    def go(nodes):
        for node in nodes:
            obj = doc.objects.get(node.ref_id)
            text = obj.description if obj and obj.description else ""
            yield _Row(obj_id=node.ref_id, heading_or_text=text)
            yield from go(node.children)

    yield from go(doc.outline)
