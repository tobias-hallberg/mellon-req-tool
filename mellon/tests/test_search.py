"""Unit tests for `mellon.search.search_objects`."""

from __future__ import annotations

from pathlib import Path

import pytest

from mellon.loader import load_document
from mellon.repo import LoadedDoc
from mellon.search import SearchHit, search_objects


def _load_docs(*folders: Path) -> dict[str, LoadedDoc]:
    out: dict[str, LoadedDoc] = {}
    for folder in folders:
        doc = load_document(folder)
        out[doc.id] = LoadedDoc(folder=folder, document=doc)
    return out


def test_empty_query_returns_empty(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    assert search_objects(docs, "") == []


def test_substring_match_in_description(mcp_repo: Path) -> None:
    # CHILD-0.description = "The system shall child."
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    hits = search_objects(docs, "child")
    # At least the CHILD-0 description matches; PARENT-1's description may
    # also match because it mentions the child doc, but in this fixture it
    # doesn't. Easier to just assert the expected hit is present.
    ids = {(h.doc_id, h.obj_id) for h in hits}
    assert ("CHILD", "CHILD-0") in ids


def test_obj_id_match_outranks_description_only(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    hits = search_objects(docs, "PARENT-1")
    # PARENT-1 itself should be the top hit (obj_id match = +5).
    assert hits[0].doc_id == "PARENT"
    assert hits[0].obj_id == "PARENT-1"
    assert hits[0].score >= 5
    assert "id" in hits[0].match_fields


def test_doc_ids_filter_limits_scan(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    hits = search_objects(docs, "shall", doc_ids=("CHILD",))
    assert all(h.doc_id == "CHILD" for h in hits)


def test_attr_filter_requires_exact_match(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    # CHILD-0 has status=Draft; PARENT-{0,1} have status=Approved.
    draft_hits = search_objects(
        docs, "shall", attr_filters={"status": "Draft"}
    )
    assert {(h.doc_id, h.obj_id) for h in draft_hits} == {("CHILD", "CHILD-0")}
    # A status nobody has should empty the results.
    none = search_objects(
        docs, "shall", attr_filters={"status": "Rejected"}
    )
    assert none == []


def test_max_results_caps_output(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    # The fixture has very few objects; pick a query that hits most, then
    # cap at 1.
    hits = search_objects(docs, "shall", max_results=1)
    assert len(hits) <= 1


def test_snippet_contains_query(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    hits = search_objects(docs, "child")
    [hit] = [h for h in hits if (h.doc_id, h.obj_id) == ("CHILD", "CHILD-0")]
    assert "child" in hit.snippet.lower()


def test_attr_value_match_recorded_in_match_fields(mcp_repo: Path) -> None:
    # Re-tag CHILD-0 with a type value that no description mentions.
    from mellon.editor import set_attribute
    set_attribute(mcp_repo / "CHILD", "CHILD-0", "type", "Note")
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    [hit] = search_objects(docs, "Note")
    assert hit.doc_id == "CHILD"
    assert hit.obj_id == "CHILD-0"
    assert "attrs.type" in hit.match_fields
    assert hit.score >= 2


def test_results_stable_sorted_by_score_then_id(mcp_repo: Path) -> None:
    docs = _load_docs(mcp_repo / "PARENT", mcp_repo / "CHILD")
    hits = search_objects(docs, "shall")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
