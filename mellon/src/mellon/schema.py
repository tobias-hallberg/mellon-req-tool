"""In-memory model for a requirements document.

Mirrors the on-disk XML schema defined in the project PLAN.md. All types are
frozen dataclasses — a loaded document is immutable; mutations produce new
instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Default labels for "system" attributes that DOORS conventions name
# consistently across documents. A manifest's `<attribute label=...>` always
# wins; this is just the fallback so individual manifests don't have to
# repeat universally-known labels.
SYSTEM_ATTRIBUTE_LABELS: dict[str, str] = {
    "type": "Type",
}


def attribute_display_name(name: str, schema: "Attribute | None") -> str:
    """Display label for an attribute name, applying every fallback.

    Resolution order: manifest `label` → system default → raw attribute name.
    Use this when the schema lookup may return None (e.g. an object carries
    an attribute the manifest no longer declares).
    """
    if schema and schema.label:
        return schema.label
    return SYSTEM_ATTRIBUTE_LABELS.get(name, name)


@dataclass(frozen=True, slots=True)
class Attribute:
    """One entry in the manifest's attribute schema."""

    name: str
    kind: str  # "enum" | "text" | "integer" | "real" | "boolean" | "date"
    required: bool
    options: tuple[str, ...] = ()  # non-empty only when kind == "enum"
    label: str = ""  # optional human-readable display name; falls back to name

    @property
    def display_name(self) -> str:
        return self.label or SYSTEM_ATTRIBUTE_LABELS.get(self.name, self.name)


@dataclass(frozen=True, slots=True)
class OutlineNode:
    """One node in the manifest outline tree."""

    ref_id: str
    children: tuple["OutlineNode", ...] = ()


@dataclass(frozen=True, slots=True)
class RequirementObject:
    """A single requirement / heading object (objects/<id>.xml)."""

    id: str
    attrs: dict[str, str] = field(default_factory=dict)
    description: str = ""
    # Relative path (e.g. "figures/REQ-0001.svg") populated by the loader when
    # `<doc-folder>/figures/<id>.svg` exists; None otherwise. The figure file
    # itself is the source of truth — this field is a derived convenience so
    # consumers don't have to stat the disk per object.
    figure_path: str | None = None
    # Structured tabular content, parsed inline from the object's `<table>`
    # element. None when the object carries no table. By convention such an
    # object's `type` attribute is "Table" and its `description` is the table
    # caption, but rendering/export/editing key off `table is not None`, never
    # the convention — so hand-authored files don't have to set the type.
    table: "Table | None" = None


@dataclass(frozen=True, slots=True)
class TableCell:
    """One cell in a table row.

    `colspan`/`rowspan` are 1 for an ordinary cell; values >1 merge the cell
    across columns/rows (rendered as `\\multicolumn`/`\\multirow` on export).
    Covered grid positions carry no `TableCell` of their own — the spanning
    cell owns them, and `table_geometry_errors` verifies the tiling.
    """

    text: str = ""
    colspan: int = 1
    rowspan: int = 1


@dataclass(frozen=True, slots=True)
class TableColumn:
    """One column declaration: just its horizontal alignment."""

    align: str = "left"  # "left" | "center" | "right"


@dataclass(frozen=True, slots=True)
class TableRow:
    """One table row. `header` rows render bold/shaded and sit above the rule."""

    cells: tuple[TableCell, ...] = ()
    header: bool = False


@dataclass(frozen=True, slots=True)
class Table:
    """Structured tabular content attached to a RequirementObject.

    The grid is `len(columns)` wide; `rows` tile that width exactly once
    accounting for colspan/rowspan (checked by `table_geometry_errors`).
    """

    columns: tuple[TableColumn, ...] = ()
    rows: tuple[TableRow, ...] = ()


TABLE_ALIGNMENTS: frozenset[str] = frozenset({"left", "center", "right"})


def table_geometry_errors(table: "Table") -> list[str]:
    """Return human-readable problems with a table's cell tiling, empty if OK.

    Verifies the rows/cells exactly tile a rectangle `ncols` wide: every
    logical slot is covered exactly once, nothing overflows the width, and no
    rowspan extends past the last row. Walks row-by-row with a per-column
    `carry` buffer counting how many further rows each column is still occupied
    by a rowspan opened above. O(rows x cols).

    Shared by the loader, editor, validator, and GUI editor so the consistency
    rule has a single definition.
    """
    errors: list[str] = []
    ncols = len(table.columns)
    if ncols == 0:
        if table.rows:
            errors.append("table has rows but no <col> declarations")
        return errors

    carry = [0] * ncols  # rows still owed to each column by a rowspan above
    for r, row in enumerate(table.rows):
        col = 0
        cell_iter = iter(row.cells)
        while col < ncols:
            if carry[col] > 0:  # slot filled by a rowspan from a row above
                carry[col] -= 1
                col += 1
                continue
            try:
                cell = next(cell_iter)
            except StopIteration:
                errors.append(
                    f"row {r}: too few cells to fill all {ncols} columns"
                )
                break
            cs = max(cell.colspan, 1)
            rs = max(cell.rowspan, 1)
            if col + cs > ncols:
                errors.append(
                    f"row {r}: cell at column {col} spans {cs} columns, "
                    f"overflowing table width {ncols}"
                )
                cs = ncols - col  # clamp so the walk can finish
            if rs > 1:
                for k in range(col, col + cs):
                    carry[k] = rs - 1
            col += cs
        else:
            # Row consumed every column slot; any remaining cells are extra.
            leftover = sum(1 for _ in cell_iter)
            if leftover:
                errors.append(
                    f"row {r}: {leftover} cell(s) beyond table width {ncols}"
                )

    for c in range(ncols):
        if carry[c] > 0:
            errors.append(
                f"column {c}: a rowspan extends {carry[c]} row(s) past the "
                f"last table row"
            )
    return errors


