"""Unit + integration tests for the read-only MCP tools.

Tools are invoked directly as Python callables (FastMCP's `@tool` decorator
leaves the function callable). `_State.repo_root` is set by the `mcp_repo`
fixture in conftest.py (or `real_repo` below for the req-test integration).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mellon.mcp import server
from tests.test_repo import _write_merge_conflict


@pytest.fixture
def real_repo(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points at the committed req-test fixture. Skipped if not present."""
    root = (Path(__file__).resolve().parents[2] / "req-test").resolve()
    if not (root / ".git").exists():
        pytest.skip(f"req-test repo not available at {root}")
    monkeypatch.setattr(server._State, "repo_root", root)
    return root


# --- list_documents / show_document ----------------------------------------


def test_list_documents_compact_default_shape(mcp_repo: Path) -> None:
    # Default response is the compact shape: counts + current_issue only.
    # Heavy fields require an explicit `include`.
    result = server.list_documents()
    ids = {d["doc_id"] for d in result}
    assert ids == {"PARENT", "CHILD"}
    child = next(d for d in result if d["doc_id"] == "CHILD")
    assert child["object_count"] == 1
    assert child["link_count"] == 1
    # No heavy fields by default.
    for key in ("metadata", "parents", "attributes", "outline", "views", "filters"):
        assert key not in child


def test_list_documents_with_parents_include(mcp_repo: Path) -> None:
    result = server.list_documents(include=["parents"])
    child = next(d for d in result if d["doc_id"] == "CHILD")
    assert child["parents"] == ["PARENT"]
    # Still no other heavy fields.
    assert "outline" not in child


def test_list_documents_all_returns_fat_shape(mcp_repo: Path) -> None:
    result = server.list_documents(include=["all"])
    child = next(d for d in result if d["doc_id"] == "CHILD")
    assert child["parents"] == ["PARENT"]
    assert {a["name"] for a in child["attributes"]} == {"type", "status"}
    assert child["outline"][0]["id"] == "CHILD-0"


def test_show_document_compact_default_shape(mcp_repo: Path) -> None:
    doc = server.show_document("PARENT")
    assert doc["doc_id"] == "PARENT"
    assert doc["object_count"] == 2
    # No outline, metadata, attributes, views, filters by default.
    for key in ("metadata", "parents", "attributes", "outline", "views", "filters"):
        assert key not in doc


def test_show_document_with_include(mcp_repo: Path) -> None:
    doc = server.show_document(
        "PARENT", include=["metadata", "attributes", "outline"]
    )
    assert doc["metadata"]["title"] == "Parent doc"
    assert {a["name"] for a in doc["attributes"]} == {"type", "status"}
    assert doc["outline"][0]["id"] == "PARENT-0"
    assert doc["outline"][0]["children"][0]["id"] == "PARENT-1"
    # Not requested → absent.
    assert "views" not in doc


def test_show_document_unknown_raises(mcp_repo: Path) -> None:
    with pytest.raises(ValueError, match="No document 'NOPE'"):
        server.show_document("NOPE")


def test_doc_id_cannot_reach_a_document_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mellon.editor import create_document

    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "X").mkdir()
    create_document(tmp_path / "X", "X")  # tmp_path/X/X/X.xml, outside root
    monkeypatch.setattr(server._State, "repo_root", root.resolve())
    monkeypatch.setattr(server._State, "read_only", False)
    # git_history uses _require_doc's folder itself (it opens git from it),
    # so the service's own doc_id guard doesn't cover it.
    with pytest.raises(ValueError, match="No document"):
        server.git_history("../X/X", "X-0")
    with pytest.raises(ValueError, match="No document"):
        server.show_document("../X/X")
    manifest = tmp_path / "X" / "X" / "X.xml"
    before = manifest.read_bytes()
    # A write tool returns it as its failure result (MELLON-MCP-SRS-45).
    result = server.edit_metadata("../X/X", "title", "outside")
    assert result["ok"] is False and "No document" in result["error"], result
    assert manifest.read_bytes() == before


# --- show_object -----------------------------------------------------------


def test_show_object_includes_outline_and_links(mcp_repo: Path) -> None:
    result = server.show_object("CHILD", "CHILD-0")
    assert result["object"]["id"] == "CHILD-0"
    assert result["object"]["attrs"]["status"] == "Draft"
    assert result["outline_path"] == ["CHILD-0"]
    # Tmp repo has no git history, so state falls back to "unknown".
    assert len(result["outgoing"]) == 1
    out = result["outgoing"][0]
    assert out["link"]["target_doc"] == "PARENT"
    assert out["link"]["target_id"] == "PARENT-1"
    assert out["state"] in ("unknown", "suspect", "broken")


def test_show_object_unknown_id_raises(mcp_repo: Path) -> None:
    with pytest.raises(ValueError, match="No object 'CHILD-99'"):
        server.show_object("CHILD", "CHILD-99")


def test_show_object_incoming_is_repo_wide(mcp_repo: Path) -> None:
    # PARENT-1 is the target of CHILD-0's link.
    result = server.show_object("PARENT", "PARENT-1")
    assert len(result["incoming"]) == 1
    inc = result["incoming"][0]
    assert inc["source_doc"] == "CHILD"
    assert inc["source_id"] == "CHILD-0"
    assert inc["link_type"] == "satisfies"


# --- outline ---------------------------------------------------------------


def test_outline_is_preorder_with_depth(mcp_repo: Path) -> None:
    result = server.outline("PARENT")
    assert result == [
        {"id": "PARENT-0", "depth": 0, "heading_or_text": "Parent top heading"},
        {
            "id": "PARENT-1",
            "depth": 1,
            "heading_or_text": "The system shall parent.",
        },
    ]


def _deepen_parent() -> tuple[str, str, str]:
    """PARENT-0 > PARENT-1 > a > b, plus a second top-level object c."""
    a = server.new_object("PARENT", parent_id="PARENT-1", description="A")["new_id"]
    b = server.new_object("PARENT", parent_id=a, description="B")["new_id"]
    c = server.new_object("PARENT", description="C")["new_id"]
    return a, b, c


def test_outline_depth_lists_top_levels_and_counts_the_rest(mcp_repo: Path) -> None:
    a, b, c = _deepen_parent()
    assert server.outline("PARENT", depth=1) == [
        {
            "id": "PARENT-0",
            "depth": 0,
            "heading_or_text": "Parent top heading",
            "descendants_omitted": 3,
        },
        {"id": c, "depth": 0, "heading_or_text": "C"},
    ]
    assert [r["id"] for r in server.outline("PARENT", depth=2)] == [
        "PARENT-0", "PARENT-1", c,
    ]
    assert server.outline("PARENT", depth=2)[1]["descendants_omitted"] == 2
    # Deep enough to reach every row: identical to the unbounded outline.
    full = server.outline("PARENT")
    assert server.outline("PARENT", depth=4) == full
    assert [r["id"] for r in full] == ["PARENT-0", "PARENT-1", a, b, c]
    assert not any("descendants_omitted" in r for r in full)


