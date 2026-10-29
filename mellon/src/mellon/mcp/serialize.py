"""Dataclass -> JSON-serializable dict conversions for MCP tool outputs.

Every conversion here is one-way (model -> dict) and produces stable, explicit
keys. No nested unknowns, no tuples (they become lists), no enums (they become
their `.value`).
"""

from __future__ import annotations

from pathlib import Path

from ..coverage import CoverageReport
from ..repo import DocLoadError
from ..schema import (
    Attribute,
    Document,
    Filter,
    FilterRule,
    Link,
    Metadata,
    OutlineNode,
    RequirementObject,
    Table,
    View,
)
from ..search import SearchHit
from ..suspects import LinkStatus
from ..trace import TraceEdge


# Field names accepted by `document_summary_to_dict.include`. Any other
# value is silently dropped (so callers can mix the actual field names with
# wildcards without crashing on typos). `*` and `all` are aliases for "the
# full fat shape" — handy when a caller knows they want everything.
SUMMARY_INCLUDE_FIELDS: frozenset[str] = frozenset(
    {"metadata", "parents", "attributes", "outline", "views", "filters"}
)
SUMMARY_INCLUDE_ALL: frozenset[str] = frozenset({"*", "all"})


def outline_node_to_dict(node: OutlineNode) -> dict:
    return {
        "id": node.ref_id,
        "children": [outline_node_to_dict(c) for c in node.children],
    }


def attribute_to_dict(attr: Attribute) -> dict:
    return {
        "name": attr.name,
        "label": attr.label,
        "kind": attr.kind,
        "required": attr.required,
        "options": list(attr.options),
    }


def view_to_dict(view: View) -> dict:
    return {
        "slug": view.slug,
        "name": view.name,
        "visible": list(view.visible),
    }


def filter_rule_to_dict(rule: FilterRule) -> dict:
    return {
        "kind": rule.kind,
        "attr": rule.attr,
        "op": rule.op,
        "value": rule.value,
    }


def filter_to_dict(filt: Filter) -> dict:
    return {
        "slug": filt.slug,
        "name": filt.name,
        "combinator": filt.combinator,
        "keep_ancestors": filt.keep_ancestors,
        "rules": [filter_rule_to_dict(r) for r in filt.rules],
    }


def metadata_to_dict(meta: Metadata) -> dict:
    return {
        "title": meta.title,
        "author": meta.author,
        "date": meta.date,
    }


def table_to_dict(table: Table) -> dict:
    """Serialize a Table to nested lists/dicts.

    Cells carry `colspan`/`rowspan` only when > 1, mirroring the on-disk shape
    and keeping the common (unmerged) case compact.
    """
    rows: list[dict] = []
    for row in table.rows:
        cells: list[dict] = []
        for cell in row.cells:
            c: dict = {"text": cell.text}
            if cell.colspan != 1:
                c["colspan"] = cell.colspan
            if cell.rowspan != 1:
                c["rowspan"] = cell.rowspan
            cells.append(c)
        rows.append({"header": row.header, "cells": cells})
    return {
        "columns": [{"align": col.align} for col in table.columns],
        "rows": rows,
    }


def object_to_dict(obj: RequirementObject) -> dict:
    return {
        "id": obj.id,
        "attrs": dict(obj.attrs),
        "description": obj.description,
        "figure": obj.figure_path,
        "table": table_to_dict(obj.table) if obj.table is not None else None,
    }


def link_to_dict(link: Link) -> dict:
    return {
        "target_doc": link.target_doc,
        "target_id": link.target_id,
        "type": link.type,
        "ack": link.ack,
    }


def link_status_to_dict(status: LinkStatus) -> dict:
    return {
        "source_id": status.source_id,
        "link": link_to_dict(status.link),
        "state": status.state.value,
        "current_hash": status.current_hash,
        "reason": status.reason,
    }


def trace_edge_to_dict(edge: TraceEdge) -> dict:
    return {
        "link_type": edge.link_type,
        "target_doc": edge.target_doc,
        "target_id": edge.target_id,
        "children": [trace_edge_to_dict(c) for c in edge.children],
    }


def coverage_report_to_dict(
    report: CoverageReport, *, include_lists: bool = False
) -> dict:
    """Coverage report as JSON: header counts plus optional per-object lists.

    The `uncovered` and `covered_objects` lists are only included when
    `include_lists=True`; otherwise the response carries just the totals so
    agents asking "what's the coverage percentage" don't pay for hundreds of
    object dicts they don't need.
    """
    out: dict = {
        "source_doc": report.source_doc,
        "target_doc": report.target_doc,
        "direction": report.direction,
        "link_type": report.link_type,
        "transitive": report.transitive,
        "total": report.total,
        "covered": report.covered,
        "coverage_percent": report.coverage_percent,
    }
    if include_lists:
        out["uncovered"] = [
            {"obj_id": r.source_id, "title": r.title}
            for r in report.rows
            if not r.covered
        ]
        out["covered_objects"] = [
            {"obj_id": r.source_id, "title": r.title, "link_count": r.link_count}
            for r in report.rows
            if r.covered
        ]
    return out


def document_summary_to_dict(
    doc: Document,
    folder: str,
    *,
    current_issue: str = "",
    include: frozenset[str] | None = None,
) -> dict:
    """Manifest-level view: counts always, heavier fields opt-in via `include`.

    The compact default (`include=None`) carries only `doc_id`, `folder`,
    `object_count`, `link_count`, and `current_issue` — enough for an agent
    to decide which docs to drill into. Pass any subset of
    `{"metadata", "parents", "attributes", "outline", "views", "filters"}`
    to add those fields, or `{"*"}` / `{"all"}` for the full fat shape.

    `current_issue` is the doc's tag-tracked issue (resolved by the caller
    via `git_ops.current_issue` or service equivalents). Surfaced as a
    top-level field rather than inside `metadata` because it doesn't live
    in the manifest.
    """
    out: dict = {
        "doc_id": doc.id,
        "folder": folder,
        "current_issue": current_issue,
        "object_count": len(doc.objects),
        "link_count": sum(len(v) for v in doc.links.values()),
    }
    if include is None:
        return out

    if include & SUMMARY_INCLUDE_ALL:
        wanted: frozenset[str] = SUMMARY_INCLUDE_FIELDS
    else:
        wanted = include & SUMMARY_INCLUDE_FIELDS

    if "metadata" in wanted:
        out["metadata"] = metadata_to_dict(doc.metadata)
    if "parents" in wanted:
        out["parents"] = list(doc.parents)
    if "attributes" in wanted:
        out["attributes"] = [attribute_to_dict(a) for a in doc.attributes]
    if "outline" in wanted:
        out["outline"] = [outline_node_to_dict(n) for n in doc.outline]
    if "views" in wanted:
        out["views"] = [view_to_dict(v) for v in doc.views]
    if "filters" in wanted:
        out["filters"] = [filter_to_dict(f) for f in doc.filters]
    return out


def load_error_to_dict(err: DocLoadError, root: Path) -> dict:
    """A document that failed to load; `file` is relative to `root`."""
    return {
        "doc_id": err.doc_id,
        "file": err.file.relative_to(root).as_posix() if err.file else None,
        "line": err.line,
        "message": err.message,
    }


def search_hit_to_dict(hit: SearchHit) -> dict:
    return {
        "doc_id": hit.doc_id,
        "obj_id": hit.obj_id,
        "snippet": hit.snippet,
        "score": hit.score,
        "match_fields": list(hit.match_fields),
    }