@dataclass(frozen=True, slots=True)
class Link:
    """One outgoing link from a source object."""

    target_doc: str
    target_id: str
    type: str
    ack: str | None  # short commit hash; None = never reviewed


@dataclass(frozen=True, slots=True)
class LinkDirective:
    """A link-presence directive under a `<then>` block.

    `target_doc` names the required outgoing-link target document. `status`
    is currently always `"required"` — a matching object must have at least
    one outgoing link to an object in that document.
    """

    target_doc: str
    status: str = "required"


@dataclass(frozen=True, slots=True)
class Rule:
    """One per-document rule.

    `conditions` are matched against an object as an AND of equality checks.
    Reserved predicate names:
        - "type"               — matches obj.attrs["type"]
        - "requirement-type"   — matches obj.attrs["RequirementType"]
        - "has-outgoing-link"  — "true" / "false"; matches whether links/<id>.xml has >=1 link
    Any other predicate name falls back to an attribute-equality check:
    `obj.attrs.get(<predicate>, "") == <value>`.

    `directives` map attribute names to one of:
        - "required"   — value must be present and non-empty
        - "optional"   — value is allowed (default)
        - "forbidden"  — value must be absent or empty

    `link_directives` add link-presence constraints — each `LinkDirective`
    requires that a matching object has at least one outgoing link whose
    `target_doc` equals the directive's `target_doc`.

    When several rules match the same object, attribute directives are merged
    in document order (later rules override earlier ones for the same
    attribute); link directives are accumulated.
    """

    conditions: dict[str, str] = field(default_factory=dict)
    directives: dict[str, str] = field(default_factory=dict)
    link_directives: tuple[LinkDirective, ...] = ()


@dataclass(frozen=True, slots=True)
class View:
    """A saved selection of which user attributes are visible in the grid.

    Stored on disk as `<doc-id>/views/<slug>.xml`. System columns
    (state/id/description) are always visible regardless of the view.
    """

    slug: str
    name: str
    visible: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FilterRule:
    """One predicate in a saved Filter.

    `kind` selects the rule family:
        - "attr"          — compare an attribute value: uses `attr` (name) and `op`
        - "has-outgoing"  — outgoing-link presence: `attr` may name a target doc
                            ("" = any document); `op` is "present" or "absent"
        - "has-incoming"  — incoming-link presence: same shape as has-outgoing

    `op` is one of the operator codes accepted by `mellon.filter`. `value` is
    a canonical string ("True"/"False" for booleans, ISO-8601 for dates, the
    raw enum option for enums, "<lo>|<hi>" for the "between" operator).
    """

    kind: str
    attr: str = ""
    op: str = ""
    value: str = ""


@dataclass(frozen=True, slots=True)
class Filter:
    """A saved query that narrows the visible objects in a document.

    Stored on disk as `<doc-id>/filters/<slug>.xml`. Independent of `View` —
    layout (View) and selection (Filter) compose freely in the grid.

    `combinator` is "all" (AND) or "any" (OR) over `rules`. `keep_ancestors`
    keeps every matched object's outline ancestors visible so heading context
    survives the filter (DOORS-style).
    """

    slug: str
    name: str
    combinator: str = "all"
    keep_ancestors: bool = True
    rules: tuple[FilterRule, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.rules


@dataclass(frozen=True, slots=True)
class Metadata:
    """Edit metadata embedded in a manifest's `<metadata>` block.

    `issue` lives outside this dataclass: it's the source-of-truth tag
    `<doc-id>-issue-<n>` in git, looked up via `git_ops.current_issue`.
    """

    title: str = ""
    author: str = ""
    date: str = ""


@dataclass(frozen=True, slots=True)
class Document:
    """A whole document: manifest + objects + outgoing links."""

    id: str
    metadata: Metadata
    parents: tuple[str, ...]
    attributes: tuple[Attribute, ...]
    outline: tuple[OutlineNode, ...]
    objects: dict[str, RequirementObject]
    links: dict[str, tuple[Link, ...]]  # keyed by source object id
    rules: tuple[Rule, ...] = ()
    views: tuple[View, ...] = ()
    filters: tuple[Filter, ...] = ()

    def attribute(self, name: str) -> Attribute | None:
        for a in self.attributes:
            if a.name == name:
                return a
        return None

    def iter_outline_ids(self) -> list[str]:
        """Flat pre-order list of every ref_id in the outline tree."""
        out: list[str] = []

        def walk(nodes: tuple[OutlineNode, ...]) -> None:
            for n in nodes:
                out.append(n.ref_id)
                walk(n.children)

        walk(self.outline)
        return out

    def outline_path(self, ref_id: str) -> tuple[str, ...] | None:
        """Return the chain of ref_ids from the top-level ancestor down to `ref_id`.

        None if `ref_id` is not in the outline.
        """

        def walk(
            nodes: tuple[OutlineNode, ...], trail: tuple[str, ...]
        ) -> tuple[str, ...] | None:
            for n in nodes:
                here = trail + (n.ref_id,)
                if n.ref_id == ref_id:
                    return here
                found = walk(n.children, here)
                if found is not None:
                    return found
            return None

        return walk(self.outline, ())

    def incoming_links(self, target_id: str) -> list[tuple[str, Link]]:
        """Same-document incoming links to `target_id`, as (source_id, link) pairs."""
        out: list[tuple[str, Link]] = []
        for source_id, link_list in self.links.items():
            for link in link_list:
                if link.target_doc == self.id and link.target_id == target_id:
                    out.append((source_id, link))
        return out