def test_outline_root_lists_the_subtree_below_it(mcp_repo: Path) -> None:
    a, b, _ = _deepen_parent()
    # The root itself is left out; depths stay document depths.
    assert server.outline("PARENT", root="PARENT-1") == [
        {"id": a, "depth": 2, "heading_or_text": "A"},
        {"id": b, "depth": 3, "heading_or_text": "B"},
    ]
    assert server.outline("PARENT", root="PARENT-1", depth=1) == [
        {"id": a, "depth": 2, "heading_or_text": "A", "descendants_omitted": 1},
    ]
    assert server.outline("PARENT", root=b) == []


def test_outline_rejects_unknown_root_and_depth_below_one(mcp_repo: Path) -> None:
    with pytest.raises(ValueError, match="No object 'PARENT-99' in the outline"):
        server.outline("PARENT", root="PARENT-99")
    with pytest.raises(ValueError, match="depth must be 1 or more"):
        server.outline("PARENT", depth=0)


# --- validate --------------------------------------------------------------


def test_validate_clean_document(mcp_repo: Path) -> None:
    result = server.validate_document("PARENT")
    assert result == {"ok": True, "errors": []}


def test_validate_returns_structured_errors(mcp_repo: Path) -> None:
    # Add a new required attribute the existing CHILD-0 doesn't carry; the
    # validator should surface ATTR_MISSING_REQUIRED with structured fields.
    server.define_attribute("CHILD", "rationale", "text", required=True)
    result = server.validate_document("CHILD")
    assert result["ok"] is False
    assert any(
        e["code"] == "ATTR_MISSING_REQUIRED" and "CHILD-0" in e["message"]
        for e in result["errors"]
    )
    for err in result["errors"]:
        assert set(err.keys()) == {"code", "message"}


def test_validate_repo_clean_returns_empty(mcp_repo: Path) -> None:
    result = server.validate_repo()
    assert result["ok"] is True
    assert result["total_errors"] == 0
    assert result["load_errors"] == []
    doc_ids = {d["doc_id"] for d in result["documents"]}
    assert doc_ids == {"PARENT", "CHILD"}
    for entry in result["documents"]:
        assert entry["ok"] is True
        assert entry["errors"] == []


def test_validate_repo_surfaces_errors_per_doc(mcp_repo: Path) -> None:
    # Break CHILD by adding a required attribute the existing object lacks.
    server.define_attribute("CHILD", "rationale", "text", required=True)
    result = server.validate_repo()
    assert result["ok"] is False
    assert result["total_errors"] >= 1
    child_entry = next(d for d in result["documents"] if d["doc_id"] == "CHILD")
    parent_entry = next(d for d in result["documents"] if d["doc_id"] == "PARENT")
    assert child_entry["ok"] is False
    assert parent_entry["ok"] is True
    # Errors are structured per the PR 1.2 contract.
    for err in child_entry["errors"]:
        assert set(err.keys()) == {"code", "message"}
    assert any(
        e["code"] == "ATTR_MISSING_REQUIRED" for e in child_entry["errors"]
    )


# --- documents that fail to load -------------------------------------------


@pytest.fixture
def conflicted_child(mcp_repo: Path) -> Path:
    """`mcp_repo` with CHILD's only object left mid-merge."""
    _write_merge_conflict(mcp_repo / "CHILD" / "objects" / "CHILD-0.xml")
    return mcp_repo


def _assert_child_load_error(err: dict) -> None:
    assert set(err) == {"doc_id", "file", "line", "message"}
    assert err["doc_id"] == "CHILD"
    assert err["file"] == "CHILD/objects/CHILD-0.xml"
    assert err["line"] == 3
    assert err["message"].endswith("(unresolved merge conflict?)")


def test_list_documents_lists_a_document_that_fails_to_load(
    conflicted_child: Path,
) -> None:
    result = server.list_documents()
    assert [d["doc_id"] for d in result] == ["CHILD", "PARENT"]
    child = result[0]
    assert set(child) == {"doc_id", "folder", "load_error"}
    _assert_child_load_error(child["load_error"])
    assert "load_error" not in result[1]


def test_validate_repo_not_ok_when_a_document_fails_to_load(
    conflicted_child: Path,
) -> None:
    result = server.validate_repo()
    assert result["ok"] is False
    assert [d["doc_id"] for d in result["documents"]] == ["PARENT"]
    (err,) = result["load_errors"]
    _assert_child_load_error(err)


def test_repo_health_reports_load_errors(conflicted_child: Path) -> None:
    result = server.repo_health()
    assert [d["doc_id"] for d in result["documents"]] == ["PARENT"]
    (err,) = result["load_errors"]
    _assert_child_load_error(err)


