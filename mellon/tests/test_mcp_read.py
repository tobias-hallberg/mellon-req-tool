"""Unit + integration tests for the read-only MCP tools.

Tools are invoked directly as Python callables (FastMCP's `@tool` decorator
leaves the function callable). `_State.repo_root` is set by the `mcp_repo`
fixture in conftest.py (or `real_repo` below for the req-test integration).
"""

from __future__ import annotations

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
    with pytest.raises(ValueError, match="No document"):
        server.edit_metadata("../X/X", "title", "outside")
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
