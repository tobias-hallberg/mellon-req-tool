"""Substring search across loaded documents.

Pure in-memory scan: no index, no FTS. The repo is small enough that walking
every object on every call is fine, and indexing would add a maintenance
burden (cache invalidation, write-through on edits) that doesn't fit the
project's "small repo, small overhead" style.

The MCP server's `search_objects` tool is the primary consumer — it lets an
agent find a requirement without having to load whole documents to grep
descriptions client-side.
"""

from __future__ import annotations

from dataclasses import dataclass

from .repo import LoadedDoc


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One match returned by `search_objects`.

    `score` ranks hits across the whole repo; `match_fields` lists which
    fields contributed (e.g. `("description", "attrs.status")`). `snippet`
    is a short excerpt from the description containing the query — empty
    when the match was on an obj id or attribute value only.
    """

    doc_id: str
    obj_id: str
    snippet: str
    score: int
    match_fields: tuple[str, ...]


def _snippet(text: str, query_lower: str, *, width: int = 80) -> str:
    """Return ~`width` chars of `text` centered on the first hit of `query_lower`."""
    if not text:
        return ""
    idx = text.lower().find(query_lower)
    if idx < 0:
        return text.splitlines()[0][:width]
    start = max(0, idx - width // 2)
    end = min(len(text), start + width)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet.replace("\n", " ")


def search_objects(
    docs: dict[str, LoadedDoc],
    query: str,
    *,
    doc_ids: tuple[str, ...] | None = None,
    attr_filters: dict[str, str] | None = None,
    max_results: int = 50,
) -> list[SearchHit]:
    """Find objects whose description, attributes, or id contain `query`.

    Case-insensitive substring match. `doc_ids` limits the scan to a subset
    of documents. `attr_filters` requires exact equality on the given
    attribute names (e.g. `{"status": "Approved"}` keeps only approved
    objects). Results are sorted by descending score, then by `(doc_id,
    obj_id)` for stable order.

    Scoring (additive):
      - `obj_id` contains query → +5
      - each attribute value contains query → +2
      - description contains query → +1
    """
    if not query:
        return []
    q = query.lower()
    selected_doc_ids = (
        set(doc_ids) if doc_ids is not None else set(docs.keys())
    )
    filters = dict(attr_filters or {})

    hits: list[SearchHit] = []
    for doc_id in sorted(selected_doc_ids):
        loaded = docs.get(doc_id)
        if loaded is None:
            continue
        for obj in loaded.document.objects.values():
            # attr_filters is a hard gate, evaluated before the substring scan.
            if filters and not all(
                obj.attrs.get(name, "") == value
                for name, value in filters.items()
            ):
                continue

            score = 0
            match_fields: list[str] = []
            if q in obj.id.lower():
                score += 5
                match_fields.append("id")
            for attr_name, attr_value in obj.attrs.items():
                if attr_value and q in attr_value.lower():
                    score += 2
                    match_fields.append(f"attrs.{attr_name}")
            description = obj.description or ""
            if q in description.lower():
                score += 1
                match_fields.append("description")

            if score == 0:
                continue

            hits.append(
                SearchHit(
                    doc_id=doc_id,
                    obj_id=obj.id,
                    snippet=_snippet(description, q) if description else "",
                    score=score,
                    match_fields=tuple(match_fields),
                )
            )

    hits.sort(key=lambda h: (-h.score, h.doc_id, h.obj_id))
    return hits[:max_results]