def test_load_errors_with_an_unresolved_repo_root(
    conflicted_child: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Load-error paths are resolved; they must still come out repo-relative.
    monkeypatch.setattr(server._State, "repo_root", conflicted_child / "CHILD" / "..")
    _assert_child_load_error(server.validate_repo()["load_errors"][0])
    _assert_child_load_error(server.repo_health()["load_errors"][0])


# --- repo_health -----------------------------------------------------------


def test_repo_health_smoke(mcp_repo: Path) -> None:
    result = server.repo_health()
    doc_ids = {d["doc_id"] for d in result["documents"]}
    assert doc_ids == {"PARENT", "CHILD"}
    child = next(d for d in result["documents"] if d["doc_id"] == "CHILD")
    assert child["parents"] == ["PARENT"]
    assert child["object_count"] == 1
    assert child["link_count"] == 1
    assert "validation_error_count" in child
    assert "suspect_count" in child
    assert "broken_count" in child


def test_repo_health_validation_count_reflects_errors(mcp_repo: Path) -> None:
    server.define_attribute("CHILD", "rationale", "text", required=True)
    result = server.repo_health()
    child = next(d for d in result["documents"] if d["doc_id"] == "CHILD")
    assert child["validation_error_count"] >= 1
    assert result["total_validation_errors"] >= 1


def test_repo_health_coverage_includes_parent_relationships(mcp_repo: Path) -> None:
    result = server.repo_health()
    # CHILD declares PARENT as parent → expect outgoing + incoming rows.
    rows = result["coverage"]
    directions = {
        (r["source_doc"], r["target_doc"], r["direction"]) for r in rows
    }
    assert ("CHILD", "PARENT", "outgoing") in directions
    assert ("PARENT", "CHILD", "incoming") in directions
    for row in rows:
        assert set(row.keys()) >= {
            "source_doc", "target_doc", "direction",
            "total", "covered", "coverage_percent",
        }


def test_repo_health_real_repo_three_docs(real_repo: Path) -> None:
    result = server.repo_health()
    doc_ids = {d["doc_id"] for d in result["documents"]}
    assert doc_ids == {"VCU3-SyRS", "VCU3-SRS", "VCU3-STS"}
    # Fixture is engineered with 6 suspects.
    assert result["total_suspect_count"] == 6
    # SRS declares SyRS as parent, STS declares SRS as parent → 4 rows
    # (2 per relationship).
    coverage_pairs = {
        (r["source_doc"], r["target_doc"]) for r in result["coverage"]
    }
    assert ("VCU3-SRS", "VCU3-SyRS") in coverage_pairs
    assert ("VCU3-STS", "VCU3-SRS") in coverage_pairs


# --- incoming_links --------------------------------------------------------


def test_incoming_links_tool(mcp_repo: Path) -> None:
    assert server.incoming_links("PARENT", "PARENT-1") == [
        {"source_doc": "CHILD", "source_id": "CHILD-0", "link_type": "satisfies"}
    ]
    assert server.incoming_links("PARENT", "PARENT-0") == []


# --- coverage_report -------------------------------------------------------


def test_coverage_report_incoming_default(mcp_repo: Path) -> None:
    # PARENT has PARENT-0 (Heading, excluded) and PARENT-1 (Requirement).
    # CHILD-0 has an outgoing link -[satisfies]-> PARENT-1, so PARENT-1 is
    # covered against CHILD.
    result = server.coverage_report("PARENT", "CHILD", include_lists=True)
    assert result["source_doc"] == "PARENT"
    assert result["target_doc"] == "CHILD"
    assert result["direction"] == "incoming"
    assert result["transitive"] is False
    # Heading dropped from denominator -> only PARENT-1 counts.
    assert result["total"] == 1
    assert result["covered"] == 1
    assert result["coverage_percent"] == 100.0
    by_id = {entry["obj_id"]: entry for entry in result["covered_objects"]}
    assert "PARENT-1" in by_id
    assert by_id["PARENT-1"]["link_count"] == 1


def test_coverage_report_compact_default_omits_lists(mcp_repo: Path) -> None:
    # Default response carries totals but not per-object lists.
    result = server.coverage_report("PARENT", "CHILD")
    assert result["total"] == 1
    assert result["covered"] == 1
    assert "uncovered" not in result
    assert "covered_objects" not in result


def test_coverage_report_include_headings(mcp_repo: Path) -> None:
    # With headings included, PARENT-0 enters the denominator and stays
    # uncovered.
    result = server.coverage_report(
        "PARENT", "CHILD", include_headings=True, include_lists=True
    )
    assert result["total"] == 2
    assert result["covered"] == 1
    uncovered_ids = {u["obj_id"] for u in result["uncovered"]}
    assert uncovered_ids == {"PARENT-0"}


def test_coverage_report_outgoing_direction(mcp_repo: Path) -> None:
    # CHILD has one object that links out to PARENT -> 100% outgoing coverage
    # of CHILD against PARENT.
    result = server.coverage_report("CHILD", "PARENT", direction="outgoing")
    assert result["direction"] == "outgoing"
    assert result["total"] == 1
    assert result["covered"] == 1


def test_coverage_report_unknown_doc_raises(mcp_repo: Path) -> None:
    with pytest.raises(ValueError, match="MISSING"):
        server.coverage_report("MISSING", "PARENT")


def test_real_repo_coverage_srs_against_sts(real_repo: Path) -> None:
    # V-model: STS verifies SRS, so coverage(SRS, STS) under default incoming
    # direction tells us how many SRS reqs have an STS test.
    result = server.coverage_report("VCU3-SRS", "VCU3-STS", include_lists=True)
    assert result["source_doc"] == "VCU3-SRS"
    assert result["target_doc"] == "VCU3-STS"
    assert result["direction"] == "incoming"
    assert result["total"] >= 1
    assert 0 <= result["covered"] <= result["total"]
    assert 0.0 <= result["coverage_percent"] <= 100.0
    # The fixture has at least one STS test verifying SRS -> non-zero coverage.
    assert result["covered"] > 0
    for entry in result["uncovered"]:
        assert set(entry.keys()) == {"obj_id", "title"}
    for entry in result["covered_objects"]:
        assert set(entry.keys()) == {"obj_id", "title", "link_count"}
        assert entry["link_count"] >= 1


# --- integration against the committed req-test fixture --------------------


def test_real_repo_lists_three_documents(real_repo: Path) -> None:
    result = server.list_documents()
    ids = {d["doc_id"] for d in result}
    assert ids == {"VCU3-SyRS", "VCU3-SRS", "VCU3-STS"}


def test_real_repo_suspects_match_fixture(real_repo: Path) -> None:
    result = server.suspects()
    # Fixture was engineered for exactly 6 suspects (4 parent-edited + 2
    # never-reviewed) and 0 broken links.
    assert len(result) == 6
    reasons = [s["reason"] for s in result]
    assert sum("never reviewed" in r for r in reasons) == 2
    assert sum("but target is at" in r for r in reasons) == 4
    assert all(s["state"] == "suspect" for s in result)


def test_real_repo_suspects_scoped_to_one_doc(real_repo: Path) -> None:
    # SyRS has no outgoing links of its own -> 0 suspects.
    assert server.suspects("VCU3-SyRS") == []
    # STS's 8 links should all be OK -> 0 non-ok entries.
    assert server.suspects("VCU3-STS") == []


def test_suspects_refuses_to_report_clean_when_a_document_failed_to_load(
    real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_worktree
) -> None:
    """An empty answer has to mean clean: a document that didn't load wasn't
    scanned, and its suspects would otherwise just vanish."""
    copy = tmp_path / "req-test"
    shutil.copytree(real_repo, copy)
    clean_worktree(copy)
    monkeypatch.setattr(server._State, "repo_root", copy)
    (copy / "VCU3-SRS" / "VCU3-SRS.xml").write_text("<<<<<<< HEAD", encoding="utf-8")

    with pytest.raises(ValueError, match="VCU3-SRS"):
        server.suspects()
    # A document that does load can still be asked about by name.
    assert server.suspects("VCU3-STS") == []


def test_real_repo_trace_chains_sts_to_syrs(real_repo: Path) -> None:
    result = server.trace("VCU3-STS", "VCU3-STS-1", direction="up")
    assert "down" not in result
    up = result["up"]
    assert len(up) == 1
    level1 = up[0]
    assert level1["target_doc"] == "VCU3-SRS"
    assert level1["target_id"] == "VCU3-SRS-1"
    assert level1["link_type"] == "verifies"
    level2 = level1["children"][0]
    assert level2["target_doc"] == "VCU3-SyRS"
    assert level2["target_id"] == "VCU3-SyRS-42"
    assert level2["link_type"] == "satisfies"


def test_real_repo_trace_respects_max_depth(real_repo: Path) -> None:
    shallow = server.trace("VCU3-STS", "VCU3-STS-1", direction="up", max_depth=1)
    # With max_depth=1, the first hop is returned but its children are trimmed.
    assert shallow["up"][0]["target_id"] == "VCU3-SRS-1"
    assert shallow["up"][0]["children"] == []


def test_real_repo_show_object_has_live_link_state(real_repo: Path) -> None:
    # VCU3-SRS-15 has a SUSPECT link to VCU3-SyRS-47 in the fixture.
    result = server.show_object("VCU3-SRS", "VCU3-SRS-15")
    out = result["outgoing"]
    assert len(out) == 1
    assert out[0]["state"] == "suspect"
    assert out[0]["link"]["target_id"] == "VCU3-SyRS-47"


# --- search_objects --------------------------------------------------------


def test_search_objects_basic(mcp_repo: Path) -> None:
    hits = server.search_objects(query="child")
    assert any(
        h["doc_id"] == "CHILD" and h["obj_id"] == "CHILD-0" for h in hits
    )
    # Each hit carries the canonical wire shape.
    for h in hits:
        assert set(h.keys()) == {
            "doc_id", "obj_id", "snippet", "score", "match_fields"
        }


def test_search_objects_doc_ids_narrows(mcp_repo: Path) -> None:
    hits = server.search_objects(query="shall", doc_ids=["CHILD"])
    assert all(h["doc_id"] == "CHILD" for h in hits)


def test_search_objects_attr_filter(mcp_repo: Path) -> None:
    hits = server.search_objects(
        query="shall", attr_filters={"status": "Draft"}
    )
    assert all(h["doc_id"] == "CHILD" for h in hits)


def test_search_objects_max_results(mcp_repo: Path) -> None:
    hits = server.search_objects(query="shall", max_results=1)
    assert len(hits) <= 1


def test_search_objects_empty_query_returns_empty(mcp_repo: Path) -> None:
    assert server.search_objects(query="") == []


# --- inspect_object composite ----------------------------------------------


def test_inspect_object_includes_outgoing_and_trace(real_repo: Path) -> None:
    result = server.inspect_object("VCU3-STS", "VCU3-STS-1")
    assert result["object"]["id"] == "VCU3-STS-1"
    assert isinstance(result["outgoing"], list)
    assert isinstance(result["incoming"], list)
    # Default include_trace=True walks two levels: STS-1 -> SRS-1 -> SyRS-42.
    assert "trace_up" in result
    assert "trace_down" in result
    up = result["trace_up"]
    assert up[0]["target_id"] == "VCU3-SRS-1"
    assert up[0]["children"][0]["target_id"] == "VCU3-SyRS-42"


def test_inspect_object_trace_depth_bounded_by_param(real_repo: Path) -> None:
    shallow = server.inspect_object(
        "VCU3-STS", "VCU3-STS-1", trace_depth=1
    )
    # With trace_depth=1, the first hop is returned but its children are trimmed.
    assert shallow["trace_up"][0]["target_id"] == "VCU3-SRS-1"
    assert shallow["trace_up"][0]["children"] == []


def test_inspect_object_include_history_false_omits_field(
    real_repo: Path,
) -> None:
    result = server.inspect_object(
        "VCU3-STS", "VCU3-STS-1", include_history=False
    )
    assert "history" not in result


def test_inspect_object_include_history_returns_log(real_repo: Path) -> None:
    result = server.inspect_object(
        "VCU3-STS", "VCU3-STS-1", include_history=True, history_max=3
    )
    assert isinstance(result["history"], list)
    # The fixture has committed VCU3-STS-1 at least once.
    assert len(result["history"]) >= 1
    # Each entry has the standard log fields.
    for entry in result["history"]:
        assert "short_hash" in entry
        assert "message" in entry


def test_inspect_object_include_trace_false_omits_field(real_repo: Path) -> None:
    result = server.inspect_object(
        "VCU3-STS", "VCU3-STS-1", include_trace=False, include_history=False
    )
    assert "trace_up" not in result
    assert "trace_down" not in result


def test_inspect_object_unknown_obj_raises(mcp_repo: Path) -> None:
    with pytest.raises(ValueError, match="No object 'NOPE'"):
        server.inspect_object("CHILD", "NOPE")


def test_show_object_table_is_none_without_table(mcp_repo: Path) -> None:
    assert server.show_object("CHILD", "CHILD-0")["object"]["table"] is None


def test_show_object_includes_table(mcp_repo: Path) -> None:
    server.set_table(
        "CHILD",
        "CHILD-0",
        {
            "columns": [{"align": "left"}, {"align": "right"}],
            "rows": [
                {"header": True, "cells": ["Signal", "Latency"]},
                {"cells": [{"text": "wide", "colspan": 2}]},
            ],
        },
    )
    table = server.show_object("CHILD", "CHILD-0")["object"]["table"]
    assert table is not None
    assert [c["align"] for c in table["columns"]] == ["left", "right"]
    assert table["rows"][0]["header"] is True
    assert table["rows"][1]["cells"][0]["colspan"] == 2


# --- written blind from MELLON-CHECK-SRS ---
# Imports for this block (the helpers below are prefixed _blind_).
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from mellon.loader import load_document
from mellon.validator import validate
from tests.conftest import _ensure_req_test_fixture


_BLIND_REQ_TEST = Path(__file__).resolve().parents[2] / "req-test"


_BLIND_DEFAULT_ATTRS = (
    ("type", "enum", True, ("Heading", "Requirement", "Information")),
    ("status", "enum", False, ("Draft", "Approved")),
    ("rationale", "text", False, ()),
)


def _blind_manifest(doc_id, *, attrs=_BLIND_DEFAULT_ATTRS, outline=(), parents=(),
                    date="2026-04-21"):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<document id="{doc_id}">',
        "  <metadata>",
        f"    <title>{doc_id}</title>",
        "    <author>tester</author>",
    ]
    if date is not None:
        lines.append(f"    <date>{escape(date)}</date>")
    lines.append("  </metadata>")
    if parents:
        lines.append("  <parents>")
        lines += [f'    <parent doc="{p}"/>' for p in parents]
        lines.append("  </parents>")
    else:
        lines.append("  <parents/>")
    lines.append("  <attributes>")
    for name, kind, required, options in attrs:
        req = "true" if required else "false"
        if options:
            lines.append(f'    <attribute name="{name}" kind="{kind}" required="{req}">')
            lines += [f"      <option>{escape(o)}</option>" for o in options]
            lines.append("    </attribute>")
        else:
            lines.append(f'    <attribute name="{name}" kind="{kind}" required="{req}"/>')
    lines.append("  </attributes>")
    lines.append("  <outline>")
    lines += [f'    <ref id="{ref}"/>' for ref in outline]
    lines.append("  </outline>")
    lines.append("</document>")
    return "\n".join(lines) + "\n"


