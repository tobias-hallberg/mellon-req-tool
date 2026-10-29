"""Recursive traceability: follow outgoing (up) or incoming (down) links.

Both walks are cycle-safe — once a (doc, id) has been visited on the current
chain, it isn't expanded again. This also collapses diamonds (same target
reached via two paths shows children once), which is the right behavior for
human consumption.
"""

from __future__ import annotations

from dataclasses import dataclass

from .repo import LoadedDoc, incoming_links


@dataclass(frozen=True, slots=True)
class TraceEdge:
    """One step in a trace. For `trace_up` the edge points AT target_doc/id
    (outgoing link); for `trace_down` the edge arrives FROM target_doc/id
    (the source of an incoming link). Meaning is encoded by which function
    produced the edge, not by the dataclass itself."""

    link_type: str
    target_doc: str
    target_id: str
    children: tuple["TraceEdge", ...] = ()


def trace_up(
    docs: dict[str, LoadedDoc], doc_id: str, obj_id: str
) -> tuple[TraceEdge, ...]:
    """Follow outgoing links recursively from (doc_id, obj_id)."""
    return _walk_up(docs, doc_id, obj_id, frozenset())


def trace_down(
    docs: dict[str, LoadedDoc], doc_id: str, obj_id: str
) -> tuple[TraceEdge, ...]:
    """Follow incoming links recursively into (doc_id, obj_id)."""
    return _walk_down(docs, doc_id, obj_id, frozenset())


def _walk_up(
    docs: dict[str, LoadedDoc],
    doc_id: str,
    obj_id: str,
    seen: frozenset[tuple[str, str]],
) -> tuple[TraceEdge, ...]:
    key = (doc_id, obj_id)
    if key in seen:
        return ()
    loaded = docs.get(doc_id)
    if loaded is None:
        return ()
    out: list[TraceEdge] = []
    next_seen = seen | {key}
    for link in loaded.document.links.get(obj_id, ()):
        children = _walk_up(docs, link.target_doc, link.target_id, next_seen)
        out.append(
            TraceEdge(
                link_type=link.type,
                target_doc=link.target_doc,
                target_id=link.target_id,
                children=children,
            )
        )
    return tuple(out)


def _walk_down(
    docs: dict[str, LoadedDoc],
    doc_id: str,
    obj_id: str,
    seen: frozenset[tuple[str, str]],
) -> tuple[TraceEdge, ...]:
    key = (doc_id, obj_id)
    if key in seen:
        return ()
    out: list[TraceEdge] = []
    next_seen = seen | {key}
    for src_doc, src_id, link in incoming_links(docs, doc_id, obj_id):
        children = _walk_down(docs, src_doc, src_id, next_seen)
        out.append(
            TraceEdge(
                link_type=link.type,
                target_doc=src_doc,
                target_id=src_id,
                children=children,
            )
        )
    return tuple(out)
