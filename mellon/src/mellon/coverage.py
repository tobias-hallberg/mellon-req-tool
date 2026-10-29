"""Coverage analysis: how many objects in one document are linked from another.

Answers V-model questions like "which SRS requirements have no STS test?" by
checking, for each object in the source document, whether at least one object
in the target document has an outgoing link to it.

Direction mirrors the V-model link convention used in this codebase:
verification/satisfaction links are authored on the *child* document and point
*up* at the parent. So `coverage_report(source_doc=SRS, target_doc=STS)`
asks "is there a STS object linking to this SRS?" by default — that's the
incoming-from-target semantic.

Two semantic directions:
    - "incoming" (default): an object in `source_doc` is covered iff at least
      one object in `target_doc` has an outgoing link pointing at it. This is
      the natural V-model verification question.
    - "outgoing": an object in `source_doc` is covered iff it has at least one
      outgoing link of its own pointing at `target_doc`. Useful when checking
      that every requirement is justified by an upstream document
      (e.g. every SRS satisfies some SyRS).

Two reach modes (orthogonal to direction):
    - direct (default): only one-hop links count.
    - transitive: follow the appropriate trace chain. For incoming-direction
      coverage we walk `trace_down` (incoming chain) on each source object
      and check whether `target_doc` appears anywhere in the tree. For
      outgoing-direction coverage we walk `trace_up`.

Headings (`type="Heading"` per the DOORS convention used in the fixture) are
excluded from the denominator by default — they're structural and not
verifiable. Pass `include_headings=True` to include them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .repo import LoadedDoc
from .trace import TraceEdge, trace_down, trace_up


Direction = Literal["incoming", "outgoing"]


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One source-document object and its coverage state."""

    source_doc: str
    source_id: str
    title: str
    covered: bool
    link_count: int  # direct one-hop links connecting this object to target_doc


@dataclass(frozen=True, slots=True)
class CoverageReport:
    source_doc: str
    target_doc: str
    direction: Direction
    link_type: str | None
    transitive: bool
    rows: tuple[CoverageRow, ...]
    total: int
    covered: int
    coverage_percent: float


def coverage_report(
    docs: dict[str, LoadedDoc],
    source_doc: str,
    target_doc: str,
    *,
    direction: Direction = "incoming",
    link_type: str | None = None,
    include_headings: bool = False,
    transitive: bool = False,
) -> CoverageReport:
    """Build a coverage report from `source_doc` against `target_doc`.

    See module docstring for the semantics of `direction` and `transitive`.
    Raises `KeyError` if either document is not in `docs`.
    """
    if source_doc not in docs:
        raise KeyError(f"source document {source_doc!r} not in repo")
    if target_doc not in docs:
        raise KeyError(f"target document {target_doc!r} not in repo")
    if direction not in ("incoming", "outgoing"):
        raise ValueError(f"unknown direction {direction!r}")

    source = docs[source_doc].document

    # For incoming mode we only need to walk the target doc's link tables once
    # to build a lookup keyed by what each link points at.
    incoming_index: dict[str, int] = {}  # source obj_id -> link count
    if direction == "incoming":
        target = docs[target_doc].document
        for src_id, link_list in target.links.items():
            for link in link_list:
                if link.target_doc != source_doc:
                    continue
                if link_type is not None and link.type != link_type:
                    continue
                incoming_index[link.target_id] = (
                    incoming_index.get(link.target_id, 0) + 1
                )

    rows: list[CoverageRow] = []
    for obj_id in source.iter_outline_ids():
        obj = source.objects.get(obj_id)
        if obj is None:
            continue
        if not include_headings and obj.attrs.get("type") == "Heading":
            continue

        title = ""
        if obj.description:
            title = obj.description.splitlines()[0][:200]

        if direction == "incoming":
            link_count = incoming_index.get(obj_id, 0)
            if transitive:
                covered = _reaches(
                    trace_down(docs, source_doc, obj_id), target_doc, link_type
                )
            else:
                covered = link_count > 0
        else:  # outgoing
            outgoing = source.links.get(obj_id, ())
            link_count = sum(
                1
                for link in outgoing
                if link.target_doc == target_doc
                and (link_type is None or link.type == link_type)
            )
            if transitive:
                covered = _reaches(
                    trace_up(docs, source_doc, obj_id), target_doc, link_type
                )
            else:
                covered = link_count > 0

        rows.append(
            CoverageRow(
                source_doc=source_doc,
                source_id=obj_id,
                title=title,
                covered=covered,
                link_count=link_count,
            )
        )

    total = len(rows)
    covered = sum(1 for r in rows if r.covered)
    percent = (covered / total * 100.0) if total else 0.0
    return CoverageReport(
        source_doc=source_doc,
        target_doc=target_doc,
        direction=direction,
        link_type=link_type,
        transitive=transitive,
        rows=tuple(rows),
        total=total,
        covered=covered,
        coverage_percent=percent,
    )


def _reaches(
    edges: tuple[TraceEdge, ...], target_doc: str, link_type: str | None
) -> bool:
    """True if any edge in the trace tree targets `target_doc` with matching type."""
    for edge in edges:
        if edge.target_doc == target_doc and (
            link_type is None or edge.link_type == link_type
        ):
            return True
        if _reaches(edge.children, target_doc, link_type):
            return True
    return False