def _blind_object(obj_id, attrs, description="text"):
    """An object file. An attribute value of None is written as an empty element <attr/>."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<object id="{obj_id}">']
    for name, value in attrs.items():
        if value is None:
            lines.append(f'  <attr name="{name}"/>')
        else:
            lines.append(f'  <attr name="{name}">{escape(value)}</attr>')
    lines.append(f"  <description>{escape(description)}</description>")
    lines.append("</object>")
    return "\n".join(lines) + "\n"


def _blind_links(src, links):
    """links: (target_doc, target_id, link_type, ack-or-None) tuples."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<links source="{src}">']
    for doc, target, link_type, ack in links:
        ack_part = f' ack="{ack}"' if ack else ""
        lines.append(
            f'  <link target-doc="{doc}" target-id="{target}" type="{link_type}"{ack_part}/>'
        )
    lines.append("</links>")
    return "\n".join(lines) + "\n"


def _blind_rules(rules):
    """rules: (when: dict, attr directives: dict name -> status, link target docs) tuples."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<rules>"]
    for when, attr_directives, link_docs in rules:
        lines.append("  <rule>")
        conds = " ".join(f"{k}={quoteattr(v)}" for k, v in when.items())
        lines.append(f"    <when {conds}/>" if conds else "    <when/>")
        lines.append("    <then>")
        for name, status in attr_directives.items():
            lines.append(f'      <attr name="{name}" status="{status}"/>')
        for doc in link_docs:
            lines.append(f'      <link target-doc="{doc}" status="required"/>')
        lines.append("    </then>")
        lines.append("  </rule>")
    lines.append("</rules>")
    return "\n".join(lines) + "\n"


def _blind_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _blind_write_doc(root, doc_id, objects, *, attrs=_BLIND_DEFAULT_ATTRS, outline=None,
                     parents=(), links=None, rules=None, date="2026-04-21",
                     descriptions=None) -> Path:
    """Write a whole document folder: manifest, objects, links and an optional rules file."""
    folder = Path(root) / doc_id
    (folder / "objects").mkdir(parents=True, exist_ok=True)
    if outline is None:
        outline = list(objects)
    _blind_write(folder / f"{doc_id}.xml",
                 _blind_manifest(doc_id, attrs=attrs, outline=outline, parents=parents,
                                 date=date))
    descriptions = descriptions or {}
    for oid, obj_attrs in objects.items():
        _blind_write(folder / "objects" / f"{oid}.xml",
                     _blind_object(oid, obj_attrs, descriptions.get(oid, f"text of {oid}")))
    for src, src_links in (links or {}).items():
        _blind_write(folder / "links" / f"{src}.xml", _blind_links(src, src_links))
    if rules is not None:
        _blind_write(folder / f"{doc_id}.rules.xml", _blind_rules(rules))
    return folder


def _blind_git_env(root: Path) -> dict[str, str]:
    cfg = Path(root).parent / f".{Path(root).name}-blind.gitconfig"
    if not cfg.exists():
        cfg.write_text(
            "[user]\n\tname = Blind Tester\n\temail = blind@example.invalid\n"
            "[core]\n\tautocrlf = false\n[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")
    return env


def _blind_git(root: Path, *args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd or root, env=_blind_git_env(root),
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.rstrip()


def _blind_commit(root: Path, message: str) -> str:
    _blind_git(root, "add", "-A")
    _blind_git(root, "commit", "-q", "-m", message)
    return _blind_git(root, "rev-parse", "HEAD")


def _blind_snapshot(root: Path, *, include_git: bool) -> dict[str, tuple[str, int]]:
    """Each file's sha256 and modified time (ns), keyed by its path relative to `root`."""
    out: dict[str, tuple[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_git and Path(dirpath) == Path(root) and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            p = Path(dirpath, name)
            out[p.relative_to(root).as_posix()] = (
                hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def _blind_assert_unchanged(before, after, what: str) -> None:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), (
        f"{what} changed files: added={added} removed={removed} changed={changed}")


def _blind_req_test_copy(dest: Path, clean_worktree) -> Path:
    _ensure_req_test_fixture()
    shutil.copytree(_BLIND_REQ_TEST, dest, dirs_exist_ok=True)
    clean_worktree(dest)
    return dest


def _blind_dirty_req_test(root: Path) -> None:
    """Uncommitted change in VCU3-SRS: an edited description, an object deleted with its
    outline entry, an untracked file and an ignored file in the document folder."""
    srs = root / "VCU3-SRS"
    obj = srs / "objects" / "VCU3-SRS-1.xml"
    text = obj.read_text(encoding="utf-8")
    new = re.sub(r"<description>(.*?)</description>",
                 r"<description>\1 Edited, not committed.</description>", text, flags=re.S)
    assert new != text
    _blind_write(obj, new)
    (srs / "objects" / "VCU3-SRS-33.xml").unlink()
    manifest = srs / "VCU3-SRS.xml"
    mtext = manifest.read_text(encoding="utf-8")
    assert '<ref id="VCU3-SRS-33"/>' in mtext
    _blind_write(manifest, mtext.replace('      <ref id="VCU3-SRS-33"/>\n', ""))
    _blind_write(srs / "notes.txt", "untracked note\n")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n*.ignored\n")
    _blind_write(srs / "scratch.ignored", "ignored file\n")


def _blind_exe(name: str) -> str:
    exe = shutil.which(name, path=str(Path(sys.executable).parent))
    assert exe, f"{name} is not installed next to {sys.executable}"
    return exe


@pytest.fixture
def blind_mcp_root(mcp_repo: Path) -> Path:
    """The in-process MCP server's repository root, emptied for the test to fill."""
    for child in list(mcp_repo.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return mcp_repo


def _blind_server():
    from mellon.mcp import server

    return server


def _blind_clean_pair(root: Path) -> None:
    _blind_write_doc(root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement", "status": "Draft"},
    })
    _blind_write_doc(root, "CHILD", {"CHILD-0": {"type": "Requirement", "status": "Draft"}},
                     parents=("PARENT",),
                     links={"CHILD-0": [("PARENT", "PARENT-1", "satisfies", None)]})


def _blind_three_error_repo(root: Path) -> None:
    """PARENT with one ATTR_UNKNOWN; CHILD with one ATTR_BAD_ENUM and one
    OUTLINE_MISSING_OBJECT."""
    _blind_write_doc(root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement", "status": "Draft", "colour": "red"},
    })
    _blind_write_doc(root, "CHILD", {
        "CHILD-0": {"type": "Requirement", "status": "Bogus"},
        "CHILD-1": {"type": "Requirement", "status": "Draft"},
    }, outline=["CHILD-0", "CHILD-1", "CHILD-7"], parents=("PARENT",))


def _blind_per_doc(result: dict, key: str) -> dict:
    return {d["doc_id"]: d[key] for d in result["documents"]}


def _blind_inject_validate_fault(monkeypatch, doc_id: str) -> list[str]:
    """Make validation of `doc_id` stop with an exception, wherever validate is referenced.
    Returns the list the fault appends to each time it fires."""
    import mellon.validator as validator_module

    original = validator_module.validate
    fired: list[str] = []

    def faulty(doc, *args, **kwargs):
        if getattr(doc, "id", None) == doc_id:
            fired.append(doc_id)
            raise RuntimeError("injected fault: validation stopped partway")
        return original(doc, *args, **kwargs)

    for mod in list(sys.modules.values()):
        if mod is None or not getattr(mod, "__name__", "").startswith("mellon"):
            continue
        for attr, value in list(vars(mod).items()):
            if value is original:
                monkeypatch.setattr(mod, attr, faulty)
    return fired


def _blind_decode_tool_result(res) -> dict:
    assert not res.isError, f"tool error over stdio: {res.content}"
    text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    return json.loads(text)


def _blind_mcp_stdio(root: Path, calls, *, read_only=False, after_each=None,
                     errlog_path: Path | None = None) -> list[dict]:
    """Run one stdio session against `mellon-mcp --repo-root root` and call each tool."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = ["--repo-root", str(root)] + (["--read-only"] if read_only else [])
    results: list[dict] = []
    errlog_path = errlog_path or Path(root).parent / f"{Path(root).name}-mcp-stderr.log"

    async def run():
        params = StdioServerParameters(command=_blind_exe("mellon-mcp"), args=args)
        with errlog_path.open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for name, arguments in calls:
                        res = await session.call_tool(name, arguments)
                        results.append(_blind_decode_tool_result(res))
                        if after_each is not None:
                            after_each(name)

    anyio.run(run)
    return results


def _blind_parent_doc(root, doc_id):
    _blind_write_doc(root, doc_id, {
        f"{doc_id}-0": {"type": "Heading"},
        f"{doc_id}-1": {"type": "Requirement", "status": "Draft"},
    })


def test_check_srs_9_mcp_validate_tools_change_no_working_copy_file(blind_mcp_root,
                                                                   clean_worktree):
    """MELLON-CHECK-SRS-9: "While Mellon validates a document, Mellon shall not change a file
    in the working copy." Condition: the full (not read-only) server, in process, on a
    working copy with an uncommitted change: validate_document, validate_repo, repo_health."""
    server = _blind_server()
    root = _blind_req_test_copy(blind_mcp_root, clean_worktree)
    _blind_dirty_req_test(root)
    for name, call in [("validate_document", lambda: server.validate_document("VCU3-SRS")),
                       ("validate_repo", server.validate_repo),
                       ("repo_health", server.repo_health)]:
        before = _blind_snapshot(root, include_git=False)
        call()
        _blind_assert_unchanged(before, _blind_snapshot(root, include_git=False), name)


def test_check_srs_9_mcp_stdio_validate_tools_change_no_working_copy_file(tmp_path,
                                                                         clean_worktree):
    """MELLON-CHECK-SRS-9: "While Mellon validates a document, Mellon shall not change a file
    in the working copy." Condition: mellon-mcp started without --read-only, over stdio, on a
    working copy with an uncommitted change: validate_document, validate_repo, repo_health."""
    root = _blind_req_test_copy(tmp_path / "rt", clean_worktree)
    _blind_dirty_req_test(root)
    state = {"before": _blind_snapshot(root, include_git=False)}
    failures: list[str] = []

    def after_each(name):
        after = _blind_snapshot(root, include_git=False)
        try:
            _blind_assert_unchanged(state["before"], after, name)
        except AssertionError as exc:
            failures.append(str(exc))
        state["before"] = after

    _blind_mcp_stdio(root, [("validate_document", {"doc_id": "VCU3-SRS"}),
                            ("validate_repo", {}), ("repo_health", {})],
                     after_each=after_each)
    assert not failures, failures


def test_check_srs_32_suspect_links_are_not_validation_errors(blind_mcp_root, clean_worktree):
    """MELLON-CHECK-SRS-32: "Mellon shall not report a suspect link as a validation error."
    Condition: req-test's VCU3-SRS has 6 suspect links and no other problem;
    validate_document and validate_repo."""
    server = _blind_server()
    _blind_req_test_copy(blind_mcp_root, clean_worktree)
    assert server.repo_health()["total_suspect_count"] == 6, "precondition: 6 suspect links"
    doc = server.validate_document("VCU3-SRS")
    assert doc["ok"] is True and doc["errors"] == [], doc
    repo = server.validate_repo()
    assert repo["ok"] is True and repo["total_errors"] == 0, repo
    assert all(d["errors"] == [] for d in repo["documents"]), repo


def test_check_srs_86_validate_document_returns_each_error_with_code_and_message(
        blind_mcp_root):
    """MELLON-CHECK-SRS-86: "The MCP server's validate_document tool shall return each
    validation error of the given document with its code and a message." Condition: CHILD
    with one object missing a required attribute and one with a value that isn't an
    option."""
    server = _blind_server()
    attrs = (("type", "enum", True, ("Heading", "Requirement")),
             ("status", "enum", True, ("Draft", "Approved")))
    _blind_parent_doc(blind_mcp_root, "PARENT")
    _blind_write_doc(blind_mcp_root, "CHILD", {
        "CHILD-0": {"type": "Requirement"},
        "CHILD-1": {"type": "Requirement", "status": "Bogus"},
    }, attrs=attrs, parents=("PARENT",))
    res = server.validate_document("CHILD")
    errors = res["errors"]
    assert sorted(e["code"] for e in errors) == ["ATTR_BAD_ENUM", "ATTR_MISSING_REQUIRED"], res
    assert all(isinstance(e.get("message"), str) and e["message"].strip() for e in errors), res


def test_check_srs_87_validate_document_not_ok_for_load_error(blind_mcp_root):
    """MELLON-CHECK-SRS-87: "The MCP server's validate_document tool shall return ok as true
    if and only if the given document loads, its validation finishes and the document has no
    validation error." Condition: CHILD's object file holds merge conflict markers; per
    MELLON-CHECK-SRS-2 the load error is reported rather than an empty error list."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    _blind_write(blind_mcp_root / "CHILD" / "objects" / "CHILD-0.xml", (
        '<?xml version="1.0" encoding="UTF-8"?>\n<object id="CHILD-0">\n'
        '<<<<<<< HEAD\n  <attr name="type">Requirement</attr>\n=======\n'
        '  <attr name="type">Heading</attr>\n>>>>>>> other\n'
        '  <description>text</description>\n</object>\n'))
    try:
        res = server.validate_document("CHILD")
    except Exception as exc:  # an error answer isn't ok true; it must name the load error
        ok, text = None, str(exc)
    else:
        ok, text = res.get("ok"), json.dumps(res)
    assert ok is not True, text
    assert "CHILD-0.xml" in text, f"the load error (its file) isn't reported: {text}"


def test_check_srs_87_validate_document_not_ok_when_validation_stops(blind_mcp_root,
                                                                     monkeypatch):
    """MELLON-CHECK-SRS-87: "... ok as true if and only if the given document loads, its
    validation finishes and the document has no validation error." Condition: PARENT is clean
    (ok true), then its validation is made to stop partway by an injected fault."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    assert server.validate_document("PARENT")["ok"] is True, "precondition: PARENT is clean"
    fired = _blind_inject_validate_fault(monkeypatch, "PARENT")
    try:
        res = server.validate_document("PARENT")
    except Exception:
        res = None
    assert fired, "test setup: the injected fault was never reached"
    assert res is None or res.get("ok") is not True, res


def test_check_srs_89_validate_repo_returns_each_error_under_its_document(blind_mcp_root):
    """MELLON-CHECK-SRS-89: "The MCP server's validate_repo tool shall return each validation
    error of each document that loads, with the document ID, the error's code and a
    message." Condition: PARENT with ATTR_UNKNOWN; CHILD with ATTR_BAD_ENUM and
    OUTLINE_MISSING_OBJECT."""
    server = _blind_server()
    _blind_three_error_repo(blind_mcp_root)
    res = server.validate_repo()
    per_doc = {d["doc_id"]: sorted(e["code"] for e in d["errors"]) for d in res["documents"]}
    assert per_doc == {"PARENT": ["ATTR_UNKNOWN"],
                       "CHILD": ["ATTR_BAD_ENUM", "OUTLINE_MISSING_OBJECT"]}, res
    assert all(isinstance(e.get("message"), str) and e["message"].strip()
               for d in res["documents"] for e in d["errors"]), res


def test_check_srs_91_validate_repo_not_ok_when_a_validation_stops(blind_mcp_root,
                                                                   monkeypatch):
    """MELLON-CHECK-SRS-91: "The MCP server's validate_repo tool shall return ok as true if
    and only if each document loads, each document's validation finishes and no document has
    a validation error." Condition: a clean repository (ok true), then CHILD's validation is
    made to stop by an injected fault."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    assert server.validate_repo()["ok"] is True, "precondition: the repository is clean"
    fired = _blind_inject_validate_fault(monkeypatch, "CHILD")
    try:
        res = server.validate_repo()
    except Exception:
        res = None
    assert fired, "test setup: the injected fault was never reached"
    assert res is None or res.get("ok") is not True, res


def test_check_srs_94_validate_repo_total_errors_is_exact(blind_mcp_root):
    """MELLON-CHECK-SRS-94: "The MCP server's validate_repo tool shall return the number of
    validation errors." Condition: three validation errors over two documents."""
    server = _blind_server()
    _blind_three_error_repo(blind_mcp_root)
    res = server.validate_repo()
    listed = sum(len(d["errors"]) for d in res["documents"])
    assert res["total_errors"] == 3 and listed == 3, res


def test_check_srs_96_repo_health_validation_count_per_document(blind_mcp_root):
    """MELLON-CHECK-SRS-96: "The MCP server's repo_health tool shall return for each document
    that loads the number of its validation errors." Condition: PARENT 1 error, CHILD 2."""
    server = _blind_server()
    _blind_three_error_repo(blind_mcp_root)
    counts = _blind_per_doc(server.repo_health(), "validation_error_count")
    assert counts == {"PARENT": 1, "CHILD": 2}


def test_check_srs_96_repo_health_validation_count_zero_when_clean(blind_mcp_root):
    """MELLON-CHECK-SRS-96: "... repo_health tool shall return for each document that loads
    the number of its validation errors." Condition: no validation error anywhere."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    counts = _blind_per_doc(server.repo_health(), "validation_error_count")
    assert counts == {"PARENT": 0, "CHILD": 0}


def test_check_srs_96_repo_health_counts_same_document_broken_link_once(blind_mcp_root):
    """MELLON-CHECK-SRS-96: "... the number of its validation errors." (rationale: "A broken
    link is among them, as LINK_TARGET_MISSING"). Condition: CHILD-0 links to CHILD-9, which
    doesn't exist; nothing else is wrong."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    _blind_write(blind_mcp_root / "CHILD" / "links" / "CHILD-0.xml",
                 _blind_links("CHILD-0", [("CHILD", "CHILD-9", "refines", None)]))
    counts = _blind_per_doc(server.repo_health(), "validation_error_count")
    assert counts == {"PARENT": 0, "CHILD": 1}


def test_check_srs_97_repo_health_suspect_count_is_own_outgoing_links(blind_mcp_root,
                                                                      clean_worktree):
    """MELLON-CHECK-SRS-97: "The MCP server's repo_health tool shall return for each document
    that loads the number of its objects' outgoing links that are suspect links." Condition:
    req-test's 6 suspect links all go from VCU3-SRS to VCU3-SyRS; then 2 are acknowledged by
    link key and committed."""
    server = _blind_server()
    _blind_req_test_copy(blind_mcp_root, clean_worktree)
    counts = _blind_per_doc(server.repo_health(), "suspect_count")
    assert counts == {"VCU3-SRS": 6, "VCU3-SyRS": 0, "VCU3-STS": 0}
    server.ack_suspects("VCU3-SRS", link_keys=[
        {"source_id": "VCU3-SRS-15", "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-47"},
        {"source_id": "VCU3-SRS-17", "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-61"},
    ])
    server.commit_changes("Acknowledge two suspect links")
    counts = _blind_per_doc(server.repo_health(), "suspect_count")
    assert counts == {"VCU3-SRS": 4, "VCU3-SyRS": 0, "VCU3-STS": 0}


def test_check_srs_100_repo_health_total_validation_errors(blind_mcp_root):
    """MELLON-CHECK-SRS-100: "The MCP server's repo_health tool shall return the total number
    of validation errors in the documents that load." Condition: three errors over two
    documents."""
    server = _blind_server()
    _blind_three_error_repo(blind_mcp_root)
    assert server.repo_health()["total_validation_errors"] == 3


def test_check_srs_100_repo_health_total_validation_errors_zero_when_clean(blind_mcp_root):
    """MELLON-CHECK-SRS-100: "... the total number of validation errors in the documents that
    load." Condition: no validation error."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    assert server.repo_health()["total_validation_errors"] == 0


def test_check_srs_104_repo_health_link_count_is_outgoing_links(blind_mcp_root):
    """MELLON-CHECK-SRS-104: "The MCP server's repo_health tool shall return for each
    document that loads its number of outgoing links." Condition: CHILD-0 links to PARENT-1
    (PARENT has one incoming link and no outgoing link); then a second outgoing link from
    CHILD-0 to PARENT-0."""
    server = _blind_server()
    _blind_clean_pair(blind_mcp_root)
    assert _blind_per_doc(server.repo_health(), "link_count") == {"PARENT": 0, "CHILD": 1}
    _blind_write(blind_mcp_root / "CHILD" / "links" / "CHILD-0.xml", _blind_links(
        "CHILD-0", [("PARENT", "PARENT-1", "satisfies", None),
                    ("PARENT", "PARENT-0", "refines", None)]))
    assert _blind_per_doc(server.repo_health(), "link_count") == {"PARENT": 0, "CHILD": 2}


def test_check_srs_105_repo_health_current_issue_or_empty(blind_mcp_root):
    """MELLON-CHECK-SRS-105: "The MCP server's repo_health tool shall return for each
    document that loads its current issue, or an empty value when the document has none."
    Condition: PARENT-issue-1 and -2 on ancestors of the latest commit, PARENT-issue-3 on a
    commit of another branch only; CHILD has no issue."""
    server = _blind_server()
    root = blind_mcp_root
    _blind_git(root, "init", "-q")
    _blind_clean_pair(root)
    _blind_commit(root, "c1")
    _blind_git(root, "tag", "PARENT-issue-1")
    p1 = root / "PARENT" / "objects" / "PARENT-1.xml"
    _blind_write(p1, _blind_object("PARENT-1", {"type": "Requirement"}, "second"))
    _blind_commit(root, "c2")
    _blind_git(root, "tag", "PARENT-issue-2")
    _blind_git(root, "checkout", "-q", "-b", "side")
    _blind_write(p1, _blind_object("PARENT-1", {"type": "Requirement"}, "side"))
    _blind_commit(root, "c3 on side")
    _blind_git(root, "tag", "PARENT-issue-3")
    _blind_git(root, "checkout", "-q", "main")
    _blind_write(root / "CHILD" / "objects" / "CHILD-0.xml",
                 _blind_object("CHILD-0", {"type": "Requirement"}, "latest"))
    _blind_commit(root, "c4")
    issues = _blind_per_doc(server.repo_health(), "current_issue")
    assert issues == {"PARENT": "2", "CHILD": ""}


def _blind_coverage_row(health: dict, source_doc: str, target_doc: str) -> dict:
    rows = [r for r in health["coverage"]
            if r["source_doc"] == source_doc and r["target_doc"] == target_doc]
    assert len(rows) == 1, f"expected one coverage row {source_doc}->{target_doc}: {health}"
    return rows[0]


def test_check_srs_107_repo_health_document_objects_linking_to_parent(blind_mcp_root):
    """MELLON-CHECK-SRS-107: "The MCP server's repo_health tool shall return for each pair of
    document and parent document how many of the document's objects, headings left out,
    link directly to the parent document, out of how many." Condition: CHILD-0 links to
    PARENT-1, CHILD-1 has no link, the heading CHILD-2 links to PARENT-2: 1 out of 2."""
    server = _blind_server()
    _blind_write_doc(blind_mcp_root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement"},
        "PARENT-2": {"type": "Requirement"},
    })
    _blind_write_doc(blind_mcp_root, "CHILD", {
        "CHILD-0": {"type": "Requirement"},
        "CHILD-1": {"type": "Requirement"},
        "CHILD-2": {"type": "Heading"},
    }, parents=("PARENT",), links={
        "CHILD-0": [("PARENT", "PARENT-1", "satisfies", None)],
        "CHILD-2": [("PARENT", "PARENT-2", "satisfies", None)],
    })
    row = _blind_coverage_row(server.repo_health(), "CHILD", "PARENT")
    assert (row["covered"], row["total"]) == (1, 2), row


def test_check_srs_108_repo_health_parent_objects_linked_by_document(blind_mcp_root):
    """MELLON-CHECK-SRS-108: "The MCP server's repo_health tool shall return for each pair of
    document and parent document how many of the parent document's objects, headings left
    out, the document links to directly, out of how many." Condition: CHILD links to PARENT-1
    and to the heading PARENT-0; PARENT-2 has no incoming link: 1 out of 2. Then GRAND
    becomes PARENT's parent and PARENT-1 links to it: CHILD's numbers stay (direct only)."""
    server = _blind_server()
    _blind_write_doc(blind_mcp_root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement"},
        "PARENT-2": {"type": "Requirement"},
    })
    _blind_write_doc(blind_mcp_root, "CHILD", {
        "CHILD-0": {"type": "Requirement"},
        "CHILD-1": {"type": "Requirement"},
    }, parents=("PARENT",), links={
        "CHILD-0": [("PARENT", "PARENT-1", "satisfies", None)],
        "CHILD-1": [("PARENT", "PARENT-0", "refines", None)],
    })
    health = server.repo_health()
    row = _blind_coverage_row(health, "PARENT", "CHILD")
    assert (row["covered"], row["total"]) == (1, 2), row
    child_row = _blind_coverage_row(health, "CHILD", "PARENT")

    _blind_parent_doc(blind_mcp_root, "GRAND")
    _blind_write_doc(blind_mcp_root, "PARENT", {
        "PARENT-0": {"type": "Heading"},
        "PARENT-1": {"type": "Requirement"},
        "PARENT-2": {"type": "Requirement"},
    }, parents=("GRAND",), links={"PARENT-1": [("GRAND", "GRAND-1", "satisfies", None)]})
    health = server.repo_health()
    row = _blind_coverage_row(health, "PARENT", "CHILD")
    assert (row["covered"], row["total"]) == (1, 2), row
    after = _blind_coverage_row(health, "CHILD", "PARENT")
    assert (after["covered"], after["total"]) == (child_row["covered"], child_row["total"])


def test_check_srs_86_mcp_stdio_results_match_in_process(blind_mcp_root, clean_worktree):
    """MELLON-CHECK-SRS-86, -87, -89, -91, -94, -96, -97, -100: the validate_document,
    validate_repo and repo_health tools over standard input and output (rung 3) give the
    same codes, ok values and counts as in process. Condition: req-test with one
    ATTR_BAD_ENUM added to VCU3-SRS (which also has 6 suspect links)."""
    server = _blind_server()
    root = _blind_req_test_copy(blind_mcp_root, clean_worktree)
    p10 = root / "VCU3-SRS" / "objects" / "VCU3-SRS-10.xml"
    _blind_write(p10, p10.read_text(encoding="utf-8").replace(
        '<attr name="status">Draft</attr>', '<attr name="status">Bogus</attr>'))
    local = [server.validate_document("VCU3-SRS"), server.validate_repo(),
             server.repo_health()]
    remote = _blind_mcp_stdio(root, [("validate_document", {"doc_id": "VCU3-SRS"}),
                                     ("validate_repo", {}), ("repo_health", {})])

    def vd(r):
        return r["ok"], sorted(e["code"] for e in r["errors"])

    def vr(r):
        return (r["ok"], r["total_errors"],
                {d["doc_id"]: sorted(e["code"] for e in d["errors"]) for d in r["documents"]})

    def rh(r):
        return (r["total_validation_errors"], r["total_suspect_count"],
                _blind_per_doc(r, "validation_error_count"), _blind_per_doc(r, "suspect_count"))

    assert vd(local[0]) == vd(remote[0]) == (False, ["ATTR_BAD_ENUM"])
    assert vr(local[1]) == vr(remote[1]) == (False, 1, {
        "VCU3-SRS": ["ATTR_BAD_ENUM"], "VCU3-STS": [], "VCU3-SyRS": []})
    assert rh(local[2]) == rh(remote[2]) == (
        1, 6, {"VCU3-SRS": 1, "VCU3-STS": 0, "VCU3-SyRS": 0},
        {"VCU3-SRS": 6, "VCU3-STS": 0, "VCU3-SyRS": 0})
