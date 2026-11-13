"""Tests for the mutation MCP tools.

Most tests use the `mcp_repo` fixture (tmp_path, no git). Tests that need a
real git history — `ack_suspects` — use `req_test_copy`, which copies the
committed req-test fixture into tmp_path so the test never mutates the
original.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from lxml import etree

from mellon.mcp import server
from mellon.service import DocumentService
from tests.store_support import (  # MELLON-STORE-SRS blind tests
    _BAD_SLUGS,
    _EXE,
    changed,
    git,
    git_init,
    snapshot,
)


# --- helper ---------------------------------------------------------------


@pytest.fixture
def req_test_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_worktree) -> Path:
    src = (Path(__file__).resolve().parents[2] / "req-test").resolve()
    if not (src / ".git").exists():
        pytest.skip(f"req-test repo not available at {src}")
    dst = tmp_path / "req-test"
    shutil.copytree(src, dst)
    clean_worktree(dst)
    monkeypatch.setattr(server._State, "repo_root", dst.resolve())
    monkeypatch.setattr(server._State, "read_only", False)
    return dst


# --- new_object ----------------------------------------------------------



def _refused_read_only(result: dict) -> None:
    """MELLON-MCP-SRS-100: a read-only server returns its refusal as a
    failure result with error_type PermissionError, not as a tool error."""
    assert result["ok"] is False, result
    assert result["error_type"] == "PermissionError", result
    assert "read-only" in result["error"], result
    assert result["affected_files"] == [], result


def _tool_result(res) -> dict:
    """The dict a tool returned, from FastMCP's call_tool."""
    if isinstance(res, tuple):
        res = res[1] if isinstance(res[1], dict) else res[0]
    if isinstance(res, dict):
        return res.get("result", res) if set(res) == {"result"} else res
    return json.loads(res[0].text)

def test_new_object_appends_top_level(mcp_repo: Path) -> None:
    result = server.new_object("PARENT", description="Freshly added")
    assert result["ok"] is True
    new_id = result["new_id"]
    assert new_id.startswith("PARENT-")
    # The object file and the manifest should be the affected pair.
    assert any(p.endswith(f"objects/{new_id}.xml") for p in result["affected_files"])
    assert any(p.endswith("PARENT.xml") for p in result["affected_files"])
    # And it actually lives on disk.
    assert (mcp_repo / "PARENT" / "objects" / f"{new_id}.xml").is_file()


def test_new_object_with_parent(mcp_repo: Path) -> None:
    result = server.new_object("PARENT", parent_id="PARENT-0", description="Child")
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    # New child's id should appear inside the PARENT-0 subtree in the manifest.
    new_id = result["new_id"]
    assert f'ref id="{new_id}"' in manifest


def test_new_object_unknown_doc_raises(mcp_repo: Path) -> None:
    # MELLON-MCP-SRS-45, -53: a failure result, not a tool error.
    result = server.new_object("NOPE")
    assert result["ok"] is False, result
    assert result["error_type"] == "EditorError", result
    assert "No document 'NOPE'" in result["error"], result


# --- new_object composite (attrs + links in one call) -------------------


def test_new_object_with_attrs_populates_dict(mcp_repo: Path) -> None:
    result = server.new_object(
        "CHILD",
        description="A new req",
        attrs={"type": "Requirement", "status": "Draft"},
    )
    assert result["ok"] is True
    new_id = result["new_id"]
    assert result["object"]["id"] == new_id
    assert result["object"]["attrs"] == {"type": "Requirement", "status": "Draft"}
    assert result["object"]["description"] == "A new req"
    assert set(result["attrs_set"]) == {"type", "status"}


def test_new_object_with_links_creates_link_file(mcp_repo: Path) -> None:
    result = server.new_object(
        "CHILD",
        description="Linked req",
        attrs={"type": "Requirement", "status": "Draft"},
        links=[
            {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"}
        ],
    )
    assert result["ok"] is True
    new_id = result["new_id"]
    # Link file written and exposed in affected_files.
    assert any(
        p.endswith(f"CHILD/links/{new_id}.xml") for p in result["affected_files"]
    )
    assert (mcp_repo / "CHILD" / "links" / f"{new_id}.xml").is_file()
    assert result["links_added"] == [
        {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"}
    ]


def test_new_object_pre_validation_unknown_attr_no_files_written(
    mcp_repo: Path,
) -> None:
    before = list((mcp_repo / "CHILD" / "objects").iterdir())
    result = server.new_object(
        "CHILD",
        description="Should not land",
        attrs={"frobnitz": "Requirement"},
    )
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"
    assert "frobnitz" in result["error"]
    # No new object file appeared.
    after = list((mcp_repo / "CHILD" / "objects").iterdir())
    assert before == after


def test_new_object_pre_validation_disallowed_target_doc(mcp_repo: Path) -> None:
    # PARENT has no declared parents, so it can only link to itself.
    result = server.new_object(
        "PARENT",
        description="Bad link target",
        links=[
            {"target_doc": "CHILD", "target_id": "CHILD-0", "type": "satisfies"}
        ],
    )
    assert result["ok"] is False
    assert "not allowed" in result["error"]
    assert result["error_type"] == "EditorError"


def test_new_object_pre_validation_bad_enum_value(mcp_repo: Path) -> None:
    result = server.new_object(
        "CHILD",
        description="bad enum",
        attrs={"type": "Banana"},  # not in the type options
    )
    assert result["ok"] is False
    assert "Banana" in result["error"]


def test_new_object_pre_validation_bad_links_no_files_written(mcp_repo: Path) -> None:
    before = sorted((mcp_repo / "CHILD" / "objects").iterdir())
    link = {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"}
    for links in ([link, link], [dict(link, target_id="PARENT-99")]):
        result = server.new_object("CHILD", attrs={"type": "Requirement"}, links=links)
        assert result["ok"] is False
        assert result["error_type"] == "EditorError"
        assert "partial" not in result
    assert sorted((mcp_repo / "CHILD" / "objects").iterdir()) == before


def test_new_object_simple_returns_populated_object(mcp_repo: Path) -> None:
    # Even without attrs/links, the response carries the populated object.
    result = server.new_object("CHILD", description="No attrs yet")
    assert result["ok"] is True
    assert result["object"]["description"] == "No attrs yet"
    assert result["object"]["attrs"] == {}


# --- copy_object -------------------------------------------------------------


def test_copy_object_single_copy(mcp_repo: Path) -> None:
    # CHILD-0 has type=Requirement, status=Draft, and a satisfies link to PARENT-1.
    result = server.copy_object("CHILD", "CHILD-0")
    assert result["ok"] is True
    assert len(result["new_ids"]) == 1
    new_id = result["new_ids"][0]
    # The new object has the same description + attrs.
    show = server.show_object("CHILD", new_id)
    assert show["object"]["description"] == "The system shall child."
    assert show["object"]["attrs"] == {"type": "Requirement", "status": "Draft"}
    # Outgoing link was copied by default.
    assert len(show["outgoing"]) == 1
    assert show["outgoing"][0]["link"]["target_id"] == "PARENT-1"


def test_copy_object_count_creates_n_copies(mcp_repo: Path) -> None:
    result = server.copy_object("CHILD", "CHILD-0", count=3)
    assert result["ok"] is True
    assert len(result["new_ids"]) == 3
    # All distinct ids.
    assert len(set(result["new_ids"])) == 3
    # Each carries the source's attrs.
    for nid in result["new_ids"]:
        show = server.show_object("CHILD", nid)
        assert show["object"]["attrs"] == {"type": "Requirement", "status": "Draft"}


def test_copy_object_copy_links_false(mcp_repo: Path) -> None:
    result = server.copy_object("CHILD", "CHILD-0", copy_links=False)
    new_id = result["new_ids"][0]
    show = server.show_object("CHILD", new_id)
    assert show["outgoing"] == []


def test_copy_object_parent_id_places_under_target(mcp_repo: Path) -> None:
    # Add a second object to use as a parent.
    second = server.new_object(
        "CHILD",
        description="parent-to-be",
        attrs={"type": "Heading", "status": "Draft"},
    )["new_id"]
    result = server.copy_object("CHILD", "CHILD-0", parent_id=second)
    assert result["ok"] is True
    new_id = result["new_ids"][0]
    outline = server.show_document("CHILD", include=["outline"])["outline"]
    # Find the `second` node in the outline; new_id should be a child.
    def child_ids_of(nodes, target):
        for n in nodes:
            if n["id"] == target:
                return [c["id"] for c in n["children"]]
            sub = child_ids_of(n["children"], target)
            if sub is not None:
                return sub
        return None
    children = child_ids_of(outline, second)
    assert children is not None
    assert new_id in children


def test_copy_object_count_zero_rejected(mcp_repo: Path) -> None:
    result = server.copy_object("CHILD", "CHILD-0", count=0)
    assert result["ok"] is False
    assert "count" in result["error"]


def test_copy_object_unknown_source_rejected(mcp_repo: Path) -> None:
    result = server.copy_object("CHILD", "CHILD-NOPE")
    assert result["ok"] is False
    assert "CHILD-NOPE" in result["error"]


def test_copy_object_unknown_parent_rejected(mcp_repo: Path) -> None:
    result = server.copy_object("CHILD", "CHILD-0", parent_id="CHILD-NOPE")
    assert result["ok"] is False
    assert "CHILD-NOPE" in result["error"]


def test_copy_object_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    _refused_read_only(server.copy_object("CHILD", "CHILD-0"))


# --- edit_attribute ------------------------------------------------------


def test_edit_attribute_success(mcp_repo: Path) -> None:
    result = server.set_attribute("PARENT", "PARENT-1", "status", "Draft")
    assert result["ok"] is True
    obj = (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").read_text(encoding="utf-8")
    assert ">Draft<" in obj


def test_edit_attribute_validation_fails(mcp_repo: Path) -> None:
    result = server.set_attribute("PARENT", "PARENT-1", "status", "Urgent")
    assert result["ok"] is False
    assert "Urgent" in result["error"]
    assert result["error_type"] == "EditorError"
    # File untouched.
    assert result["affected_files"] == []


def test_edit_attribute_unknown_attr(mcp_repo: Path) -> None:
    result = server.set_attribute("PARENT", "PARENT-1", "no-such-attr", "x")
    assert result["ok"] is False
    assert "no-such-attr" in result["error"]


# --- edit_description ---------------------------------------------------


def test_edit_description_replaces_text(mcp_repo: Path) -> None:
    result = server.edit_description("PARENT", "PARENT-1", "Updated text\nsecond line")
    assert result["ok"] is True
    obj = (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").read_text(encoding="utf-8")
    assert "Updated text" in obj
    assert "second line" in obj


# --- update_object composite -------------------------------------------


def test_update_object_no_changes_is_noop(mcp_repo: Path) -> None:
    result = server.update_object("CHILD", "CHILD-0")
    assert result["ok"] is True
    assert result["affected_files"] == []
    assert "no changes requested" in result["message"]


def test_update_object_description_only(mcp_repo: Path) -> None:
    result = server.update_object(
        "CHILD", "CHILD-0", description="Reworded body"
    )
    assert result["ok"] is True
    assert result["description_changed"] is True
    assert result["attrs_set"] == []
    obj = (mcp_repo / "CHILD" / "objects" / "CHILD-0.xml").read_text(encoding="utf-8")
    assert "Reworded body" in obj


def test_update_object_attrs_and_links_combined(mcp_repo: Path) -> None:
    # Existing CHILD-0 already has a satisfies-link to PARENT-1.
    # Add another (different type) and flip status to Approved in one call.
    result = server.update_object(
        "CHILD", "CHILD-0",
        attrs={"status": "Approved"},
        add_links=[
            {"target_doc": "PARENT", "target_id": "PARENT-0", "type": "satisfies"}
        ],
    )
    assert result["ok"] is True
    assert result["attrs_set"] == ["status"]
    assert result["links_added"] == [
        {"target_doc": "PARENT", "target_id": "PARENT-0", "type": "satisfies"}
    ]
    assert result["object"]["attrs"]["status"] == "Approved"
    links = (mcp_repo / "CHILD" / "links" / "CHILD-0.xml").read_text(encoding="utf-8")
    assert "PARENT-0" in links and "PARENT-1" in links


def test_update_object_remove_then_add_same_target_nets_to_add(mcp_repo: Path) -> None:
    # CHILD-0 currently has a satisfies-link to PARENT-1. Remove it and re-add
    # it (e.g., to change the type). The remove happens first, then the add.
    result = server.update_object(
        "CHILD", "CHILD-0",
        remove_links=[{"target_doc": "PARENT", "target_id": "PARENT-1"}],
        add_links=[
            {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "refines"}
        ],
    )
    assert result["ok"] is True
    assert result["links_removed"] == [
        {"target_doc": "PARENT", "target_id": "PARENT-1"}
    ]
    assert result["links_added"] == [
        {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "refines"}
    ]
    links = (mcp_repo / "CHILD" / "links" / "CHILD-0.xml").read_text(encoding="utf-8")
    assert 'type="refines"' in links


def test_update_object_unknown_obj_raises(mcp_repo: Path) -> None:
    result = server.update_object(
        "CHILD", "CHILD-NOPE", attrs={"status": "Approved"}
    )
    assert result["ok"] is False
    assert "CHILD-NOPE" in result["error"]
    assert result["error_type"] == "EditorError"


def test_update_object_bad_attr_value_no_files_touched(mcp_repo: Path) -> None:
    before = (
        mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    ).read_text(encoding="utf-8")
    result = server.update_object(
        "CHILD", "CHILD-0", attrs={"type": "Banana"}
    )
    assert result["ok"] is False
    assert "Banana" in result["error"]
    after = (
        mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    ).read_text(encoding="utf-8")
    assert before == after


# --- edit_metadata ------------------------------------------------------


def test_edit_metadata_sets_title(mcp_repo: Path) -> None:
    result = server.edit_metadata("CHILD", "title", "Renamed child")
    assert result["ok"] is True
    manifest = (mcp_repo / "CHILD" / "CHILD.xml").read_text(encoding="utf-8")
    assert "<title>Renamed child</title>" in manifest


def test_edit_metadata_unknown_field(mcp_repo: Path) -> None:
    result = server.edit_metadata("CHILD", "nonsense", "value")
    assert result["ok"] is False
    assert "nonsense" in result["error"]


def test_edit_metadata_rejects_issue_field(mcp_repo: Path) -> None:
    """Issue lives as a tag now — `edit_metadata` no longer accepts it."""
    result = server.edit_metadata("CHILD", "issue", "2")
    assert result["ok"] is False
    assert "issue" in result["error"]


# --- bulk_set_attribute ------------------------------------------------------


def test_bulk_set_attribute_happy(mcp_repo: Path) -> None:
    # Create two more objects in CHILD so we have three to bulk-update.
    a = server.new_object("CHILD", description="a", attrs={"type": "Requirement", "status": "Draft"})["new_id"]
    b = server.new_object("CHILD", description="b", attrs={"type": "Requirement", "status": "Draft"})["new_id"]
    result = server.bulk_set_attribute(
        "CHILD", ["CHILD-0", a, b], "status", "Approved"
    )
    assert result["ok"] is True
    assert set(result["set_on"]) == {"CHILD-0", a, b}
    assert len(result["affected_files"]) == 3
    for obj_id in result["set_on"]:
        text = (mcp_repo / "CHILD" / "objects" / f"{obj_id}.xml").read_text(
            encoding="utf-8"
        )
        assert ">Approved<" in text


def test_bulk_set_attribute_empty_is_noop(mcp_repo: Path) -> None:
    result = server.bulk_set_attribute("CHILD", [], "status", "Approved")
    assert result["ok"] is True
    assert result["set_on"] == []
    assert result["affected_files"] == []


def test_bulk_set_attribute_unknown_obj_no_files_touched(mcp_repo: Path) -> None:
    before = (
        mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    ).read_text(encoding="utf-8")
    result = server.bulk_set_attribute(
        "CHILD", ["CHILD-0", "CHILD-NOPE"], "status", "Approved"
    )
    assert result["ok"] is False
    assert "CHILD-NOPE" in result["error"]
    after = (
        mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    ).read_text(encoding="utf-8")
    assert before == after


def test_bulk_set_attribute_bad_enum_no_files_touched(mcp_repo: Path) -> None:
    before = (
        mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    ).read_text(encoding="utf-8")
    result = server.bulk_set_attribute(
        "CHILD", ["CHILD-0"], "status", "Banana"
    )
    assert result["ok"] is False
    assert "Banana" in result["error"]
    after = (
        mcp_repo / "CHILD" / "objects" / "CHILD-0.xml"
    ).read_text(encoding="utf-8")
    assert before == after


def test_bulk_set_attribute_unknown_attr_no_files_touched(mcp_repo: Path) -> None:
    result = server.bulk_set_attribute(
        "CHILD", ["CHILD-0"], "frobnitz", "Approved"
    )
    assert result["ok"] is False
    assert "frobnitz" in result["error"]


def test_bulk_set_attribute_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    _refused_read_only(server.bulk_set_attribute(
            "CHILD", ["CHILD-0"], "status", "Approved"
        ))


# --- reorder_outline ---------------------------------------------------------


def test_reorder_outline_round_trip_identity(mcp_repo: Path) -> None:
    # Fetch the current outline, send it back unchanged — no-op.
    before = server.show_document("PARENT", include=["outline"])["outline"]
    result = server.reorder_outline("PARENT", before)
    assert result["ok"] is True
    assert "PARENT/PARENT.xml" in result["affected_files"]
    after = server.show_document("PARENT", include=["outline"])["outline"]
    assert before == after


def test_reorder_outline_reparents(mcp_repo: Path) -> None:
    # Move PARENT-1 from being a child of PARENT-0 to being a sibling.
    result = server.reorder_outline(
        "PARENT",
        [
            {"id": "PARENT-0", "children": []},
            {"id": "PARENT-1", "children": []},
        ],
    )
    assert result["ok"] is True
    outline = server.show_document("PARENT", include=["outline"])["outline"]
    assert outline[0]["id"] == "PARENT-0"
    assert outline[0]["children"] == []
    assert outline[1]["id"] == "PARENT-1"


def test_reorder_outline_missing_id_returns_error(mcp_repo: Path) -> None:
    result = server.reorder_outline(
        "PARENT", [{"id": "PARENT-0", "children": []}]
    )
    assert result["ok"] is False
    assert "PARENT-1" in result["error"]
    # No file was written — outline unchanged.
    outline = server.show_document("PARENT", include=["outline"])["outline"]
    assert outline[0]["id"] == "PARENT-0"
    assert outline[0]["children"][0]["id"] == "PARENT-1"


def test_reorder_outline_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    _refused_read_only(server.reorder_outline(
            "PARENT",
            [
                {"id": "PARENT-0", "children": []},
                {"id": "PARENT-1", "children": []},
            ],
        ))


# --- tag_issue ----------------------------------------------------------


def test_tag_issue_creates_tag(req_test_copy: Path) -> None:
    import pygit2

    result = server.tag_issue("VCU3-SRS", "5")
    assert result["ok"] is True
    assert result["tag_name"] == "VCU3-SRS-issue-5"
    repo = pygit2.Repository(str(req_test_copy))
    assert "refs/tags/VCU3-SRS-issue-5" in repo.references


def test_tag_issue_validates_format(req_test_copy: Path) -> None:
    result = server.tag_issue("VCU3-SRS", "draft")
    assert result["ok"] is False
    assert "dot-separated numeric" in result["error"]


def test_tag_issue_refuses_duplicate(req_test_copy: Path) -> None:
    server.tag_issue("VCU3-SRS", "9")
    result = server.tag_issue("VCU3-SRS", "9")
    assert result["ok"] is False
    assert "already exists" in result["error"]


# --- add_parent_doc / remove_parent_doc --------------------------------


def test_add_and_remove_parent_doc(mcp_repo: Path) -> None:
    # PARENT currently has no <parents>; add CHILD (nonsensical but valid).
    result = server.add_parent_doc("PARENT", "CHILD")
    assert result["ok"] is True
    assert 'doc="CHILD"' in (mcp_repo / "PARENT" / "PARENT.xml").read_text(
        encoding="utf-8"
    )

    removed = server.remove_parent_doc("PARENT", "CHILD")
    assert removed["ok"] is True

    # Removing a non-existent parent returns ok=False.
    again = server.remove_parent_doc("PARENT", "CHILD")
    assert again["ok"] is False
    assert again["error_type"] == "NotFound"


# --- add_attr_option ---------------------------------------------------


def test_add_attr_option_success(mcp_repo: Path) -> None:
    result = server.add_attr_option("PARENT", "status", "Rejected")
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert "<option>Rejected</option>" in manifest


def test_add_attr_option_duplicate_fails(mcp_repo: Path) -> None:
    # Draft already exists.
    result = server.add_attr_option("PARENT", "status", "Draft")
    assert result["ok"] is False


# --- add_attribute / delete_attribute / remove_attr_option -------------


def test_add_attribute_enum_success(mcp_repo: Path) -> None:
    result = server.define_attribute(
        "PARENT", "priority", "enum", required=True, options=["Must", "Should"]
    )
    assert result["ok"] is True
    assert "priority" in result["message"]
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert 'name="priority"' in manifest
    assert "<option>Must</option>" in manifest
    assert "<option>Should</option>" in manifest


def test_add_attribute_text_success(mcp_repo: Path) -> None:
    result = server.define_attribute("PARENT", "comment", "text")
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert 'name="comment"' in manifest
    assert 'kind="text"' in manifest


def test_add_attribute_label_round_trips(mcp_repo: Path) -> None:
    result = server.define_attribute(
        "PARENT", "req-type", "text", label="Requirement Type"
    )
    assert result["ok"] is True
    summary = server.show_document("PARENT", include=["attributes"])
    [attr] = [a for a in summary["attributes"] if a["name"] == "req-type"]
    assert attr["label"] == "Requirement Type"


def test_add_attribute_duplicate_fails(mcp_repo: Path) -> None:
    result = server.define_attribute("PARENT", "status", "text")
    assert result["ok"] is False
    assert "already declared" in result["error"]
    assert result["error_type"] == "AlreadyExists"


def test_delete_attribute_success(mcp_repo: Path) -> None:
    result = server.delete_attribute("PARENT", "status")
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert 'name="status"' not in manifest
    # And every object lost its <attr name="status">.
    obj = (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").read_text(encoding="utf-8")
    assert 'name="status"' not in obj


def test_delete_attribute_unknown_fails(mcp_repo: Path) -> None:
    result = server.delete_attribute("PARENT", "madeup")
    assert result["ok"] is False
    assert "not declared" in result["error"]


def test_change_attribute_required_flips_flag(mcp_repo: Path) -> None:
    # PARENT has `status` as required=true; flip to false.
    result = server.change_attribute_required("PARENT", "status", False)
    assert result["ok"] is True
    assert "optional" in result["message"]
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert 'name="status" kind="enum" required="false"' in manifest


def test_change_attribute_required_unknown_fails(mcp_repo: Path) -> None:
    result = server.change_attribute_required("PARENT", "madeup", True)
    assert result["ok"] is False
    assert "not declared" in result["error"]
    assert result["error_type"] == "EditorError"


@pytest.mark.parametrize("kind", ["integer", "real", "boolean", "date"])
def test_add_attribute_non_string_kinds(mcp_repo: Path, kind: str) -> None:
    result = server.define_attribute("PARENT", f"x_{kind}", kind)
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert f'name="x_{kind}" kind="{kind}"' in manifest


def test_add_attribute_options_on_non_enum_fails(mcp_repo: Path) -> None:
    result = server.define_attribute(
        "PARENT", "weight", "integer", options=["1", "2"]
    )
    assert result["ok"] is False
    assert "cannot have options" in result["error"]


def test_define_attribute_default_value_backfills_objects(mcp_repo: Path) -> None:
    # CHILD has CHILD-0 with no `rationale`; add the attr with a default and
    # verify the existing object got the value.
    result = server.define_attribute(
        "CHILD", "rationale", "text",
        required=True, default_value="TBD",
    )
    assert result["ok"] is True
    assert result["backfilled"] == ["CHILD-0"]
    # The object file actually got rewritten.
    text = (mcp_repo / "CHILD" / "objects" / "CHILD-0.xml").read_text(
        encoding="utf-8"
    )
    assert ">TBD<" in text
    # validate_document is now clean (no ATTR_MISSING_REQUIRED).
    val = server.validate_document("CHILD")
    assert val["ok"] is True


def test_define_attribute_default_value_validates_against_kind(mcp_repo: Path) -> None:
    # Adding an integer attr with a non-integer default should fail before
    # the schema is written.
    result = server.define_attribute(
        "CHILD", "weight", "integer", default_value="heavy"
    )
    assert result["ok"] is False
    assert "integer" in result["error"]
    # Schema is unchanged (manifest still has only type + status).
    after = server.show_document("CHILD", include=["attributes"])
    assert {a["name"] for a in after["attributes"]} == {"type", "status"}


def test_define_attribute_default_value_skips_objects_already_set(
    mcp_repo: Path,
) -> None:
    # Set CHILD-0.rationale-equivalent before defining the attr by adding
    # the attr without default first, then writing a value, then re-defining
    # — actually simpler: define `rationale` without default, set CHILD-0's
    # value, delete the attr, redefine with default. That's contrived; instead
    # use a fresh object that doesn't have the attr.
    new_id = server.new_object(
        "CHILD",
        description="extra",
        attrs={"type": "Requirement", "status": "Draft"},
    )["new_id"]
    # Verify backfill targets both objects.
    result = server.define_attribute(
        "CHILD", "rationale", "text", default_value="TBD"
    )
    assert result["ok"] is True
    assert set(result["backfilled"]) == {"CHILD-0", new_id}


def test_define_attribute_without_default_value_no_backfill(mcp_repo: Path) -> None:
    # No default_value means backfilled is empty / response carries no key.
    result = server.define_attribute("CHILD", "comment", "text")
    assert result["ok"] is True
    assert "backfilled" not in result


def test_remove_attr_option_success(mcp_repo: Path) -> None:
    # First add an option that some object can use, then remove it.
    server.add_attr_option("PARENT", "status", "Reviewed")
    server.set_attribute("PARENT", "PARENT-1", "status", "Reviewed")

    result = server.remove_attr_option("PARENT", "status", "Reviewed")
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    assert "<option>Reviewed</option>" not in manifest
    # PARENT-1's status was Reviewed; it should now be blank but the element stays.
    obj = (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").read_text(encoding="utf-8")
    assert 'name="status"' in obj


def test_remove_attr_option_unknown_fails(mcp_repo: Path) -> None:
    result = server.remove_attr_option("PARENT", "status", "Nonsense")
    assert result["ok"] is False
    assert "not declared" in result["error"]


def test_remove_attr_option_text_attr_fails(mcp_repo: Path) -> None:
    # PARENT has no text attr in the basic fixture; add one first.
    server.define_attribute("PARENT", "comment", "text")
    result = server.remove_attr_option("PARENT", "comment", "x")
    assert result["ok"] is False
    assert "not enum" in result["error"]


# --- move_object -------------------------------------------------------


def test_move_object_to_top(mcp_repo: Path) -> None:
    # PARENT-1 is nested under PARENT-0; promote it to top level.
    result = server.move_object("PARENT", "PARENT-1", to_top=True)
    assert result["ok"] is True
    manifest = (mcp_repo / "PARENT" / "PARENT.xml").read_text(encoding="utf-8")
    # PARENT-0 should no longer contain PARENT-1 as a nested ref; PARENT-1 is now a sibling.
    assert manifest.count('ref id="PARENT-1"') == 1


def test_move_object_cycle_rejected(mcp_repo: Path) -> None:
    result = server.move_object("PARENT", "PARENT-0", parent_id="PARENT-1")
    assert result["ok"] is False
    assert "descendant" in result["error"].lower() or "itself" in result["error"].lower()


def test_move_object_no_intent(mcp_repo: Path) -> None:
    result = server.move_object("PARENT", "PARENT-1")
    assert result["ok"] is False


# --- add_link / remove_link -------------------------------------------


def test_add_link_no_git_has_no_ack(mcp_repo: Path) -> None:
    # CHILD already has a link; add a second one to PARENT-0.
    result = server.add_link("CHILD", "CHILD-0", "PARENT", "PARENT-0", "satisfies")
    assert result["ok"] is True
    # No git -> ack is None.
    assert result["ack"] is None
    links = (mcp_repo / "CHILD" / "links" / "CHILD-0.xml").read_text(encoding="utf-8")
    assert "PARENT-0" in links
    assert "PARENT-1" in links  # the pre-existing link survives


def test_add_link_duplicate_fails(mcp_repo: Path) -> None:
    # CHILD-0 -> PARENT-1 already exists.
    result = server.add_link("CHILD", "CHILD-0", "PARENT", "PARENT-1", "satisfies")
    assert result["ok"] is False
    assert "already exists" in result["error"]


def test_remove_link_success(mcp_repo: Path) -> None:
    result = server.remove_link("CHILD", "CHILD-0", "PARENT", "PARENT-1")
    assert result["ok"] is True
    # CHILD-0 had exactly one link, so the file should now be gone.
    assert result["links_file_deleted"] is True
    assert not (mcp_repo / "CHILD" / "links" / "CHILD-0.xml").exists()


def test_remove_link_not_found(mcp_repo: Path) -> None:
    result = server.remove_link("CHILD", "CHILD-0", "PARENT", "PARENT-0")
    assert result["ok"] is False
    assert result["error_type"] == "NotFound"


# --- delete_object ---------------------------------------------------


def test_delete_object_blocked_by_incoming(mcp_repo: Path) -> None:
    # PARENT-1 is linked IN to by CHILD-0.
    result = server.delete_object("PARENT", "PARENT-1")
    assert result["ok"] is False
    assert result["error_type"] == "BlockedByIncomingLinks"
    assert len(result["incoming"]) == 1
    assert result["incoming"][0]["source_doc"] == "CHILD"
    # File still there.
    assert (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").is_file()


def test_delete_object_force_cascades(mcp_repo: Path) -> None:
    result = server.delete_object("PARENT", "PARENT-1", force=True)
    assert result["ok"] is True
    # PARENT-1's object file gone, CHILD-0's link file gone (empty after strip).
    assert not (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").exists()
    assert not (mcp_repo / "CHILD" / "links" / "CHILD-0.xml").exists()


def test_delete_object_blocked_by_children(mcp_repo: Path) -> None:
    # PARENT-0 has PARENT-1 as a child.
    result = server.delete_object("PARENT", "PARENT-0")
    assert result["ok"] is False
    # Raised EditorError path — error_type reflects that.
    assert result["error_type"] == "EditorError"
    assert "children" in result["error"]


# --- manage_view (create / rename / delete / edit) ------------------


def test_manage_view_create_success(mcp_repo: Path) -> None:
    result = server.manage_view(
        "PARENT", action="create", name="Main View",
        visible_attrs=["status", "type"],
    )
    assert result["ok"] is True
    assert result["slug"] == "main-view"
    assert any(p.endswith("PARENT/views/main-view.xml") for p in result["affected_files"])
    on_disk = (mcp_repo / "PARENT" / "views" / "main-view.xml").read_text(encoding="utf-8")
    assert '<view name="Main View">' in on_disk
    assert '<attribute name="status"/>' in on_disk
    assert '<attribute name="type"/>' in on_disk


def test_manage_view_create_appears_in_show_document(mcp_repo: Path) -> None:
    server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"])
    summary = server.show_document("PARENT", include=["views"])
    assert summary["views"] == [
        {"slug": "dev", "name": "Dev", "visible": ["status"]}
    ]


def test_manage_view_create_dedupes_visible_attrs(mcp_repo: Path) -> None:
    server.manage_view(
        "PARENT", action="create", name="Dupes",
        visible_attrs=["status", "type", "status"],
    )
    summary = server.show_document("PARENT", include=["views"])
    [view] = summary["views"]
    assert view["visible"] == ["status", "type"]


def test_manage_view_create_slug_collision_appends_suffix(mcp_repo: Path) -> None:
    first = server.manage_view("PARENT", action="create", name="Main", visible_attrs=["status"])
    second = server.manage_view("PARENT", action="create", name="Main", visible_attrs=["type"])
    third = server.manage_view("PARENT", action="create", name="Main", visible_attrs=[])
    assert first["slug"] == "main"
    assert second["slug"] == "main-2"
    assert third["slug"] == "main-3"


def test_manage_view_rejects_path_traversal_slug(mcp_repo: Path) -> None:
    (mcp_repo / "PARENT" / "views").mkdir()
    target = mcp_repo / "PARENT" / "objects" / "PARENT-1.xml"
    result = server.manage_view("PARENT", action="delete", slug="../objects/PARENT-1")
    assert result["ok"] is False
    assert "invalid slug" in result["error"]
    assert target.is_file()


def test_manage_view_create_empty_name_fails(mcp_repo: Path) -> None:
    result = server.manage_view("PARENT", action="create", name="   ", visible_attrs=[])
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"


def test_manage_view_create_missing_name_fails(mcp_repo: Path) -> None:
    result = server.manage_view("PARENT", action="create")
    assert result["ok"] is False
    assert "create requires `name`" in result["error"]


def test_manage_view_rename_same_slug(mcp_repo: Path) -> None:
    server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"])
    result = server.manage_view("PARENT", action="rename", slug="dev", name="DEV")
    assert result["ok"] is True
    assert result["slug"] == "dev"
    assert result["affected_files"] == ["PARENT/views/dev.xml"]
    on_disk = (mcp_repo / "PARENT" / "views" / "dev.xml").read_text(encoding="utf-8")
    assert '<view name="DEV">' in on_disk


def test_manage_view_rename_new_slug_moves_file(mcp_repo: Path) -> None:
    server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"])
    result = server.manage_view("PARENT", action="rename", slug="dev", name="QA Pass")
    assert result["ok"] is True
    assert result["slug"] == "qa-pass"
    assert "PARENT/views/qa-pass.xml" in result["affected_files"]
    assert "PARENT/views/dev.xml" in result["affected_files"]
    assert (mcp_repo / "PARENT" / "views" / "qa-pass.xml").is_file()
    assert not (mcp_repo / "PARENT" / "views" / "dev.xml").exists()


def test_manage_view_rename_unknown_fails(mcp_repo: Path) -> None:
    result = server.manage_view("PARENT", action="rename", slug="missing", name="Anything")
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"
    assert result["affected_files"] == []


def test_manage_view_rename_missing_params_fails(mcp_repo: Path) -> None:
    result = server.manage_view("PARENT", action="rename", slug="dev")
    assert result["ok"] is False
    assert "rename requires" in result["error"]


def test_manage_view_delete_success(mcp_repo: Path) -> None:
    server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"])
    result = server.manage_view("PARENT", action="delete", slug="dev")
    assert result["ok"] is True
    assert result["affected_files"] == ["PARENT/views/dev.xml"]
    assert not (mcp_repo / "PARENT" / "views" / "dev.xml").exists()
    summary = server.show_document("PARENT", include=["views"])
    assert summary["views"] == []


def test_manage_view_delete_unknown_fails(mcp_repo: Path) -> None:
    result = server.manage_view("PARENT", action="delete", slug="missing")
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"


def test_manage_view_edit_replaces_visible_order(mcp_repo: Path) -> None:
    server.manage_view(
        "PARENT", action="create", name="Dev", visible_attrs=["status", "type"]
    )
    result = server.manage_view(
        "PARENT", action="edit", slug="dev", visible_attrs=["type", "status"]
    )
    assert result["ok"] is True
    assert result["slug"] == "dev"
    assert result["affected_files"] == ["PARENT/views/dev.xml"]
    summary = server.show_document("PARENT", include=["views"])
    [view] = summary["views"]
    assert view["visible"] == ["type", "status"]


def test_manage_view_edit_dedupes(mcp_repo: Path) -> None:
    server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"])
    server.manage_view(
        "PARENT", action="edit", slug="dev",
        visible_attrs=["type", "status", "type"],
    )
    [view] = server.show_document("PARENT", include=["views"])["views"]
    assert view["visible"] == ["type", "status"]


def test_manage_view_edit_unknown_fails(mcp_repo: Path) -> None:
    result = server.manage_view(
        "PARENT", action="edit", slug="missing", visible_attrs=["status"]
    )
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"
    assert result["affected_files"] == []


def test_manage_view_edit_preserves_display_name(mcp_repo: Path) -> None:
    server.manage_view(
        "PARENT", action="create", name="Dev View", visible_attrs=["status"]
    )
    server.manage_view(
        "PARENT", action="edit", slug="dev-view", visible_attrs=["type"]
    )
    on_disk = (
        mcp_repo / "PARENT" / "views" / "dev-view.xml"
    ).read_text(encoding="utf-8")
    assert '<view name="Dev View">' in on_disk
    assert '<attribute name="type"/>' in on_disk
    assert "status" not in on_disk


def test_manage_view_unknown_action_fails(mcp_repo: Path) -> None:
    result = server.manage_view("PARENT", action="explode")  # type: ignore[arg-type]
    assert result["ok"] is False
    assert "unknown action" in result["error"]


def test_manage_view_read_only_blocks(
    mcp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    _refused_read_only(server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"]))
    _refused_read_only(server.manage_view("PARENT", action="rename", slug="dev", name="Anything"))
    _refused_read_only(server.manage_view("PARENT", action="delete", slug="dev"))
    _refused_read_only(server.manage_view("PARENT", action="edit", slug="dev", visible_attrs=["status"]))


# --- read-only guard -------------------------------------------------


def test_read_only_blocks_writes(
    mcp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    _refused_read_only(server.new_object("PARENT"))
    _refused_read_only(server.set_attribute("PARENT", "PARENT-1", "status", "Draft"))
    # Read tools remain callable.
    docs = server.list_documents()
    assert {d["doc_id"] for d in docs} == {"PARENT", "CHILD"}


# Every registered tool not listed here must refuse under --read-only, so a
# new write tool that forgets `_guard_write()` fails the test below.
_READ_TOOLS = frozenset({
    "list_documents", "show_document", "search_objects", "show_object",
    "inspect_object", "outline", "validate_document", "validate_repo",
    "repo_health", "suspects", "trace", "incoming_links", "coverage_report",
    "git_history", "show_object_at", "compare_document", "review_suspect_link",
})
_TOOL_SCHEMAS = {t.name: t.inputSchema for t in asyncio.run(server.mcp.list_tools())}
# Real req-test ids for the arguments the read tools need; others get dummies.
_ARG_BY_NAME = {
    "doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1",
    "source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-1",
    "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42",
    "commit_ref": "HEAD",
}
_DUMMY_BY_TYPE = {
    "string": "x", "array": [], "object": {}, "boolean": False, "integer": 1,
}


def _required_args(schema: dict) -> dict:
    args = {}
    for name in schema.get("required", []):
        prop = schema["properties"][name]
        if name in _ARG_BY_NAME:
            args[name] = _ARG_BY_NAME[name]
        elif "enum" in prop:
            args[name] = prop["enum"][0]
        else:
            args[name] = _DUMMY_BY_TYPE[prop["type"]]
    return args


@pytest.mark.parametrize("tool", sorted(_TOOL_SCHEMAS))
def test_read_only_refuses_every_write_tool(
    req_test_copy: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    import pygit2

    assert _READ_TOOLS <= _TOOL_SCHEMAS.keys()
    monkeypatch.setattr(server._State, "read_only", True)
    args = _required_args(_TOOL_SCHEMAS[tool])
    if tool in _READ_TOOLS:
        asyncio.run(server.mcp.call_tool(tool, args))  # still works
    else:
        # MELLON-MCP-SRS-100: the refusal is the tool's failure result.
        _refused_read_only(_tool_result(asyncio.run(server.mcp.call_tool(tool, args))))
    assert not pygit2.Repository(str(req_test_copy)).status()


# --- ack_suspects against a real git repo ---------------------------


def test_ack_suspects_dry_run(req_test_copy: Path) -> None:
    # Fixture has 4 parent-edited suspects in VCU3-SRS (each has a current_hash).
    # Two others are never-reviewed on a different target; they also have a
    # current_hash because the target IS committed. Total 6.
    result = server.ack_suspects("VCU3-SRS", dry_run=True)
    assert result["ok"] is True
    assert result["affected_files"] == []
    assert len(result["would_acknowledge"]) == 6


def test_ack_suspects_rewrites_link_files(req_test_copy: Path) -> None:
    # Pre-condition: SRS has 6 suspects.
    before = server.suspects("VCU3-SRS")
    assert len(before) == 6

    result = server.ack_suspects("VCU3-SRS")
    assert result["ok"] is True
    assert len(result["acknowledged"]) == 6
    # Each acknowledged link writes to its source's links file.
    assert all(
        p.startswith("VCU3-SRS/links/") for p in result["affected_files"]
    )

    # After acking, the scan should return no suspects.
    after = server.suspects("VCU3-SRS")
    assert after == []


def test_ack_suspects_subset_acks_only_listed_keys(req_test_copy: Path) -> None:
    before = server.suspects("VCU3-SRS")
    assert len(before) == 6
    first = before[0]
    chosen_key = {
        "source_id": first["source_id"],
        "target_doc": first["link"]["target_doc"],
        "target_id": first["link"]["target_id"],
    }
    bogus_key = {
        "source_id": first["source_id"],
        "target_doc": "VCU3-SyRS",
        "target_id": "VCU3-SyRS-does-not-exist",
    }
    result = server.ack_suspects(
        "VCU3-SRS", link_keys=[chosen_key, bogus_key]
    )
    assert result["ok"] is True
    assert len(result["acknowledged"]) == 1
    assert result["acknowledged"][0]["source_id"] == chosen_key["source_id"]
    # The unknown key is reported in `skipped`, not silently dropped.
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["key"]["target_id"] == (
        "VCU3-SyRS-does-not-exist"
    )
    # The other five suspects remain.
    after = server.suspects("VCU3-SRS")
    assert len(after) == 5


# --- auto-commit (commit_message=…) ---------------------------------------


def test_auto_commit_set_attribute_against_git_repo(req_test_copy: Path) -> None:
    # set_attribute with commit_message=… auto-commits the affected files.
    import pygit2
    repo = pygit2.Repository(str(req_test_copy))
    before_head = str(repo.head.target)

    result = server.set_attribute(
        "VCU3-SRS", "VCU3-SRS-0", "type", "Requirement",
        commit_message="Refine type on SRS-0",
    )
    assert result["ok"] is True, result
    assert "commit" in result
    assert "short_hash" in result["commit"]
    assert "Via: mellon-mcp" in result["commit"]["commit_message"]
    after_head = str(repo.head.target)
    assert before_head != after_head


def test_auto_commit_skipped_when_no_message(req_test_copy: Path) -> None:
    # Same edit without commit_message leaves the working tree dirty.
    import pygit2
    repo = pygit2.Repository(str(req_test_copy))
    before_head = str(repo.head.target)
    result = server.set_attribute(
        "VCU3-SRS", "VCU3-SRS-0", "type", "Requirement",
    )
    assert result["ok"] is True
    assert "commit" not in result
    assert "commit_error" not in result
    after_head = str(repo.head.target)
    assert before_head == after_head


def test_auto_commit_skipped_on_write_failure(req_test_copy: Path) -> None:
    # If the write itself fails, no commit is attempted.
    result = server.set_attribute(
        "VCU3-SRS", "VCU3-SRS-0", "no-such-attr", "x",
        commit_message="should not commit",
    )
    assert result["ok"] is False, result
    assert "commit" not in result


def test_auto_commit_bulk_set_attribute_single_commit(req_test_copy: Path) -> None:
    import pygit2
    repo = pygit2.Repository(str(req_test_copy))
    parent_head = str(repo.head.target)
    result = server.bulk_set_attribute(
        "VCU3-SRS",
        ["VCU3-SRS-0", "VCU3-SRS-1", "VCU3-SRS-2"],
        "type", "Requirement",
        commit_message="Refine type on three SRS reqs",
    )
    assert result["ok"] is True, result
    assert "commit" in result
    new_head = str(repo.head.target)
    assert new_head != parent_head
    # All three objects are reflected on disk and in the new tree.
    for obj_id in ("VCU3-SRS-0", "VCU3-SRS-1", "VCU3-SRS-2"):
        text = (req_test_copy / "VCU3-SRS" / "objects" / f"{obj_id}.xml").read_text(
            encoding="utf-8"
        )
        assert ">Requirement<" in text
    # Working tree is clean after the commit — no pending changes.
    status = repo.status()
    assert not status, status


def test_auto_commit_uses_via_mellon_mcp_trailer(req_test_copy: Path) -> None:
    import pygit2
    repo = pygit2.Repository(str(req_test_copy))
    result = server.set_attribute(
        "VCU3-SRS", "VCU3-SRS-0", "type", "Requirement",
        commit_message="One-line message",
    )
    head_commit = repo.revparse_single(result["commit"]["short_hash"]).peel(pygit2.Commit)
    assert "Via: mellon-mcp" in head_commit.message


def _rename_committed_view(message: str) -> dict:
    created = server.manage_view(
        "VCU3-SRS", "create", name="Draft view", commit_message="Add view"
    )
    return server.manage_view(
        "VCU3-SRS", "rename", slug=created["slug"], name="Review view",
        commit_message=message,
    )


# Mutations that touch several files (or delete one): auto-commit must take
# every one of them.
_MULTI_FILE_MUTATIONS = {
    "create_document": lambda m: server.create_document("VCU3-NEW", commit_message=m),
    "delete_attribute": lambda m: server.delete_attribute(
        "VCU3-SRS", "rationale", commit_message=m
    ),
    "remove_attr_option": lambda m: server.remove_attr_option(
        "VCU3-SRS", "priority", "Should", commit_message=m
    ),
    "new_object_with_links": lambda m: server.new_object(
        "VCU3-SRS",
        attrs={"type": "Requirement"},
        links=[
            {"target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42", "type": "satisfies"}
        ],
        commit_message=m,
    ),
    "copy_object": lambda m: server.copy_object(
        "VCU3-SRS", "VCU3-SRS-1", commit_message=m
    ),
    "remove_link_deletes_file": lambda m: server.remove_link(
        "VCU3-SRS", "VCU3-SRS-1", "VCU3-SyRS", "VCU3-SyRS-42", commit_message=m
    ),
    "delete_object": lambda m: server.delete_object(
        "VCU3-SRS", "VCU3-SRS-1", force=True, commit_message=m
    ),
    "manage_view_rename": _rename_committed_view,
}


@pytest.mark.parametrize("mutation", sorted(_MULTI_FILE_MUTATIONS))
def test_auto_commit_multi_file_mutation_leaves_tree_clean(
    req_test_copy: Path, mutation: str
) -> None:
    import pygit2
    repo = pygit2.Repository(str(req_test_copy))
    result = _MULTI_FILE_MUTATIONS[mutation](f"Auto-commit {mutation}")
    assert result["ok"] is True, result
    assert "commit" in result, result
    # Every file the call touched went into the commit.
    assert not repo.status(), repo.status()


# --- create_document --------------------------------------------------------


def test_create_document_success(mcp_repo: Path) -> None:
    result = server.create_document("NEW_DOC", title="My New Document")
    assert result["ok"] is True
    assert result["doc_id"] == "NEW_DOC"
    assert result["folder"] == "NEW_DOC"
    assert any("NEW_DOC/NEW_DOC.xml" in p for p in result["affected_files"])
    assert (mcp_repo / "NEW_DOC" / "NEW_DOC.xml").is_file()
    assert (mcp_repo / "NEW_DOC" / "objects").is_dir()
    manifest = (mcp_repo / "NEW_DOC" / "NEW_DOC.xml").read_text(encoding="utf-8")
    assert "<title>My New Document</title>" in manifest
    assert 'id="NEW_DOC"' in manifest
    # summary included in result
    assert result["summary"]["doc_id"] == "NEW_DOC"
    assert result["summary"]["metadata"]["title"] == "My New Document"


def test_create_document_with_parents(mcp_repo: Path) -> None:
    result = server.create_document("GRANDCHILD", parent_docs=["PARENT", "CHILD"])
    assert result["ok"] is True
    manifest = (mcp_repo / "GRANDCHILD" / "GRANDCHILD.xml").read_text(encoding="utf-8")
    assert 'doc="PARENT"' in manifest
    assert 'doc="CHILD"' in manifest


def test_create_document_with_attr_schema(mcp_repo: Path) -> None:
    schema = [
        {"name": "type", "kind": "enum", "required": True, "options": ["Heading", "Functional"]},
        {"name": "status", "kind": "enum", "required": True, "options": ["Draft", "Approved"]},
        {"name": "rationale", "kind": "text", "required": False},
    ]
    result = server.create_document("DOC_WITH_SCHEMA", attr_schema=schema)
    assert result["ok"] is True
    manifest = (mcp_repo / "DOC_WITH_SCHEMA" / "DOC_WITH_SCHEMA.xml").read_text(encoding="utf-8")
    assert 'name="type"' in manifest
    assert 'name="status"' in manifest
    assert 'name="rationale"' in manifest
    assert ">Heading<" in manifest
    assert ">Draft<" in manifest
    assert result["summary"]["attributes"][0]["name"] == "type"


def test_create_document_duplicate_fails(mcp_repo: Path) -> None:
    result = server.create_document("PARENT")
    assert result["ok"] is False
    assert result["error_type"] == "AlreadyExists"
    assert "already exists" in result["error"]
    assert result["affected_files"] == []


def test_create_document_bad_id_fails(mcp_repo: Path) -> None:
    for bad_id in ("bad/id", "bad id", "bad.id", ""):
        result = server.create_document(bad_id)
        assert result["ok"] is False, f"expected failure for doc_id={bad_id!r}"
        assert result["error_type"] == "EditorError"


def test_create_document_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    import pytest
    _refused_read_only(server.create_document("SHOULD_FAIL"))


# --- set_figure / clear_figure ------------------------------------------


_MCP_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">
  <rect width="10" height="10"/>
</svg>
"""


def test_set_figure_attaches_svg(mcp_repo: Path, tmp_path: Path) -> None:
    src = tmp_path / "diagram.svg"
    src.write_bytes(_MCP_SVG)
    result = server.set_figure("PARENT", "PARENT-1", str(src))
    assert result["ok"] is True
    dest = mcp_repo / "PARENT" / "figures" / "PARENT-1.svg"
    assert dest.is_file()
    assert any(p.endswith("figures/PARENT-1.svg") for p in result["affected_files"])


def test_set_figure_relative_path_resolves_against_repo_root(
    mcp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (mcp_repo / "diagrams").mkdir()
    (mcp_repo / "diagrams" / "x.svg").write_bytes(_MCP_SVG)
    # The server's working directory is not the repo root.
    monkeypatch.chdir(mcp_repo / "PARENT")
    result = server.set_figure("PARENT", "PARENT-1", "diagrams/x.svg")
    assert result["ok"] is True, result
    assert (mcp_repo / "PARENT" / "figures" / "PARENT-1.svg").is_file()


def test_set_figure_appears_in_show_object(
    mcp_repo: Path, tmp_path: Path
) -> None:
    src = tmp_path / "diagram.svg"
    src.write_bytes(_MCP_SVG)
    server.set_figure("PARENT", "PARENT-1", str(src))
    obj = server.show_object("PARENT", "PARENT-1")
    assert obj["object"]["figure"] == "figures/PARENT-1.svg"
    obj_other = server.show_object("PARENT", "PARENT-0")
    assert obj_other["object"]["figure"] is None


def test_set_figure_rejects_non_svg(mcp_repo: Path, tmp_path: Path) -> None:
    src = tmp_path / "thing.png"
    src.write_bytes(b"\x89PNG\r\n")
    result = server.set_figure("PARENT", "PARENT-1", str(src))
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"
    assert ".svg" in result["error"]


def test_set_figure_unknown_obj(mcp_repo: Path, tmp_path: Path) -> None:
    src = tmp_path / "diagram.svg"
    src.write_bytes(_MCP_SVG)
    result = server.set_figure("PARENT", "PARENT-99", str(src))
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"


def test_clear_figure_removes(mcp_repo: Path, tmp_path: Path) -> None:
    src = tmp_path / "diagram.svg"
    src.write_bytes(_MCP_SVG)
    server.set_figure("PARENT", "PARENT-1", str(src))
    result = server.clear_figure("PARENT", "PARENT-1")
    assert result["ok"] is True
    assert not (mcp_repo / "PARENT" / "figures" / "PARENT-1.svg").exists()


def test_clear_figure_nothing_attached(mcp_repo: Path) -> None:
    result = server.clear_figure("PARENT", "PARENT-1")
    assert result["ok"] is False
    assert result["error_type"] == "NotFound"


def test_set_figure_read_only_raises(
    mcp_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "diagram.svg"
    src.write_bytes(_MCP_SVG)
    monkeypatch.setattr(server._State, "read_only", True)
    import pytest
    _refused_read_only(server.set_figure("PARENT", "PARENT-1", str(src)))


def test_clear_figure_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    import pytest
    _refused_read_only(server.clear_figure("PARENT", "PARENT-1"))


# --- set_table / clear_table ---------------------------------------------


_TABLE_DICT = {
    "columns": [{"align": "left"}, {"align": "right"}],
    "rows": [
        {"header": True, "cells": [{"text": "Signal"}, {"text": "Latency"}]},
        {"cells": ["OVERTEMP", "200 ms"]},  # bare strings are allowed
    ],
}


def test_set_table_tool_ok(mcp_repo: Path) -> None:
    result = server.set_table("PARENT", "PARENT-1", _TABLE_DICT)
    assert result["ok"] is True
    assert any(p.endswith("objects/PARENT-1.xml") for p in result["affected_files"])
    xml = (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").read_text(encoding="utf-8")
    assert "<table>" in xml and "Signal" in xml


def test_set_table_tool_bad_geometry(mcp_repo: Path) -> None:
    bad = {"columns": [{"align": "left"}, {"align": "left"}],
           "rows": [{"cells": [{"text": "only-one"}]}]}
    result = server.set_table("PARENT", "PARENT-1", bad)
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"


def test_set_table_tool_malformed_shape(mcp_repo: Path) -> None:
    result = server.set_table("PARENT", "PARENT-1", {"columns": "nope", "rows": []})
    assert result["ok"] is False
    assert "lists" in result["error"]


def test_clear_table_tool_ok(mcp_repo: Path) -> None:
    server.set_table("PARENT", "PARENT-1", _TABLE_DICT)
    result = server.clear_table("PARENT", "PARENT-1")
    assert result["ok"] is True
    xml = (mcp_repo / "PARENT" / "objects" / "PARENT-1.xml").read_text(encoding="utf-8")
    assert "<table>" not in xml


def test_clear_table_tool_no_table(mcp_repo: Path) -> None:
    result = server.clear_table("PARENT", "PARENT-1")
    assert result["ok"] is False


def test_set_table_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    import pytest
    _refused_read_only(server.set_table("PARENT", "PARENT-1", _TABLE_DICT))


# --- export_document -------------------------------------------------------


@pytest.fixture
def fake_pdf(monkeypatch):
    """Replace the tectonic-backed renderer with a call-capturing fake.

    Like the real one, it reports figures it leaves out through the
    `unrendered` out-list — here every figure, as if draw.io were missing.
    """
    from mellon.export import unrendered_figures

    calls: list[dict] = []

    def _fake(doc, **kwargs):
        calls.append({"doc_id": doc.id, **kwargs})
        if kwargs.get("unrendered") is not None:
            kwargs["unrendered"].extend(unrendered_figures(doc))
        return b"%PDF-fake"

    monkeypatch.setattr(server, "export_pdf", _fake)
    return calls


def test_export_document_writes_pdf(mcp_repo: Path, fake_pdf) -> None:
    # Relative path resolves against the repo root.
    result = server.export_document("CHILD", "out/report.pdf")
    assert result["ok"] is True
    written = Path(result["out_path"])
    assert written == mcp_repo / "out" / "report.pdf"
    assert written.read_bytes() == b"%PDF-fake"
    assert result["bytes"] == len(b"%PDF-fake")
    # mcp_repo is not a git repo → no issue tag → no rev on the cover.
    assert result["rev"] is None
    assert fake_pdf[0]["doc_id"] == "CHILD"
    # The document folder goes along so figures can be rendered.
    assert fake_pdf[0]["folder"] == mcp_repo / "CHILD"
    # No figures in CHILD → nothing to warn about.
    assert "unrendered_figures" not in result
    assert "not rendered" not in result["message"]


def test_export_document_passes_parents(mcp_repo: Path, fake_pdf) -> None:
    """CHILD declares PARENT, so the PDF gets its Traceability section. No
    git here, so there are no link states to warn about."""
    result = server.export_document("CHILD", "a.pdf", attrs=["status"])
    server.export_document("PARENT", "b.pdf")
    child, parent = fake_pdf
    assert list(child["parents"]) == ["PARENT"]
    assert child["parents"]["PARENT"].id == "PARENT"
    assert parent["parents"] == {}
    assert "suspect_links" not in result
    assert "suspect" not in result["message"]


def test_export_document_reports_suspect_links(req_test_copy: Path, fake_pdf) -> None:
    """The PDF shows no link states, so suspect links come back in the
    result — and still don't stop the export."""
    result = server.export_document("VCU3-SRS", "srs.pdf")
    assert result["ok"] is True
    assert (req_test_copy / "srs.pdf").exists()
    links = result["suspect_links"]
    assert links and {d["state"] for d in links} == {"suspect"}
    assert all(d["source_id"].startswith("VCU3-SRS-") for d in links)
    assert f"{len(links)} suspect and 0 broken link(s)" in result["message"]
    # The SyRS links nowhere: nothing to report.
    assert "suspect_links" not in server.export_document("VCU3-SyRS", "syrs.pdf")


def test_export_document_rendered_figures_are_not_listed(
    mcp_repo: Path, monkeypatch
) -> None:
    # draw.io rendered every figure: the out-list stays empty.
    monkeypatch.setattr(server, "export_pdf", lambda doc, **kwargs: b"%PDF-fake")
    figures = mcp_repo / "CHILD" / "figures"
    figures.mkdir()
    (figures / "CHILD-0.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8"
    )
    result = server.export_document("CHILD", "out/report.pdf")
    assert result["ok"] is True
    assert "unrendered_figures" not in result


def test_export_document_lists_unrendered_figures(
    mcp_repo: Path, fake_pdf, monkeypatch
) -> None:
    monkeypatch.setattr(server, "find_drawio", lambda *_a, **_k: None)
    figures = mcp_repo / "CHILD" / "figures"
    figures.mkdir()
    (figures / "CHILD-0.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8"
    )
    result = server.export_document("CHILD", "out/report.pdf")
    assert result["ok"] is True
    assert result["unrendered_figures"] == ["CHILD-0"]
    assert "1 figure(s) not rendered" in result["message"]
    assert result["drawio_found"] is False


def test_export_document_absolute_out_path(
    mcp_repo: Path, fake_pdf, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere" / "child.pdf"
    result = server.export_document("CHILD", str(target))
    assert result["ok"] is True
    assert Path(result["out_path"]) == target
    assert target.is_file()


def test_export_document_rev_plumbing(mcp_repo: Path, fake_pdf) -> None:
    # Explicit rev passes through.
    result = server.export_document("CHILD", "a.pdf", rev="B.2")
    assert result["rev"] == "B.2"
    assert fake_pdf[-1]["rev"] == "B.2"
    # Empty string suppresses the rev.
    result = server.export_document("CHILD", "b.pdf", rev="")
    assert result["rev"] is None
    assert fake_pdf[-1]["rev"] is None


def test_export_document_attrs_passthrough(mcp_repo: Path, fake_pdf) -> None:
    server.export_document("CHILD", "a.pdf", attrs=["status"])
    assert fake_pdf[-1]["attrs"] == ("status",)
    # Omitted attrs → renderer's own default set stands.
    server.export_document("CHILD", "b.pdf")
    assert "attrs" not in fake_pdf[-1]


@pytest.mark.parametrize(
    "out_path",
    ["out/report", "out/report.txt", ".git/report.pdf", "out/../.git/hooks/report.pdf"],
)
def test_export_document_refuses_bad_out_path(
    mcp_repo: Path, fake_pdf, out_path: str
) -> None:
    (mcp_repo / ".git").mkdir()
    result = server.export_document("CHILD", out_path)
    assert result["ok"] is False
    assert result["error_type"] == "EditorError"
    assert fake_pdf == []  # refused before rendering
    assert not list(mcp_repo.rglob("report*"))


def test_export_document_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    _refused_read_only(server.export_document("CHILD", "out.pdf"))


def test_export_document_unknown_doc_raises(mcp_repo: Path, fake_pdf) -> None:
    with pytest.raises(ValueError, match="No document"):
        server.export_document("NOPE", "out.pdf")


def test_export_document_tectonic_error_is_in_band(
    mcp_repo: Path, monkeypatch
) -> None:
    from mellon.export import TectonicError

    def _boom(doc, **kwargs):
        raise TectonicError("tectonic binary not found")

    monkeypatch.setattr(server, "export_pdf", _boom)
    result = server.export_document("CHILD", "out.pdf")
    assert result["ok"] is False
    assert result["error_type"] == "TectonicError"
    assert "tectonic" in result["error"]


def test_export_document_real_compile(mcp_repo: Path) -> None:
    from mellon.export import _find_tectonic

    if _find_tectonic() is None:
        pytest.skip("tectonic binary not available")
    result = server.export_document("CHILD", "real.pdf")
    assert result["ok"] is True
    blob = Path(result["out_path"]).read_bytes()
    assert blob.startswith(b"%PDF")


# MELLON-STORE-SRS blind tests


def test_store_srs_65_mcp_issue_tag_keeps_issue_out_of_manifest(tmp_path, git_env, mcp_at):
    """MELLON-STORE-SRS-65: through the MCP commit and issue tools, a new issue
    is the tag DOC-issue-2 on the commit, and the manifest there holds no issue
    element."""
    root = git_init(git_env, tmp_path / "iss")
    svc = DocumentService(root)
    svc.create_document("DOC", title="Issued")
    svc.create_object("DOC", description="First")
    svc.commit_document("DOC", "Issue 1", new_issue="1")
    mcp_at(root)
    assert server.edit_description("DOC", "DOC-0", "Second")["ok"]
    assert server.commit_changes("Second")["ok"]
    assert server.tag_issue("DOC", "2")["ok"]
    head = git(git_env, root, "rev-parse", "HEAD")
    assert git(git_env, root, "rev-parse", "DOC-issue-2^{commit}") == head
    manifest = git(git_env, root, "show", "HEAD:DOC/DOC.xml")
    assert etree.fromstring(manifest.encode()).findall(".//issue") == [], manifest


@pytest.mark.parametrize("slug", _BAD_SLUGS, ids=repr)
@pytest.mark.parametrize("action", ["delete", "rename", "edit"])
def test_store_srs_155_bad_slug_refused_by_manage_view(slug_doc, mcp_at, action, slug):
    """MELLON-STORE-SRS-155: the MCP server's manage_view refuses each bad slug
    for delete, rename and edit, and no file changes."""
    root = slug_doc.parent
    mcp_at(root)
    before = snapshot(root)
    kwargs = {"slug": slug}
    if action == "rename":
        kwargs["name"] = "Renamed"
    if action == "edit":
        kwargs["visible_attrs"] = ["rationale"]
    try:
        result = server.manage_view("DOC", action, **kwargs)
    except Exception:  # a raised tool error is a refusal too
        result = {"ok": False}
    assert not result.get("ok"), result
    assert changed(before, snapshot(root)) == []


@pytest.mark.parametrize("action", ["delete", "rename", "edit"])
def test_store_srs_155_good_slug_accepted_by_manage_view(slug_doc, mcp_at, action):
    """MELLON-STORE-SRS-155 (other side of the boundary): the slug main matches
    the pattern, so manage_view carries out the same delete, rename and edit
    requests; this also shows the refusals above aren't caused by the call."""
    root = slug_doc.parent
    mcp_at(root)
    kwargs = {"slug": "main"}
    if action == "rename":
        kwargs["name"] = "Renamed"
    if action == "edit":
        kwargs["visible_attrs"] = ["rationale"]
    before = snapshot(root)
    result = server.manage_view("DOC", action, **kwargs)
    assert result.get("ok"), result
    assert changed(before, snapshot(root)) != []


def _tectonic() -> str | None:
    if os.environ.get("MELLON_TECTONIC"):
        return os.environ["MELLON_TECTONIC"]
    found = shutil.which("tectonic")
    if found:
        return found
    built = Path(__file__).resolve().parents[1] / "build" / "dist" / "Mellon" / "tools" / f"tectonic{_EXE}"
    return str(built) if built.exists() else None


def test_store_srs_210_export_changes_no_file(read_only_repo, tmp_path, mcp_at, monkeypatch):
    """MELLON-STORE-SRS-210: when Mellon exports a document, it leaves each data
    file and figure file unchanged. Exporting PARENT, which has a figure, to a
    PDF outside the repository changes no file of the repository."""
    root, _ = read_only_repo
    tectonic = _tectonic()
    if tectonic is None:
        pytest.skip("tectonic isn't available, so a PDF can't be exported here")
    monkeypatch.setenv("MELLON_TECTONIC", tectonic)
    mcp_at(root)
    before = snapshot(root, include_git=False)
    out = tmp_path / "out" / "parent.pdf"
    out.parent.mkdir()
    result = server.export_document("PARENT", str(out))
    assert result.get("ok") and out.is_file(), result
    assert changed(before, snapshot(root, include_git=False)) == []


# MELLON-EDIT-SRS blind tests (1-175)
#
# Written blind from MELLON-EDIT-SRS and the public signatures (docs/specification.md,
# step 7). Helpers are in tests/edit_support.py, which builds on tests/store_support.py.

from pathlib import Path  # noqa: E402

import pygit2  # noqa: E402
import pytest  # noqa: E402

from mellon import editor, git_ops  # noqa: E402
from mellon.loader import load_document  # noqa: E402
from mellon.service import DocumentService  # noqa: E402
from tests.edit_support import (  # noqa: E402
    all_object_ids,
    assert_link_kept,
    basic_repo,
    child_parent_repo,
    copy_repo,
    delete_repo,
    manifest_attr,
    manifest_options,
    mcp_refused,
    obj_file,
    order_repo,
    read_obj,
    sub_objects,
    tools,
    with_figure,
    write_doc,
)
from tests.store_support import changed, mcp_stdio, snapshot  # noqa: E402,F401,F811


@pytest.mark.parametrize("value", ["draft", "APPROVED"])
@pytest.mark.parametrize("tool", ["set_attribute", "new_object", "update_object", "bulk_set_attribute"])
def test_edit_srs_10_enum_value_in_other_letter_case_refused(tmp_path, mcp_at, commit_all, tool, value):
    """MELLON-EDIT-SRS-10: an enumeration value that matches an option only when letter
    case is ignored is refused, through each MCP tool that sets values, and no file changes."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    call = {
        "set_attribute": lambda: srv.set_attribute("SRS", "SRS-1", "status", value),
        "new_object": lambda: srv.new_object("SRS", parent_id="SRS-0", description="x",
                                             attrs={"type": "Requirement", "status": value}),
        "update_object": lambda: srv.update_object("SRS", "SRS-1", attrs={"status": value}),
        "bulk_set_attribute": lambda: srv.bulk_set_attribute("SRS", ["SRS-1", "SRS-2"], "status", value),
    }[tool]
    res = mcp_refused(call)
    assert res.get("ok") is False, f"{tool} accepted status={value!r}: {res}"
    assert changed(before, snapshot(root)) == [], f"{tool} changed files although refused"


@pytest.mark.parametrize("tool", ["set_attribute", "new_object", "update_object", "bulk_set_attribute"])
def test_edit_srs_10_enum_value_in_exact_case_accepted(tmp_path, mcp_at, commit_all, tool):
    """MELLON-EDIT-SRS-10 (other side of the boundary): exactly an option, letter case
    included, is accepted and stored."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    if tool == "set_attribute":
        res = srv.set_attribute("SRS", "SRS-1", "status", "Approved")
        target = "SRS-1"
    elif tool == "new_object":
        res = srv.new_object("SRS", parent_id="SRS-0", description="x",
                             attrs={"type": "Requirement", "status": "Approved"})
        target = res.get("new_id")
    elif tool == "update_object":
        res = srv.update_object("SRS", "SRS-1", attrs={"status": "Approved"})
        target = "SRS-1"
    else:
        res = srv.bulk_set_attribute("SRS", ["SRS-1"], "status", "Approved")
        target = "SRS-1"
    assert res.get("ok") is True, res
    assert read_obj(root, "SRS", target)[0]["status"] == "Approved"


def test_edit_srs_12_update_object_with_bad_enum_value_refused_whole(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-12: update_object holding a new description, a valid status and an
    enumeration value that isn't an option is refused whole: no file of the repository
    changes, not even the description or the valid attribute."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    res = mcp_refused(srv.update_object, "SRS", "SRS-1", description="New text",
                      attrs={"status": "Approved", "type": "Banana"})
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


def test_edit_srs_12_update_object_with_undefined_attribute_refused_whole(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-12: update_object naming an attribute the document doesn't define,
    next to a valid one and a new description, is refused whole with no file written."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    res = mcp_refused(srv.update_object, "SRS", "SRS-1", description="New text",
                      attrs={"status": "Approved", "frobnitz": "x"})
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


@pytest.mark.parametrize("attrs", [
    {"type": "Requirement", "frobnitz": "x"},
    {"type": "Banana"},
], ids=["undefined-attribute", "bad-enum-value"])
def test_edit_srs_12_new_object_refused_whole(tmp_path, mcp_at, commit_all, attrs):
    """MELLON-EDIT-SRS-12: new_object with a refused attribute name or value, a description
    and a valid link is refused whole: no object file, no links file, and the manifest
    (its next object number included) keeps its bytes."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    res = mcp_refused(srv.new_object, "SRS", parent_id="SRS-0", description="x", attrs=attrs,
                      links=[{"target_doc": "SYS", "target_id": "SYS-0", "link_type": "satisfies"}])
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


@pytest.mark.parametrize("name,value", [("frobnitz", "x"), ("status", "Banana")],
                         ids=["undefined-attribute", "bad-enum-value"])
def test_edit_srs_12_bulk_set_attribute_refused_whole(tmp_path, mcp_at, commit_all, name, value):
    """MELLON-EDIT-SRS-12: bulk_set_attribute on two objects with a refused attribute name
    or value is refused before any file is written."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    res = mcp_refused(srv.bulk_set_attribute, "SRS", ["SRS-1", "SRS-2"], name, value)
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


def test_edit_srs_12_refusal_holds_while_another_document_fails_to_load(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-12 (startup with a bad input): with another document that fails to
    load, update_object holding a refused value is still refused whole."""
    root = basic_repo(tmp_path)
    write_doc(root, "BROKEN", objects={}, outline=[])
    (root / "BROKEN" / "BROKEN.xml").write_text("<document id='BROKEN'>", encoding="utf-8", newline="\n")
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    res = mcp_refused(srv.update_object, "SRS", "SRS-1", description="New text",
                      attrs={"status": "Approved", "type": "Banana"})
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


def test_edit_srs_12_refused_calls_write_nothing_over_stdio(tmp_path, commit_all):
    """MELLON-EDIT-SRS-12, over the MCP server's stdio interface: update_object and
    new_object holding a refused value are refused whole, and no file changes."""
    root = basic_repo(tmp_path)
    commit_all(root)
    before = snapshot(root)
    results = mcp_stdio(root, [
        ("update_object", {"doc_id": "SRS", "obj_id": "SRS-1", "description": "New text",
                           "attrs": {"status": "Approved", "type": "Banana"}}),
        ("new_object", {"doc_id": "SRS", "parent_id": "SRS-0", "description": "x",
                        "attrs": {"type": "Requirement", "frobnitz": "x"},
                        "links": [{"target_doc": "SYS", "target_id": "SYS-0", "link_type": "satisfies"}]}),
    ])
    for body in results:
        assert isinstance(body, dict) and body.get("ok") is False, body
    assert changed(before, snapshot(root)) == []


def test_edit_srs_20_mcp_create_document_has_no_objects(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-20: create_document makes the folder and manifest with no objects:
    object count 0, an empty outline, and no object file."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    res = srv.create_document("EMPTYDOC")
    assert res.get("ok") is True, res
    folder = root / "EMPTYDOC"
    assert (folder / "EMPTYDOC.xml").is_file()
    shown = srv.show_document("EMPTYDOC", include=["outline"])
    assert shown["object_count"] == 0
    assert shown["outline"] == []
    assert not list(folder.glob("objects/*"))


def test_edit_srs_23_mcp_refuses_existing_plain_folder(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-23: a folder at the root named as the new document ID, even an empty
    one that isn't a document, makes create_document refuse with AlreadyExists, and the
    folder stays empty."""
    root = basic_repo(tmp_path)
    (root / "PLAIN").mkdir()
    commit_all(root)
    srv = tools(mcp_at, root)
    res = mcp_refused(srv.create_document, "PLAIN")
    assert res.get("ok") is False, res
    assert res.get("error_type") == "AlreadyExists", res
    assert list((root / "PLAIN").iterdir()) == []


@pytest.mark.parametrize("field,value", [("author", "A. Person"), ("date", "2026-11-11"), ("title", "A title")])
def test_edit_srs_36_edit_metadata_sets_field(tmp_path, mcp_at, commit_all, field, value):
    """MELLON-EDIT-SRS-36: edit_metadata sets the title, author or date, and show_document
    returns the new value."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    res = srv.edit_metadata("SRS", field, value)
    assert res.get("ok") is True, res
    assert srv.show_document("SRS", include=["metadata"])["metadata"][field] == value
    assert getattr(load_document(root / "SRS").metadata, field) == value


def test_edit_srs_36_edit_metadata_over_stdio(tmp_path, commit_all):
    """MELLON-EDIT-SRS-36 over stdio: edit_metadata sets the author and the date, and
    show_document returns them."""
    root = basic_repo(tmp_path)
    commit_all(root)
    results = mcp_stdio(root, [
        ("edit_metadata", {"doc_id": "SRS", "field": "author", "value": "A. Person"}),
        ("edit_metadata", {"doc_id": "SRS", "field": "date", "value": "2026-11-11"}),
        ("show_document", {"doc_id": "SRS", "include": ["metadata"]}),
    ])
    assert results[0].get("ok") is True and results[1].get("ok") is True, results
    meta = results[2]["metadata"]
    assert meta["author"] == "A. Person" and meta["date"] == "2026-11-11"


def test_edit_srs_36_edit_metadata_without_git_user_name(tmp_path, mcp_at, commit_all, no_git_user):
    """MELLON-EDIT-SRS-36 (no user.name): setting metadata needs no commit, so it works in a
    repository whose git has no user.name."""
    root = basic_repo(tmp_path)
    commit_all(root)
    assert git_ops.git_user_name(pygit2.Repository(str(root))) is None, "precondition: no user.name"
    res = tools(mcp_at, root).edit_metadata("SRS", "author", "A. Person")
    assert res.get("ok") is True, res
    assert load_document(root / "SRS").metadata.author == "A. Person"


def test_edit_srs_51_mcp_remove_parent_doc_keeps_links(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-51 through remove_parent_doc: the link stays, byte-identical."""
    root = child_parent_repo(tmp_path, commit_all)
    before = (root / "CHILD" / "links" / "CHILD-0.xml").read_bytes()
    res = tools(mcp_at, root).remove_parent_doc("CHILD", "PARENT")
    assert res.get("ok") is True, res
    assert_link_kept(root, before, mcp_at)


@pytest.mark.parametrize("options", [None, []], ids=["no-options", "empty-options"])
def test_edit_srs_65_mcp_refuses_enum_without_options(tmp_path, mcp_at, commit_all, options):
    """MELLON-EDIT-SRS-65: define_attribute for an enumeration with no option is refused and
    no file changes."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    before = snapshot(root)
    res = mcp_refused(srv.define_attribute, "SRS", "prio", "enum", options=options)
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


def test_edit_srs_65_enum_with_one_option_accepted(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-65 (other side of the boundary): an enumeration with one option is
    defined."""
    root = basic_repo(tmp_path)
    commit_all(root)
    res = tools(mcp_at, root).define_attribute("SRS", "prio", "enum", options=["High"])
    assert res.get("ok") is True, res
    assert manifest_options(root, "SRS", "prio") == ("High",)


def test_edit_srs_67_attribute_defined_not_required_by_default(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-67: define_attribute without the required flag defines the attribute
    as not required, so objects without it raise no validation error."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    assert DocumentService(root).validate_document("SRS") == [], "precondition: SRS validates clean"
    res = srv.define_attribute("SRS", "comment", "text")
    assert res.get("ok") is True, res
    shown = {a["name"]: a for a in srv.show_document("SRS", include=["attributes"])["attributes"]}
    assert shown["comment"]["required"] is False
    assert DocumentService(root).validate_document("SRS") == []


def test_edit_srs_68_mcp_define_without_default_touches_manifest_only(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-68: define_attribute without a default value leaves every object file
    byte-identical and reports only the manifest as affected."""
    root = basic_repo(tmp_path)
    commit_all(root)
    before = snapshot(root)
    res = tools(mcp_at, root).define_attribute("SRS", "comment", "text")
    assert res.get("ok") is True, res
    assert changed(before, snapshot(root)) == ["SRS/SRS.xml"]
    assert [Path(p).as_posix() for p in res["affected_files"]] == ["SRS/SRS.xml"]


def test_edit_srs_93_mcp_change_required_leaves_objects_unchanged(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-93: change_attribute_required to true and back to false on rationale,
    which some objects lack, leaves every object file byte-identical and reports only the
    manifest as affected each time."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    for required in (True, False):
        before = snapshot(root)
        res = srv.change_attribute_required("SRS", "rationale", required)
        assert res.get("ok") is True, res
        assert changed(before, snapshot(root)) == ["SRS/SRS.xml"]
        assert [Path(p).as_posix() for p in res["affected_files"]] == ["SRS/SRS.xml"]
        assert manifest_attr(root, "SRS", "rationale").get("required") == ("true" if required else "false")


def test_edit_srs_118_new_object_under_parent_goes_last(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-118: new_object with parent_id SRS-4 (sub-objects SRS-5, SRS-2)
    places the object last: SRS-5, SRS-2, <new>."""
    root = order_repo(tmp_path, commit_all)
    res = tools(mcp_at, root).new_object("SRS", parent_id="SRS-4", description="x")
    assert res.get("ok") is True, res
    assert sub_objects(root, "SRS", "SRS-4") == ["SRS-5", "SRS-2", res["new_id"]]
    assert sub_objects(root, "SRS") == ["SRS-4", "SRS-1", "SRS-3"]


def test_edit_srs_118_new_object_without_parent_goes_last_at_top(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-118: new_object without a parent places the object last at the top
    level: SRS-4, SRS-1, SRS-3, <new>."""
    root = order_repo(tmp_path, commit_all)
    res = tools(mcp_at, root).new_object("SRS", description="x")
    assert res.get("ok") is True, res
    assert sub_objects(root, "SRS") == ["SRS-4", "SRS-1", "SRS-3", res["new_id"]]
    assert sub_objects(root, "SRS", "SRS-4") == ["SRS-5", "SRS-2"]


def test_edit_srs_119_default_value_not_applied_to_new_object(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-119: after rationale is defined with the default TBD, a new object
    created with no attribute values has none, and one created with status Draft has
    exactly that."""
    root = tmp_path
    attrs = (("type", "enum", False, ("Heading", "Requirement")), ("status", "enum", False, ("Draft", "Approved")))
    write_doc(root, "SRS", attrs=attrs, objects={"SRS-0": ({"type": "Requirement"}, "R")},
              outline=["SRS-0"], next_number=1)
    commit_all(root)
    srv = tools(mcp_at, root)
    res = srv.define_attribute("SRS", "rationale", "text", default_value="TBD")
    assert res.get("ok") is True, res
    assert read_obj(root, "SRS", "SRS-0")[0].get("rationale") == "TBD", "precondition: default set on SRS-0"
    bare = srv.new_object("SRS", description="x")
    assert bare.get("ok") is True, bare
    assert read_obj(root, "SRS", bare["new_id"])[0] == {}
    given = srv.new_object("SRS", description="y", attrs={"status": "Draft"})
    assert given.get("ok") is True, given
    assert read_obj(root, "SRS", given["new_id"])[0] == {"status": "Draft"}


def test_edit_srs_141_update_object_without_description_keeps_it(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-141: update_object with attributes and no description leaves the
    description unchanged; an empty description given on purpose blanks it."""
    root = basic_repo(tmp_path)
    commit_all(root)
    srv = tools(mcp_at, root)
    res = srv.update_object("SRS", "SRS-1", attrs={"status": "Approved"})
    assert res.get("ok") is True, res
    attrs, desc = read_obj(root, "SRS", "SRS-1")
    assert desc == "First" and attrs["status"] == "Approved"
    res = srv.update_object("SRS", "SRS-1", description="")
    assert res.get("ok") is True, res
    attrs, desc = read_obj(root, "SRS", "SRS-1")
    assert desc == "" and attrs["status"] == "Approved"


def test_edit_srs_146_copy_of_heading_takes_no_sub_objects(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-146: copying a heading with two sub-objects adds exactly one object
    to the outline, and it has no sub-objects."""
    root = copy_repo(tmp_path, commit_all)
    before_ids = all_object_ids(root, "SRS")
    before_outline = load_document(root / "SRS").iter_outline_ids()
    res = tools(mcp_at, root).copy_object("SRS", "SRS-0")
    assert res.get("ok") is True, res
    (new_id,) = res["new_ids"]
    assert all_object_ids(root, "SRS") - before_ids == {new_id}
    after_outline = load_document(root / "SRS").iter_outline_ids()
    assert sorted(after_outline) == sorted(before_outline + [new_id])
    assert sub_objects(root, "SRS", new_id) == []
    assert sub_objects(root, "SRS", "SRS-0") == ["SRS-1", "SRS-2"]


@pytest.mark.parametrize("copy_figure", [True, False])
def test_edit_srs_151_copy_figure_flag(tmp_path, mcp_at, commit_all, copy_figure):
    """MELLON-EDIT-SRS-151: copy_figure true gives the copy a figure byte-identical to the
    object's; false gives it none."""
    root = tmp_path / "repo"
    root.mkdir()
    copy_repo(root, commit_all)
    fig = with_figure(root, commit_all)
    res = tools(mcp_at, root).copy_object("SRS", "SRS-1", copy_figure=copy_figure)
    assert res.get("ok") is True, res
    (new_id,) = res["new_ids"]
    path = editor.figure_path(root / "SRS", new_id)
    if copy_figure:
        assert path.is_file() and path.read_bytes() == fig
    else:
        assert not path.exists()
    assert editor.figure_path(root / "SRS", "SRS-1").read_bytes() == fig


def test_edit_srs_152_copy_defaults_take_links_and_figure(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-152: copy_object without copy_links or copy_figure gives the copy the
    object's outgoing link and its figure."""
    root = tmp_path / "repo"
    root.mkdir()
    copy_repo(root, commit_all)
    fig = with_figure(root, commit_all)
    res = tools(mcp_at, root).copy_object("SRS", "SRS-1")
    assert res.get("ok") is True, res
    (new_id,) = res["new_ids"]
    assert editor.figure_path(root / "SRS", new_id).read_bytes() == fig
    links = load_document(root / "SRS").links.get(new_id, ())
    assert [(lk.target_doc, lk.target_id, lk.type) for lk in links] == [("SYS", "SYS-0", "satisfies")]


def test_edit_srs_162_mcp_refuses_delete_with_sub_object_even_forced(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-162 through delete_object: refused with force set, every file
    byte-identical."""
    root = delete_repo(tmp_path, commit_all)
    before = snapshot(root)
    res = mcp_refused(tools(mcp_at, root).delete_object, "SRS", "SRS-0", force=True)
    assert res.get("ok") is False, res
    assert changed(before, snapshot(root)) == []


def test_edit_srs_162_object_without_sub_objects_is_deleted(tmp_path, mcp_at, commit_all):
    """MELLON-EDIT-SRS-162 (other side): once the sub-object is gone, the former parent
    deletes."""
    root = delete_repo(tmp_path, commit_all)
    srv = tools(mcp_at, root)
    assert srv.delete_object("SRS", "SRS-1", force=True).get("ok") is True
    assert srv.delete_object("SRS", "SRS-0", force=True).get("ok") is True
    assert not obj_file(root, "SRS", "SRS-0").exists()


# MELLON-MCP-SRS blind tests
import contextlib  # noqa: E402

from tests.mcp_stdio_support import (  # noqa: E402
    SVG,
    TABLE,
    WINDOWS,
    child_env,
    clone_of,
    failed,
    fold,
    mcp_server,
    payload,
    rel_paths,
    requires_mellon_mcp,
    show,
    succeeded,
    write_calls,
    write_lf,
)


# ---------------------------------------------------------------------------
# Successful writes: MELLON-MCP-SRS-44, -67
# ---------------------------------------------------------------------------


WRITE_TOOLS = [name for name, _ in write_calls(Path("x.svg"))]


@requires_mellon_mcp
@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_mcp_srs_44_successful_write_returns_ok_affected_files_and_message(tool, write_run):
    """MELLON-MCP-SRS-44: when a write tool call succeeds, the MCP server shall return
    ok true, affected_files and message.

    One valid call of each write tool: ok is true, affected_files is a list of strings
    and message a non-empty string.
    """
    res = write_run["runs"][tool]["res"]
    p = payload(res)
    assert not res.isError and isinstance(p, dict), show(res)
    assert p.get("ok") is True, show(res)
    files = p.get("affected_files")
    assert isinstance(files, list) and all(isinstance(f, str) for f in files), f"affected_files: {files!r}"
    assert isinstance(p.get("message"), str) and p["message"].strip(), f"message: {p.get('message')!r}"


@requires_mellon_mcp
@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_mcp_srs_67_affected_files_are_exactly_the_files_changed(tool, write_run):
    """MELLON-MCP-SRS-67: when a write tool call succeeds, the MCP server shall list in
    affected_files exactly the files under the root, outside .git, that the call
    created, changed or deleted.

    Each file's SHA-256 before and after the call, ignored files included, compared with
    affected_files both ways.
    """
    run = write_run["runs"][tool]
    p = payload(run["res"])
    assert succeeded(run["res"]), f"precondition: the call failed: {show(run['res'])}"
    listed = rel_paths(write_run["root"], p["affected_files"])
    actual = {fold(c) for c in run["changed"]}
    assert listed == actual, (
        f"listed but not changed: {sorted(listed - actual)}; changed but not listed: {sorted(actual - listed)}"
    )


# ---------------------------------------------------------------------------
# Failed writes: MELLON-MCP-SRS-45, -52, -53, -54
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fail_server(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """One server on a rich req-test copy, for calls that fail."""
    base = tmp_path_factory.mktemp("fail")
    root = clone_of(rich_template, base / "repo")
    figure_source = base / "figure.svg"
    write_lf(figure_source, SVG)
    with mcp_server(
        ["--repo-root", root], env=child_env(ident_home), cwd=neutral_dir, stderr_path=base / "stderr.txt"
    ) as srv:
        yield {"client": srv, "root": root, "figure": figure_source}


def missing_doc_calls(figure: str) -> dict[str, dict]:
    """Each write tool but create_document, naming the document NOPE that doesn't exist."""
    d, o = "NOPE", "NOPE-1"
    return {
        "new_object": {"doc_id": d},
        "set_attribute": {"doc_id": d, "obj_id": o, "name": "status", "value": "Draft"},
        "edit_description": {"doc_id": d, "obj_id": o, "text": "x"},
        "update_object": {"doc_id": d, "obj_id": o, "description": "x"},
        "copy_object": {"doc_id": d, "obj_id": o},
        "bulk_set_attribute": {"doc_id": d, "obj_ids": [o], "name": "status", "value": "Draft"},
        "set_figure": {"doc_id": d, "obj_id": o, "source_path": figure},
        "clear_figure": {"doc_id": d, "obj_id": o},
        "set_table": {"doc_id": d, "obj_id": o, "table": TABLE},
        "clear_table": {"doc_id": d, "obj_id": o},
        "edit_metadata": {"doc_id": d, "field": "title", "value": "x"},
        "tag_issue": {"doc_id": d, "issue": "1"},
        "add_parent_doc": {"doc_id": d, "parent_doc": "VCU3-SyRS"},
        "remove_parent_doc": {"doc_id": d, "parent_doc": "VCU3-SyRS"},
        "add_attr_option": {"doc_id": d, "attr_name": "status", "option": "X"},
        "define_attribute": {"doc_id": d, "name": "blind-attr", "kind": "text"},
        "change_attribute_required": {"doc_id": d, "name": "status", "required": False},
        "delete_attribute": {"doc_id": d, "name": "status"},
        "remove_attr_option": {"doc_id": d, "attr_name": "status", "option": "Draft"},
        "manage_view": {"doc_id": d, "action": "create", "name": "View"},
        "move_object": {"doc_id": d, "obj_id": o, "to_top": True},
        "reorder_outline": {"doc_id": d, "ordering": [{"id": o, "children": []}]},
        "add_link": {"source_doc": d, "source_id": o, "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42",
                     "link_type": "satisfies"},
        "remove_link": {"source_doc": d, "source_id": o, "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42"},
        "delete_object": {"doc_id": d, "obj_id": o},
        "ack_suspects": {"doc_id": d},
    }


MISSING_DOC_TOOLS = list(missing_doc_calls("x.svg"))


@requires_mellon_mcp
@pytest.mark.parametrize("tool", MISSING_DOC_TOOLS)
def test_mcp_srs_45_failed_write_returns_ok_false_error_and_error_type(tool, fail_server):
    """MELLON-MCP-SRS-45: if a write tool call fails, the MCP server shall return ok false,
    an error message and an error_type.

    Each write tool given the document NOPE, which doesn't exist (create_document would
    create it, so it is left out).
    """
    args = missing_doc_calls(str(fail_server["figure"]))[tool]
    res = fail_server["client"].call(tool, args)
    p = payload(res)
    assert isinstance(p, dict), f"no JSON result: {show(res)}"
    assert p.get("ok") is False, show(res)
    assert isinstance(p.get("error"), str) and p["error"].strip(), show(res)
    assert isinstance(p.get("error_type"), str) and p["error_type"].strip(), show(res)


ALREADY_EXISTS = {
    "create_document": ("create_document", {"doc_id": "VCU3-SRS"}),
    "add_parent_doc": ("add_parent_doc", {"doc_id": "VCU3-SRS", "parent_doc": "VCU3-SyRS"}),
    "define_attribute": ("define_attribute", {"doc_id": "VCU3-SRS", "name": "status", "kind": "text"}),
    "add_attr_option": ("add_attr_option", {"doc_id": "VCU3-SRS", "attr_name": "status", "option": "Draft"}),
    "add_link_same_type": ("add_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-1",
                                        "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42",
                                        "link_type": "satisfies"}),
    "add_link_other_type": ("add_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-1",
                                         "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42",
                                         "link_type": "refines"}),
}


@requires_mellon_mcp
@pytest.mark.parametrize("case", list(ALREADY_EXISTS))
def test_mcp_srs_52_adding_an_existing_item_returns_already_exists(case, fail_server):
    """MELLON-MCP-SRS-52: if a write tool call asks to add a document, parent document,
    attribute or option that already exists, or a second link from one source to one
    target, the MCP server shall return error_type AlreadyExists.

    A second link to the same target counts whatever its link type.
    """
    tool, args = ALREADY_EXISTS[case]
    res = fail_server["client"].call(tool, args)
    p = payload(res)
    assert isinstance(p, dict) and p.get("ok") is False, f"precondition: the call didn't fail: {show(res)}"
    assert p.get("error_type") == "AlreadyExists", show(res)


EDITOR_ERRORS = {
    "missing_document": ("set_attribute", {"doc_id": "NOPE", "obj_id": "NOPE-1", "name": "status",
                                           "value": "Draft"}),
    "missing_object": ("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-999", "name": "status",
                                         "value": "Draft"}),
    "unknown_attribute": ("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1",
                                            "name": "no-such-attr", "value": "x"}),
    "option_not_defined": ("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1", "name": "status",
                                             "value": "Bogus"}),
    "link_target_document_not_a_parent": ("add_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-1",
                                                       "target_doc": "VCU3-STS", "target_id": "VCU3-STS-1",
                                                       "link_type": "satisfies"}),
    "attribute_kind_unknown": ("define_attribute", {"doc_id": "VCU3-SRS", "name": "blind-attr",
                                                    "kind": "bogus"}),
    "parent_not_declared": ("remove_parent_doc", {"doc_id": "VCU3-SRS", "parent_doc": "VCU3-STS"}),
    "link_missing": ("remove_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-10",
                                     "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-42"}),
    "figure_missing": ("clear_figure", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"}),
    "table_missing": ("clear_table", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"}),
}


NOT_FOUND = (
    "defect: MELLON-MCP-SRS-53: {what} returns error_type NotFound, not EditorError; NotFound is in "
    "no requirement's table (MELLON-MCP-SRS-55), so a person decides whether the code or the "
    "requirement changes"
)


def _editor_error_case(case: str):
    marks = {
        "parent_not_declared": pytest.mark.xfail(
            strict=True, reason=NOT_FOUND.format(what="remove_parent_doc with a parent that isn't declared")
        ),
        "link_missing": pytest.mark.xfail(
            strict=True, reason=NOT_FOUND.format(what="remove_link for a link that doesn't exist")
        ),
        "figure_missing": pytest.mark.xfail(
            strict=True, reason=NOT_FOUND.format(what="clear_figure on an object without a figure")
        ),
        "table_missing": pytest.mark.xfail(
            strict=True, reason=NOT_FOUND.format(what="clear_table on an object without a table")
        ),
    }
    return pytest.param(case, marks=marks[case]) if case in marks else case


@requires_mellon_mcp
@pytest.mark.parametrize("case", [_editor_error_case(c) for c in EDITOR_ERRORS])
def test_mcp_srs_53_missing_item_or_refused_value_returns_editor_error(case, fail_server):
    """MELLON-MCP-SRS-53: if a write tool call fails because a parameter names a missing
    item or gives a value that the tool or the document's attribute definitions refuse,
    the MCP server shall return error_type EditorError.

    Missing: a document, an object, an attribute, a parent declaration, a link, a figure,
    a table. Refused: an option that isn't defined, a link target document that isn't a
    parent, an unknown attribute kind.
    """
    tool, args = EDITOR_ERRORS[case]
    res = fail_server["client"].call(tool, args)
    p = payload(res)
    assert failed(res), f"precondition: the call didn't fail: {show(res)}"
    assert isinstance(p, dict) and p.get("error_type") == "EditorError", show(res)


@contextlib.contextmanager
def write_refused(path: Path):
    """Keep the operating system from replacing or writing ``path`` while the block runs."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        generic_read, share_read, open_existing, normal = 0x80000000, 0x1, 3, 0x80
        handle = kernel32.CreateFileW(str(path), generic_read, share_read, None, open_existing, normal, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            raise AssertionError(f"setup: can't hold {path} open: {ctypes.get_last_error()}")
        try:
            yield
        finally:
            kernel32.CloseHandle(handle)
    else:
        if os.geteuid() == 0:
            pytest.skip("root writes any file, so the case can't be built")
        folder_mode, file_mode = path.parent.stat().st_mode, path.stat().st_mode
        path.chmod(0o444)
        path.parent.chmod(0o555)
        try:
            yield
        finally:
            path.parent.chmod(folder_mode)
            path.chmod(file_mode)


@requires_mellon_mcp
def test_mcp_srs_54_operating_system_refusal_returns_os_error(tmp_path, rich_template, ident_home, neutral_dir):
    """MELLON-MCP-SRS-54: if a write tool call fails because the operating system refuses
    to read or write a file, the MCP server shall return error_type OSError.

    VCU3-SRS-1's object file is held open without write or delete sharing (Windows) or
    made read-only in a read-only folder (elsewhere) while set_attribute writes it.
    """
    root = clone_of(rich_template, tmp_path / "repo")
    target = root / "VCU3-SRS" / "objects" / "VCU3-SRS-1.xml"
    with mcp_server(
        ["--repo-root", root], env=child_env(ident_home), cwd=neutral_dir, stderr_path=tmp_path / "stderr.txt"
    ) as srv:
        srv.call("show_object", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1"})
        with write_refused(target):
            res = srv.call(
                "set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-1", "name": "status", "value": "Reviewed"}
            )
    p = payload(res)
    assert failed(res), f"precondition: the write didn't fail: {show(res)}"
    assert isinstance(p, dict) and p.get("error_type") == "OSError", show(res)


# MELLON-EDIT-SRS blind tests (176 and up)
from tests.edit_b_support import (  # noqa: E402
    DRAWIO_COMPRESSED,
    PNG_BYTES,
    SVG_A,
    SVG_B,
    _BAD_OUTLINES,
    _BAD_SPANS,
    _BAD_TILINGS,
    _DESCENDANT_MOVES,
    _broken_doc,
    _figure_doc,
    _nest_doc,
    _norm,
    _ordering,
    _sections_doc,
    _server,
    _table,
    _table_doc,
    changed_files,
    outline_tree,
    tree_hashes,
)


def mcp_children(nodes, ref_id):
    """Sub-object ids of `ref_id` in show_document's outline (dicts)."""
    for node in nodes:
        if node["id"] == ref_id:
            return [c["id"] for c in node.get("children", [])]
        found = mcp_children(node.get("children", []), ref_id)
        if found is not None:
            return found
    return None


@pytest.mark.parametrize("case", list(_DESCENDANT_MOVES))
def test_edit_srs_177_move_object_tool_refuses_move_under_or_beside_descendant(
        mcp_root, case):
    """MELLON-EDIT-SRS-177 through the move_object tool: placing H under itself,
    or under, before or after a descendant is refused and writes no file."""
    _nest_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().move_object("M", "M-0", **_DESCENDANT_MOVES[case])
    assert result["ok"] is False, f"move {case} was not refused: {result}"
    assert changed_files(before, tree_hashes(mcp_root)) == []


def _mcp_outline(doc_id: str) -> list:
    return _server().show_document(doc_id, include=["outline"])["outline"]


def test_edit_srs_187_move_object_under_a_parent(mcp_root):
    """MELLON-EDIT-SRS-187 (a): move_object with parent_id moves the object
    under the given parent object."""
    _sections_doc(mcp_root)
    result = _server().move_object("S", "S-6", parent_id="S-0")
    assert result["ok"] is True, result
    outline = _mcp_outline("S")
    assert "S-6" in mcp_children(outline, "S-0")
    assert [n["id"] for n in outline] == ["S-0", "S-3"]


def test_edit_srs_187_move_object_to_top(mcp_root):
    """MELLON-EDIT-SRS-187 (b): move_object with to_top moves a sub-object to
    the top level."""
    _sections_doc(mcp_root)
    result = _server().move_object("S", "S-4", to_top=True)
    assert result["ok"] is True, result
    outline = _mcp_outline("S")
    assert "S-4" in [n["id"] for n in outline]
    assert mcp_children(outline, "S-3") == ["S-5"]


def test_edit_srs_187_move_object_directly_before_object_in_other_section(mcp_root):
    """MELLON-EDIT-SRS-187 (c): move_object with before = S-4 (in section H2),
    no parent_id, places S-1 directly before S-4, under S-4's parent."""
    _sections_doc(mcp_root)
    result = _server().move_object("S", "S-1", before="S-4")
    assert result["ok"] is True, result
    outline = _mcp_outline("S")
    assert mcp_children(outline, "S-3") == ["S-1", "S-4", "S-5"]
    assert mcp_children(outline, "S-0") == ["S-2"]


def test_edit_srs_187_move_object_directly_after_object_in_other_section(mcp_root):
    """MELLON-EDIT-SRS-187 (d): move_object with after = S-5 (in section H2),
    no parent_id, places S-2 directly after S-5, under S-5's parent."""
    _sections_doc(mcp_root)
    result = _server().move_object("S", "S-2", after="S-5")
    assert result["ok"] is True, result
    outline = _mcp_outline("S")
    assert mcp_children(outline, "S-3") == ["S-4", "S-5", "S-2"]
    assert mcp_children(outline, "S-0") == ["S-1"]


def test_edit_srs_187_move_object_before_with_parent_named(mcp_root):
    """MELLON-EDIT-SRS-187 (c) with the target's parent named too: S-1 lands
    directly before S-5 under S-3."""
    _sections_doc(mcp_root)
    result = _server().move_object("S", "S-1", parent_id="S-3", before="S-5")
    assert result["ok"] is True, result
    assert mcp_children(_mcp_outline("S"), "S-3") == ["S-4", "S-1", "S-5"]


@pytest.mark.parametrize("case", list(_BAD_OUTLINES))
def test_edit_srs_190_reorder_outline_refuses_bad_outline_before_writing(
        mcp_root, case):
    """MELLON-EDIT-SRS-190 through reorder_outline: refused (ok false) and no
    file of the repository written."""
    _sections_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().reorder_outline("S", _ordering(_norm(_BAD_OUTLINES[case])))
    assert result["ok"] is False, result
    assert changed_files(before, tree_hashes(mcp_root)) == []


def test_edit_srs_190_control_full_outline_is_written(mcp_root):
    """MELLON-EDIT-SRS-189/-190 control: an outline naming each object exactly
    once is accepted and replaces the document's outline."""
    folder = _sections_doc(mcp_root)
    wanted = [("S-3", [("S-4", []), ("S-5", [])]), ("S-6", []),
              ("S-0", [("S-2", []), ("S-1", [])])]
    result = _server().reorder_outline("S", _ordering(wanted))
    assert result["ok"] is True, result
    assert outline_tree(folder / "S.xml") == wanted


def _table_dict(cols: list[str], rows: list[list[tuple]]) -> dict:
    return {
        "columns": [{"align": a} for a in cols],
        "rows": [{"cells": [{"text": t, "colspan": c, "rowspan": r}
                            for t, c, r in row]} for row in rows],
    }


# A 2x2 merged block plus a third column fills a 3x2 table exactly once.
_GOOD_TILING = (["left", "left", "left"],
                [[("block", 2, 2), ("x", 1, 1)], [("y", 1, 1)]])


@pytest.mark.parametrize("case", list(_BAD_TILINGS))
def test_edit_srs_205_set_table_tool_refuses_table_not_tiled_once(mcp_root, case):
    """MELLON-EDIT-SRS-205 through set_table: refused, no file written."""
    _table_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().set_table("T", "T-0", _table_dict(*_BAD_TILINGS[case]))
    assert result["ok"] is False, result
    assert changed_files(before, tree_hashes(mcp_root)) == []


def test_edit_srs_205_control_exact_tiling_with_merged_block_is_written(mcp_root):
    """MELLON-EDIT-SRS-205, other side: a 2x2 merged block beside a third
    column covers each position once and is written."""
    _table_doc(mcp_root)
    result = _server().set_table("T", "T-0", _table_dict(*_GOOD_TILING))
    assert result["ok"] is True, result
    from mellon.loader import load_document

    table = load_document(mcp_root / "T").objects["T-0"].table
    assert table is not None
    assert [(c.text, c.colspan, c.rowspan) for c in table.rows[0].cells] == [
        ("block", 2, 2), ("x", 1, 1)]
    assert [(c.text, c.colspan, c.rowspan) for c in table.rows[1].cells] == [
        ("y", 1, 1)]


@pytest.mark.parametrize("align", ["justify", "", "Left", "middle"],
                         ids=["justify", "empty", "Left", "middle"])
def test_edit_srs_206_refuses_column_alignment_outside_left_center_right(
        mcp_root, align):
    """MELLON-EDIT-SRS-206: a column alignment other than left, center and
    right is refused (set_table and library), object file unchanged."""
    from mellon.editor import EditorError, set_table

    folder = _table_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().set_table(
        "T", "T-0", _table_dict(["left", align], [[("a", 1, 1), ("b", 1, 1)]]))
    assert result["ok"] is False, result
    with pytest.raises((EditorError, ValueError)):
        set_table(folder, "T-0",
                  _table(["left", align], [[("a", 1, 1), ("b", 1, 1)]]))
    assert changed_files(before, tree_hashes(mcp_root)) == []


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_edit_srs_206_accepts_left_center_right_and_reads_them_back(
        mcp_root, align):
    """MELLON-EDIT-SRS-206, other side: left, center and right are written and
    read back as given."""
    from mellon.loader import load_document

    _table_doc(mcp_root)
    result = _server().set_table(
        "T", "T-0", _table_dict([align, "left"], [[("a", 1, 1), ("b", 1, 1)]]))
    assert result["ok"] is True, result
    table = load_document(mcp_root / "T").objects["T-0"].table
    assert [c.align for c in table.columns] == [align, "left"]


@pytest.mark.parametrize("case", list(_BAD_SPANS))
def test_edit_srs_207_set_table_tool_refuses_span_below_one(mcp_root, case):
    """MELLON-EDIT-SRS-207 through set_table: refused, no file written."""
    colspan, rowspan = _BAD_SPANS[case]
    _table_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().set_table(
        "T", "T-0", _table_dict(["left"], [[("a", colspan, rowspan)]]))
    assert result["ok"] is False, result
    assert changed_files(before, tree_hashes(mcp_root)) == []


def test_edit_srs_207_control_span_of_one_is_written(mcp_root):
    """MELLON-EDIT-SRS-207, other side: spans of exactly 1 are written."""
    from mellon.loader import load_document

    _table_doc(mcp_root)
    result = _server().set_table("T", "T-0",
                                 _table_dict(["left"], [[("a", 1, 1)]]))
    assert result["ok"] is True, result
    cell = load_document(mcp_root / "T").objects["T-0"].table.rows[0].cells[0]
    assert (cell.text, cell.colspan, cell.rowspan) == ("a", 1, 1)


@pytest.mark.parametrize("content", ["png", "compressed-drawio"])
def test_edit_srs_233_set_figure_tool_refuses_non_svg_named_svg(mcp_root, content):
    """MELLON-EDIT-SRS-233 through set_figure: refused (ok false), and the
    existing figure file's bytes are unchanged."""
    folder = _figure_doc(mcp_root)
    (mcp_root / "x.svg").write_bytes(
        PNG_BYTES if content == "png" else DRAWIO_COMPRESSED)
    before = tree_hashes(mcp_root)
    result = _server().set_figure("F", "F-0", "x.svg")
    assert result["ok"] is False, result
    assert (folder / "figures" / "F-0.svg").read_bytes() == SVG_A
    assert changed_files(before, tree_hashes(mcp_root)) == []


def test_edit_srs_233_control_well_formed_svg_replaces_figure(mcp_root):
    """MELLON-EDIT-SRS-232/-233 control: a well-formed SVG given by a path
    relative to the repository root replaces the figure with its bytes."""
    folder = _figure_doc(mcp_root)
    (mcp_root / "x.svg").write_bytes(SVG_B)
    result = _server().set_figure("F", "F-0", "x.svg")
    assert result["ok"] is True, result
    assert (folder / "figures" / "F-0.svg").read_bytes() == SVG_B


def test_edit_srs_190_refusal_writes_nothing_beside_document_with_load_error(mcp_root):
    """MELLON-EDIT-SRS-190 with a broken repository (T10): another document has
    a load error; reorder_outline naming S-1 twice is still refused and no
    file of the repository is written."""
    _sections_doc(mcp_root)
    _broken_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().reorder_outline("S", _ordering(_norm(_BAD_OUTLINES["named-twice"])))
    assert result["ok"] is False, result
    assert changed_files(before, tree_hashes(mcp_root)) == []


def test_edit_srs_177_refusal_beside_document_with_load_error(mcp_root):
    """MELLON-EDIT-SRS-177 with a broken repository (T10): a move of H under
    its descendant is still refused, with no file written, while another
    document has a load error."""
    _nest_doc(mcp_root)
    _broken_doc(mcp_root)
    before = tree_hashes(mcp_root)
    result = _server().move_object("M", "M-0", parent_id="M-2")
    assert result["ok"] is False, result
    assert changed_files(before, tree_hashes(mcp_root)) == []


# MELLON-TRACE-SRS blind tests
import subprocess  # noqa: E402
import sys  # noqa: E402

from tests.trace_support import (  # noqa: E402
    MISSING_TARGETS,
    Repo,
    _make_missing,
    _repo_with_never_committed_target,
    _suspects_set,
    _uncommitted_parent_5,
    all_acks,
    basic_repo as trace_basic_repo,
    call,
    disk_links,
    refused,
    snapshot as trace_snapshot,
)


def git_state(r: Repo) -> dict:
    """What a dry run must leave alone in git: status, index bytes, refs."""
    return {
        "status": dict(r.git.status()),
        "index": (r.root / ".git" / "index").read_bytes(),
        "refs": {n: str(r.git.references[n].target) for n in r.git.references},
    }


def _parse_tool_result(res):
    if res.isError:
        return {"ok": False, "raised": " ".join(getattr(c, "text", "") for c in res.content)}
    sc = res.structuredContent
    if sc is not None:
        return sc["result"] if set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def stdio_calls(root, calls, env=None) -> list:
    """Start mellon-mcp over stdio on `root` and make `calls` [(tool, args)] in order."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    exe = shutil.which("mellon-mcp", path=str(Path(sys.executable).parent))
    assert exe, "mellon-mcp is not installed beside the test interpreter"

    async def run():
        params = StdioServerParameters(command=exe, args=["--repo-root", str(root)],
                                       env=dict(os.environ) if env is None else env)
        out = []
        async with stdio_client(params) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                for name, args in calls:
                    out.append(_parse_tool_result(await s.call_tool(name, args)))
        return out

    return anyio.run(run)


@pytest.mark.parametrize("layer", ["mcp"])
def test_trace_srs_4_link_to_target_with_no_last_change_gets_no_acknowledged_commit(tmp_path, mcp, layer):
    """MELLON-TRACE-SRS-4: If a new link's target object has no last change, Mellon shall create the
    link with no acknowledged commit. Condition: the target's object file is in the working copy and
    was never committed. Other side: a committed target gets its last change (MELLON-TRACE-SRS-3)."""
    r = trace_basic_repo(tmp_path)
    _uncommitted_parent_5(r)
    if layer == "library":
        editor.add_link(r.root / "CHILD", "CHILD-1", "PARENT", "PARENT-5", "satisfies")
        editor.add_link(r.root / "CHILD", "CHILD-1", "PARENT", "PARENT-1", "satisfies")
    else:
        s = mcp.serve(r.root)
        res = s.add_link("CHILD", "CHILD-1", "PARENT", "PARENT-5", "satisfies")
        assert res.get("ok") is True, f"add_link refused: {res}"
        assert res.get("ack") is None, f"add_link reports ack {res.get('ack')!r} for a never-committed target"
        assert s.add_link("CHILD", "CHILD-1", "PARENT", "PARENT-1", "satisfies").get("ok") is True
    on_disk = disk_links(r.root, "CHILD", "CHILD-1")
    assert on_disk[("PARENT", "PARENT-5")] == ("satisfies", None), f"link to PARENT-5 on disk: {on_disk}"
    assert on_disk[("PARENT", "PARENT-1")] == ("satisfies", r.c0[:7]), f"link to PARENT-1 on disk: {on_disk}"


@pytest.mark.parametrize("target", list(MISSING_TARGETS))
@pytest.mark.parametrize("layer", ["mcp add_link", "mcp update_object"])
def test_trace_srs_6_link_to_missing_target_is_refused(tmp_path, mcp, layer, target):
    """MELLON-TRACE-SRS-6: If a new link's target object doesn't exist in the working copy, Mellon
    shall refuse to create the link. Condition: target never existed, or committed and deleted from
    the working copy. Expected: refused, and CHILD's files unchanged (no links file written)."""
    r = trace_basic_repo(tmp_path)
    _make_missing(r, target)
    before = trace_snapshot(r.root)
    if layer == "library":
        try:
            editor.add_link(r.root / "CHILD", "CHILD-1", "PARENT", target, "satisfies")
            result = {"ok": True}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "raised": repr(e)}
    elif layer == "service":
        try:
            res = DocumentService(r.root).add_link("CHILD", "CHILD-1", "PARENT", target, "satisfies")
            result = {"ok": getattr(res, "ok", True)}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "raised": repr(e)}
    elif layer == "mcp add_link":
        result = call(mcp.serve(r.root).add_link, "CHILD", "CHILD-1", "PARENT", target, "satisfies")
    else:
        result = call(mcp.serve(r.root).update_object, "CHILD", "CHILD-1",
                      add_links=[{"target_doc": "PARENT", "target_id": target, "type": "satisfies"}])
    assert refused(result), f"link CHILD-1 -> PARENT/{target} ({MISSING_TARGETS[target]}) was created: {result}"
    assert trace_snapshot(r.root) == before, "a refused link changed files"


def test_trace_srs_6_link_to_missing_target_over_stdio_is_refused(tmp_path):
    """MELLON-TRACE-SRS-6: If a new link's target object doesn't exist in the working copy, Mellon
    shall refuse to create the link. Condition: add_link to PARENT-99 through the MCP server over stdio."""
    r = trace_basic_repo(tmp_path)
    before = trace_snapshot(r.root)
    (res,) = stdio_calls(r.root, [("add_link", {
        "source_doc": "CHILD", "source_id": "CHILD-1", "target_doc": "PARENT",
        "target_id": "PARENT-99", "link_type": "satisfies"})])
    assert refused(res), f"add_link to a missing target answered {res}"
    assert trace_snapshot(r.root) == before, "a refused link changed files"


@pytest.mark.parametrize("layer", ["mcp add_link", "mcp new_object", "mcp update_object"])
def test_trace_srs_7_second_link_to_same_target_with_other_type_is_refused(tmp_path, mcp, layer):
    """MELLON-TRACE-SRS-7: If a new link's source object already has a link to the target object,
    Mellon shall refuse to create the new link, whatever its link type. Condition: CHILD-1 satisfies
    PARENT-1; add a verifies link between the same pair (new_object: both in one call)."""
    r = trace_basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7])])
    r.commit("link")
    before = trace_snapshot(r.root)
    if layer == "library":
        try:
            editor.add_link(r.root / "CHILD", "CHILD-1", "PARENT", "PARENT-1", "verifies")
            result = {"ok": True}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "raised": repr(e)}
    elif layer == "mcp add_link":
        result = call(mcp.serve(r.root).add_link, "CHILD", "CHILD-1", "PARENT", "PARENT-1", "verifies")
    elif layer == "mcp new_object":
        result = call(mcp.serve(r.root).new_object, "CHILD", description="new", attrs={"type": "Requirement"},
                      links=[{"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"},
                             {"target_doc": "PARENT", "target_id": "PARENT-1", "type": "verifies"}])
    else:
        result = call(mcp.serve(r.root).update_object, "CHILD", "CHILD-1",
                      add_links=[{"target_doc": "PARENT", "target_id": "PARENT-1", "type": "verifies"}])
    assert refused(result), f"a second link between one pair of objects was created: {result}"
    assert trace_snapshot(r.root) == before, "a refused link changed files"


def test_trace_srs_7_link_to_another_target_is_not_refused(tmp_path, mcp):
    """MELLON-TRACE-SRS-7 (other side): the refusal is only for a source that already links to the
    target. Condition: CHILD-1 satisfies PARENT-1; a verifies link from CHILD-1 to PARENT-2 is created."""
    r = trace_basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7])])
    r.commit("link")
    res = mcp.serve(r.root).add_link("CHILD", "CHILD-1", "PARENT", "PARENT-2", "verifies")
    assert res.get("ok") is True, res
    assert set(disk_links(r.root, "CHILD", "CHILD-1")) == {("PARENT", "PARENT-1"), ("PARENT", "PARENT-2")}


@pytest.mark.parametrize("link_type", ["derives from", "ÄÖ-trace", "x"])
def test_trace_srs_14_mcp_accepts_any_link_type_with_a_non_whitespace_character(tmp_path, mcp, link_type):
    """MELLON-TRACE-SRS-14: The MCP server shall accept as a link type any text that holds a
    character other than whitespace. Condition: add_link with a free-text, non-ASCII or one-character
    link type. Expected: accepted, and the link has exactly that link type on disk and in show_object."""
    r = trace_basic_repo(tmp_path)
    s = mcp.serve(r.root)
    res = s.add_link("CHILD", "CHILD-1", "PARENT", "PARENT-1", link_type)
    assert res.get("ok") is True, f"link type {link_type!r} refused: {res}"
    assert disk_links(r.root, "CHILD", "CHILD-1") == {("PARENT", "PARENT-1"): (link_type, r.c0[:7])}
    types = [e["link"]["type"] for e in s.show_object("CHILD", "CHILD-1")["outgoing"]]
    assert types == [link_type], f"show_object reports {types}"


def test_trace_srs_14_mcp_accepts_link_type_with_surrounding_spaces(tmp_path, mcp):
    """MELLON-TRACE-SRS-14: The MCP server shall accept as a link type any text that holds a
    character other than whitespace. Condition: ' verifies ' (spaces around it). Expected: accepted.
    Whether the spaces are kept is MELLON-TRACE-SRS-15 (Proposed), so it isn't checked here."""
    r = trace_basic_repo(tmp_path)
    res = mcp.serve(r.root).add_link("CHILD", "CHILD-1", "PARENT", "PARENT-1", " verifies ")
    assert res.get("ok") is True, f"link type ' verifies ' refused: {res}"
    assert ("PARENT", "PARENT-1") in disk_links(r.root, "CHILD", "CHILD-1")


def test_trace_srs_19_add_link_creates_link_with_given_type(tmp_path, mcp):
    """MELLON-TRACE-SRS-19: The MCP server shall have an add_link tool that creates a link given the
    source object, the target object and the link type. Condition: add_link CHILD-1 -> PARENT-0
    'refines'. Expected: show_object lists exactly that outgoing link, with link type refines."""
    r = trace_basic_repo(tmp_path)
    s = mcp.serve(r.root)
    assert s.add_link("CHILD", "CHILD-1", "PARENT", "PARENT-0", "refines").get("ok") is True
    out = [(e["link"]["target_doc"], e["link"]["target_id"], e["link"]["type"])
           for e in s.show_object("CHILD", "CHILD-1")["outgoing"]]
    assert out == [("PARENT", "PARENT-0", "refines")], f"outgoing links of CHILD-1: {out}"


def test_trace_srs_24_new_object_with_refused_link_changes_no_file(tmp_path, mcp):
    """MELLON-TRACE-SRS-24: If a new_object or update_object call gives a link that Mellon refuses,
    the MCP server shall change no file. Condition: new_object with attributes and a link to a
    missing target. Expected: refused; every file in the repository byte-identical, none added."""
    r = trace_basic_repo(tmp_path)
    before = trace_snapshot(r.root)
    res = call(mcp.serve(r.root).new_object, "CHILD", description="new one", attrs={"type": "Requirement"},
               links=[{"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"},
                      {"target_doc": "PARENT", "target_id": "PARENT-99", "type": "satisfies"}])
    assert refused(res), f"new_object with a link to a missing target answered {res}"
    after = trace_snapshot(r.root)
    assert after == before, f"files changed: {sorted(set(after) ^ set(before)) or [k for k in after if after[k] != before.get(k)]}"


def test_trace_srs_24_update_object_with_refused_link_changes_no_file(tmp_path, mcp):
    """MELLON-TRACE-SRS-24: If a new_object or update_object call gives a link that Mellon refuses,
    the MCP server shall change no file. Condition: update_object changing the description and an
    attribute and adding a link that already exists. Expected: refused; every file byte-identical."""
    r = trace_basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7])])
    r.commit("link")
    before = trace_snapshot(r.root)
    res = call(mcp.serve(r.root).update_object, "CHILD", "CHILD-1", description="changed text",
               attrs={"status": "Approved"},
               add_links=[{"target_doc": "PARENT", "target_id": "PARENT-1", "type": "satisfies"}])
    assert refused(res), f"update_object adding an existing link answered {res}"
    after = trace_snapshot(r.root)
    assert after == before, f"files changed: {[k for k in after if after[k] != before.get(k)]}"


# ===========================================================================
# Acknowledging (MELLON-TRACE-SRS-131, -139, -140, -141, -142)
# ===========================================================================


def _key(src, tid, tdoc="PARENT"):
    return {"source_id": src, "target_doc": tdoc, "target_id": tid}


def _skipped_keys(res) -> list:
    return [(e["key"]["source_id"], e["key"]["target_doc"], e["key"]["target_id"]) for e in res.get("skipped", [])]


def test_trace_srs_131_ack_suspects_skips_a_broken_link(tmp_path, mcp):
    """MELLON-TRACE-SRS-131: If a link is broken, Mellon shall refuse to acknowledge the link.
    Condition: ack_suspects with link_keys naming CHILD-1's link to the missing PARENT-9.
    Expected: listed as skipped, nothing acknowledged, every file byte-identical."""
    r = trace_basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-9", "satisfies", None), ("PARENT", "PARENT-1", "satisfies", None)])
    r.commit("links")
    before = trace_snapshot(r.root)
    res = mcp.serve(r.root).ack_suspects("CHILD", link_keys=[_key("CHILD-1", "PARENT-9")])
    assert res.get("acknowledged") == [], res
    assert _skipped_keys(res) == [("CHILD-1", "PARENT", "PARENT-9")], res
    assert trace_snapshot(r.root) == before, "acknowledging a broken link changed files"


def test_trace_srs_131_ack_suspects_without_keys_leaves_a_broken_link_alone(tmp_path, mcp):
    """MELLON-TRACE-SRS-131: If a link is broken, Mellon shall refuse to acknowledge the link.
    Condition: ack_suspects with no link_keys on a document with a broken and a suspect link.
    Expected: the broken link keeps no acknowledged commit."""
    r = trace_basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-9", "satisfies", None), ("PARENT", "PARENT-1", "satisfies", None)])
    r.commit("links")
    mcp.serve(r.root).ack_suspects("CHILD")
    assert disk_links(r.root, "CHILD", "CHILD-1")[("PARENT", "PARENT-9")] == ("satisfies", None)


def _prefix_case_repo(tmp_path, name="repo") -> Repo:
    r = Repo(tmp_path / name)
    r.add_doc("PARENT", {"PARENT-1": "p1", "PARENT-10": "p10"})
    r.add_doc("DOC", {"DOC-1": "d1", "DOC-10": "d10"}, parents=["PARENT"])
    r.c0 = r.commit("seed")
    for src in ("DOC-1", "DOC-10"):
        r.set_links("DOC", src, [("PARENT", "PARENT-1", "satisfies", None), ("PARENT", "PARENT-10", "satisfies", None)])
    r.commit("links")
    return r


ALL_PREFIX_LINKS = [("DOC-1", "PARENT-1"), ("DOC-1", "PARENT-10"), ("DOC-10", "PARENT-1"), ("DOC-10", "PARENT-10")]


def _acked_links(before, after) -> set:
    return {(k[1], k[3]) for k, v in after.items() if v and before.get(k) != v}


def test_trace_srs_139_link_keys_acknowledge_exactly_the_named_link(tmp_path, mcp):
    """MELLON-TRACE-SRS-139: When an ack_suspects call gives link_keys, the MCP server shall
    acknowledge only the suspect links that the link keys name. Condition: suspect links DOC-1 and
    DOC-10 to PARENT-1 and PARENT-10 (shared prefixes); the key names DOC-1 -> PARENT-1 (CF-T34)."""
    r = _prefix_case_repo(tmp_path)
    before = all_acks(r.root)
    mcp.serve(r.root).ack_suspects("DOC", link_keys=[_key("DOC-1", "PARENT-1")])
    assert _acked_links(before, all_acks(r.root)) == {("DOC-1", "PARENT-1")}


def test_trace_srs_139_link_keys_differing_in_letter_case_acknowledge_nothing(tmp_path, mcp):
    """MELLON-TRACE-SRS-139: ... acknowledge only the suspect links that the link keys name.
    Condition: keys that differ from DOC-1 -> PARENT/PARENT-1 only in letter case (source, target
    document, target ID). Expected: nothing acknowledged, every file byte-identical (CF-T34)."""
    r = _prefix_case_repo(tmp_path)
    before = trace_snapshot(r.root)
    res = mcp.serve(r.root).ack_suspects("DOC", link_keys=[
        _key("doc-1", "PARENT-1"), _key("DOC-1", "PARENT-1", tdoc="parent"), _key("DOC-1", "parent-1")])
    assert res.get("acknowledged") == [], res
    assert trace_snapshot(r.root) == before


def test_trace_srs_139_each_subset_of_link_keys_acknowledges_exactly_that_subset(tmp_path, mcp):
    """MELLON-TRACE-SRS-139: ... acknowledge only the suspect links that the link keys name.
    Condition (T11): every one of the 16 subsets of the four suspect links DOC-1/DOC-10 ->
    PARENT-1/PARENT-10 given as link_keys. Expected: exactly the subset is acknowledged."""
    template = _prefix_case_repo(tmp_path, "template")
    for n in range(16):
        subset = {lk for i, lk in enumerate(ALL_PREFIX_LINKS) if n >> i & 1}
        root = tmp_path / f"r{n}"
        shutil.copytree(template.root, root)
        before = all_acks(root)
        mcp.serve(root).ack_suspects("DOC", link_keys=[_key(s, t) for s, t in sorted(subset)])
        assert _acked_links(before, all_acks(root)) == subset, f"subset {sorted(subset)}"


def test_trace_srs_139_link_keys_over_stdio_without_git_user_name(tmp_path):
    """MELLON-TRACE-SRS-139: ... acknowledge only the suspect links that the link keys name.
    Condition (T10, stdio): the MCP server over stdio in an environment with no git user.name or
    user.email; the key names DOC-1 -> PARENT-1. Expected: only that link is acknowledged."""
    r = _prefix_case_repo(tmp_path)
    empty = tmp_path / "no-identity"
    empty.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR", "GIT_COMMITTER", "GIT_CONFIG"))}
    drive, rest = os.path.splitdrive(str(empty))
    env.update({"HOME": str(empty), "USERPROFILE": str(empty), "HOMEDRIVE": drive, "HOMEPATH": rest,
                "XDG_CONFIG_HOME": str(empty), "GIT_CONFIG_NOSYSTEM": "1"})
    probe = subprocess.run(
        [sys.executable, "-c", "import pygit2,sys; c=pygit2.Repository(sys.argv[1]).config; "
         "print('user.name' in c, 'user.email' in c)", str(r.root)],
        env=env, capture_output=True, text=True, check=True).stdout.split()
    assert probe == ["False", "False"], f"precondition: identity still set in the server's environment: {probe}"
    before = all_acks(r.root)
    (res,) = stdio_calls(r.root, [("ack_suspects", {"doc_id": "DOC", "link_keys": [_key("DOC-1", "PARENT-1")]})], env=env)
    assert not refused(res), res
    assert _acked_links(before, all_acks(r.root)) == {("DOC-1", "PARENT-1")}


def test_trace_srs_140_each_key_that_names_no_suspect_link_is_skipped_with_a_reason(tmp_path, mcp):
    """MELLON-TRACE-SRS-140: When an ack_suspects call names a link that doesn't exist or isn't
    suspect, the MCP server shall report the link as skipped, with the reason. Condition: keys naming
    an OK link, a link that doesn't exist and a key with a typo in the target ID. Expected: each
    skipped with a reason; the OK link's reason differs from the missing link's; no file changes."""
    r = trace_basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", r.c0[:7])])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-2", "satisfies", None)])
    r.commit("links")
    before = trace_snapshot(r.root)
    keys = [_key("CHILD-1", "PARENT-1"), _key("CHILD-1", "PARENT-3"), _key("CHILD-2", "PARETN-2")]
    res = mcp.serve(r.root).ack_suspects("CHILD", link_keys=keys)
    reasons = {(e["key"]["source_id"], e["key"]["target_id"]): e.get("reason") for e in res.get("skipped", [])}
    assert set(reasons) == {("CHILD-1", "PARENT-1"), ("CHILD-1", "PARENT-3"), ("CHILD-2", "PARETN-2")}, res
    assert all(isinstance(v, str) and v.strip() for v in reasons.values()), reasons
    assert reasons[("CHILD-1", "PARENT-1")] != reasons[("CHILD-1", "PARENT-3")], reasons
    assert res.get("acknowledged") == [], res
    assert trace_snapshot(r.root) == before


def test_ack_suspects_skips_a_link_whose_target_was_never_committed(tmp_path, mcp):
    """A suspect link whose target was never committed has no last change to
    record (MELLON-TRACE-SRS-45, -130): ack_suspects naming it skips it, with
    a reason that says so, and changes no file (open question Q18)."""
    r = _repo_with_never_committed_target(tmp_path)
    before = trace_snapshot(r.root)
    res = mcp.serve(r.root).ack_suspects("CHILD", link_keys=[_key("CHILD-1", "PARENT-43")])
    assert res.get("ok") is True, res
    assert res.get("acknowledged") == [], res
    (skipped,) = res.get("skipped", [])
    assert "never committed" in skipped["reason"], skipped
    assert trace_snapshot(r.root) == before


def _dry_run_repo(tmp_path, name="repo") -> Repo:
    r = trace_basic_repo(tmp_path, name)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", None), ("PARENT", "PARENT-2", "satisfies", None)])
    r.set_links("CHILD", "CHILD-2", [("PARENT", "PARENT-3", "satisfies", r.c0[:7])])
    r.set_links("CHILD", "CHILD-3", [("PARENT", "PARENT-9", "satisfies", None)])
    r.commit("links")
    return r


DRY_RUN_KEYS = {
    "no-link-keys": None,
    "some-link-keys": [_key("CHILD-1", "PARENT-2"), _key("CHILD-2", "PARENT-3"), _key("CHILD-3", "PARENT-9")],
}


@pytest.mark.parametrize("keys", list(DRY_RUN_KEYS))
def test_trace_srs_141_dry_run_lists_the_links_the_real_call_acknowledges(tmp_path, mcp, keys):
    """MELLON-TRACE-SRS-141: When an ack_suspects call asks for a dry run, the MCP server shall return
    the links that the call acknowledges when made without a dry run. Condition: suspect, OK and
    broken links; without link_keys and with a subset. Expected: the dry run's links equal the links
    whose acknowledged commit the real call writes."""
    r = _dry_run_repo(tmp_path)
    s = mcp.serve(r.root)
    link_keys = DRY_RUN_KEYS[keys]
    dry = s.ack_suspects("CHILD", dry_run=True, link_keys=link_keys)
    would = {(e["source_id"], e["link"]["target_id"]) for e in dry["would_acknowledge"]}
    before = all_acks(r.root)
    s.ack_suspects("CHILD", link_keys=link_keys)
    assert _acked_links(before, all_acks(r.root)) == would


def test_trace_srs_142_dry_run_changes_no_file(tmp_path, mcp):
    """MELLON-TRACE-SRS-142: When an ack_suspects call asks for a dry run, the MCP server shall change
    no file. Condition: dry runs without and with link_keys on a document with suspect links.
    Expected: every working-copy file byte-identical, git status, index and refs unchanged, and the
    same suspect links reported afterwards. (Mellon's own cache file in .git is not compared.)"""
    r = _dry_run_repo(tmp_path)
    s = mcp.serve(r.root)
    before_suspects = _suspects_set(s.suspects("CHILD"))
    files, git = trace_snapshot(r.root), git_state(r)
    s.ack_suspects("CHILD", dry_run=True)
    s.ack_suspects("CHILD", dry_run=True, link_keys=[_key("CHILD-1", "PARENT-1")])
    assert trace_snapshot(r.root) == files, "a dry run changed working-copy files"
    assert git_state(r) == git, "a dry run changed git status, the index or refs"
    mcp.restart()
    assert _suspects_set(s.suspects("CHILD")) == before_suspects


# MELLON-VER-SRS blind tests
from tests.ver_support import (  # noqa: E402
    ANN,
    IDENTITY,
    _assert_hooks_live,
    _assert_identity,
    _attr,
    _changed,
    _description,
    _git,
    _head,
    _install_hooks,
    _make_repo,
    _mcp_root,
    _obj,
    _parents,
    _status,
    _stdio,
    _tree,
    _work_files,
    _write,
)


def _commit_paths(root: Path, rev: str = "HEAD") -> set[str]:
    out = _git(root, "diff-tree", "-r", "--root", "--no-commit-id", "--name-only", rev).stdout
    return {line for line in out.splitlines() if line}


def test_ver_srs_107_commit_message_commits_only_the_files_the_call_changed(mcp_repo):
    """MELLON-VER-SRS-107: When a write tool is called with `commit_message`,
    the MCP server commits the files that the call changed and no other path.
    Another uncommitted change stays uncommitted with its edit."""
    root = _mcp_root(mcp_repo)
    server.edit_description("CHILD", "CHILD-1", "An edit left uncommitted.")
    _write(root / "PARENT/objects/PARENT-2.xml", _obj("PARENT-2", "Changed by hand."))
    before = _work_files(root)

    res = server.set_attribute("CHILD", "CHILD-0", "status", "Approved", commit_message="Approve CHILD-0")

    assert "commit" in res, f"the write made no commit: {res}"
    by_call = _changed(before, _work_files(root))
    assert by_call == {"CHILD/objects/CHILD-0.xml"}
    assert _commit_paths(root) == by_call, "the commit holds a path the call didn't change"
    assert _description((root / "CHILD/objects/CHILD-1.xml").read_bytes()) == "An edit left uncommitted."
    still = _status(root)
    assert any(line.endswith("CHILD/objects/CHILD-1.xml") for line in still), still
    assert any(line.endswith("PARENT/objects/PARENT-2.xml") for line in still), still


def test_ver_srs_107_commit_message_commits_files_the_call_changed_in_other_documents(mcp_repo):
    """MELLON-VER-SRS-107: the files that the call changed, in whichever
    document: deleting PARENT-1 strips CHILD-0's link to it, and the commit
    holds exactly the files the call wrote or deleted."""
    root = _mcp_root(mcp_repo)
    _write(root / "CHILD/objects/CHILD-1.xml", _obj("CHILD-1", "Unrelated uncommitted edit."))
    before = _work_files(root)

    res = server.delete_object("PARENT", "PARENT-1", force=True, commit_message="Delete PARENT-1")

    assert "commit" in res, f"the delete made no commit: {res}"
    by_call = _changed(before, _work_files(root))
    assert "PARENT/objects/PARENT-1.xml" in by_call
    assert "CHILD/links/CHILD-0.xml" in by_call, "the delete didn't strip the incoming link"
    assert _commit_paths(root) == by_call
    assert any(line.endswith("CHILD/objects/CHILD-1.xml") for line in _status(root))


def test_ver_srs_115_commit_message_commit_records_git_identity_as_author_and_committer(mcp_repo):
    """MELLON-VER-SRS-115: a write tool's `commit_message` commit records git's identity."""
    root = _mcp_root(mcp_repo, ANN)

    res = server.set_attribute("CHILD", "CHILD-0", "status", "Approved", commit_message="Approve as Ann")

    assert "commit" in res, res
    _assert_identity(root, *ANN)


def test_ver_srs_131_commit_message_commit_runs_no_git_hook(mcp_repo, tmp_path_factory):
    """MELLON-VER-SRS-131: a write tool's `commit_message` commit runs no git hook."""
    root = _mcp_root(mcp_repo)
    markers = tmp_path_factory.mktemp("hooks") / "markers"
    _install_hooks(root, markers)
    old_head = _head(root)

    res = server.set_attribute("CHILD", "CHILD-0", "status", "Approved", commit_message="Approve past hooks")

    assert "commit" in res, res
    assert _parents(root) == [old_head]
    assert sorted(p.name for p in markers.iterdir()) == [], "a git hook ran"
    _assert_hooks_live(root, markers)


# ==========================================================================
# Commits over MCP
# ==========================================================================

ver_WRITE_TOOLS = [
    "new_object", "set_attribute", "edit_description", "update_object", "copy_object",
    "bulk_set_attribute", "set_figure", "clear_figure", "set_table", "clear_table",
    "edit_metadata", "tag_issue", "add_parent_doc", "remove_parent_doc", "add_attr_option",
    "define_attribute", "change_attribute_required", "delete_attribute", "remove_attr_option",
    "manage_view", "create_document", "move_object", "reorder_outline", "add_link",
    "remove_link", "delete_object", "ack_suspects",
]


def _accepts_string(schema: dict) -> bool:
    if schema.get("type") == "string":
        return True
    return any(s.get("type") == "string" for s in schema.get("anyOf", []))


def test_ver_srs_171_write_tools_take_an_optional_commit_message_over_stdio(tmp_path):
    """MELLON-VER-SRS-171: Each write tool other than `tag_issue` accepts an
    optional `commit_message`; `tag_issue` has none. Read from the tools'
    input schemas, listed over stdio."""
    root = _make_repo(tmp_path / "repo")

    async def work(session):
        return (await session.list_tools()).tools

    tools = {t.name: t.inputSchema for t in _stdio(root, work, tmp_path)}

    missing = [n for n in ver_WRITE_TOOLS if n not in tools]
    assert missing == [], f"write tools not offered: {missing}"
    wrong = []
    for name in ver_WRITE_TOOLS:
        props = tools[name].get("properties", {})
        required = tools[name].get("required", [])
        if name == "tag_issue":
            if "commit_message" in props:
                wrong.append(f"{name} has commit_message")
            continue
        if "commit_message" not in props:
            wrong.append(f"{name} has no commit_message")
        elif "commit_message" in required:
            wrong.append(f"{name}'s commit_message is required")
        elif not _accepts_string(props["commit_message"]):
            wrong.append(f"{name}'s commit_message isn't a string: {props['commit_message']}")
    assert wrong == []


@pytest.mark.parametrize("cause", ["index_lock", "no_user_email"])
def test_ver_srs_173_failed_commit_message_commit_keeps_the_write_and_returns_the_error(mcp_repo, request, cause):
    """MELLON-VER-SRS-173: If the commit for a write tool's `commit_message`
    fails, the MCP server keeps the write in the working copy and returns the
    commit's error with the result; once the cause is gone, `commit_changes`
    commits the kept write."""
    root = _mcp_root(mcp_repo)
    old_head = _head(root)
    lock = root / ".git/index.lock"
    if cause == "index_lock":
        _write(lock, "")
    else:
        request.getfixturevalue("only_repo_git_config")
        _git(root, "config", "--unset", "user.email")
    try:
        res = server.set_attribute("CHILD", "CHILD-0", "status", "Approved", commit_message="Approve CHILD-0")
    finally:
        if lock.exists():
            lock.unlink()

    assert res.get("ok") is True, f"the write isn't reported as done: {res}"
    assert res.get("commit_error"), f"the result carries no commit error: {res}"
    assert "commit" not in res, f"the result reports a commit: {res}"
    assert _head(root) == old_head
    assert _attr((root / "CHILD/objects/CHILD-0.xml").read_bytes(), "status") == "Approved"

    if cause == "no_user_email":
        _git(root, "config", "user.email", IDENTITY[1])
    again = server.commit_changes("Commit the kept write")
    assert again.get("ok") is True, again
    assert _attr(_tree(root)["CHILD/objects/CHILD-0.xml"], "status") == "Approved"


def test_ver_srs_173_commit_message_without_a_git_repository_keeps_the_write(mcp_repo):
    """MELLON-VER-SRS-173 with git absent: the root holds documents but no
    git repository, so the commit fails; the write stays and the result
    carries the commit's error."""
    root = Path(mcp_repo)
    assert not (root / ".git").exists()

    res = server.set_attribute("CHILD", "CHILD-0", "status", "Approved", commit_message="Approve CHILD-0")

    assert res.get("ok") is True, f"the write isn't reported as done: {res}"
    assert res.get("commit_error"), f"the result carries no commit error: {res}"
    assert _attr((root / "CHILD/objects/CHILD-0.xml").read_bytes(), "status") == "Approved"


# MELLON-PDF-SRS blind tests
import re  # noqa: E402
import time  # noqa: E402

from tests.pdf_support import (  # noqa: E402
    FIGURES_FAIL,
    FIGURES_WRITE_EMPTY_FILES,
    FIGURES_WRITE_NOTHING,
    OS_REASON,
    Pdf,
    _clean_repo_for_177,
    _figure_repo,
    _git_state,
    _linked_repo,
    _status as pdf_status,
    _uncommitted_repo,
    git_commit_all,
    hide_drawio,
    make_program,
    mcp_calls,
    mcp_export,
    needs_pdf,
    obj,
    pdf_standin,
    short,
    small_repo,
    squash,
    write_doc as pdf_write_doc,
)


@needs_pdf
def test_pdf_srs_10_export_tool_shows_uncommitted_changes(tmp_path):
    """MELLON-PDF-SRS-10: an export that chooses no version exports the working
    copy, with the document's uncommitted changes (export tool)."""
    root = _uncommitted_repo(tmp_path / "repo")
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out)
    assert res["ok"], res
    text = squash(Pdf(out).text)
    assert squash("Uncommitted wording beta.") in text, "the PDF doesn't show the uncommitted text"
    assert squash("Committed wording alpha.") not in text, "the PDF shows the committed text"


# ==========================================================================
# Where the PDF goes
# ==========================================================================


@pytest.mark.parametrize(
    "name",
    ["reportpdf", "report.pdf.txt", "report.pd", "report"],
    ids=["reportpdf", "pdf-then-txt", "pd", "no-extension"],
)
def test_pdf_srs_26_export_tool_refuses_a_name_not_ending_in_pdf(tmp_path, name):
    """MELLON-PDF-SRS-26: given an output path whose file name doesn't end in
    .pdf, the export tool refuses the export, and no file is written."""
    root = small_repo(tmp_path / "repo")
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    res = mcp_export(root, "DOC", out_dir / name, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is False, f"{name!r} was accepted: {res}"
    assert list(out_dir.iterdir()) == [], "a refused export wrote a file"


def test_pdf_srs_26_export_tool_accepts_a_name_ending_in_pdf(tmp_path):
    """MELLON-PDF-SRS-26, other side of the boundary: a file name that ends in
    .pdf is exported to."""
    root = small_repo(tmp_path / "repo")
    out = tmp_path / "exports" / "report.pdf"
    out.parent.mkdir()
    res = mcp_export(root, "DOC", out, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is True, res
    assert out.read_bytes()[:5] == b"%PDF-"


@pytest.mark.parametrize("where", ["absolute", "nested", "relative"])
def test_pdf_srs_27_export_tool_refuses_a_path_inside_dot_git(tmp_path, where):
    """MELLON-PDF-SRS-27: given an output path inside the repository's .git
    folder, the export tool refuses the export, and no file is written there."""
    root = small_repo(tmp_path / "repo")
    target = {
        "absolute": root / ".git" / "x.pdf",
        "nested": root / ".git" / "refs" / "x.pdf",
        "relative": Path(".git") / "x.pdf",
    }[where]
    before = sorted(p.relative_to(root / ".git").as_posix() for p in (root / ".git").rglob("*"))
    res = mcp_export(root, "DOC", target, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is False, f"{target} was accepted: {res}"
    after = sorted(p.relative_to(root / ".git").as_posix() for p in (root / ".git").rglob("*"))
    assert [p for p in after if p not in before] == [], "a refused export wrote into .git"


def test_pdf_srs_27_export_tool_accepts_a_file_beside_dot_git(tmp_path):
    """MELLON-PDF-SRS-27, other side of the boundary: a file named .git.pdf in
    the repository root isn't inside the .git folder, so it is exported to."""
    root = small_repo(tmp_path / "repo")
    out = root / ".git.pdf"
    res = mcp_export(root, "DOC", out, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is True, res
    assert out.read_bytes()[:5] == b"%PDF-"


def _three_states_repo(root: Path) -> Path:
    """DOC-1 has an OK, a suspect and a broken outgoing link."""
    pdf_write_doc(root, "PAR", [obj("PAR-1", "Parent one."), obj("PAR-2", "Parent two.")])
    ack = short(git_commit_all(root, "parent"))
    pdf_write_doc(
        root,
        "DOC",
        [obj("DOC-1", "One.", links=[("PAR", "PAR-1", ack), ("PAR", "PAR-2", None), ("PAR", "PAR-9", None)])],
        parents=("PAR",),
    )
    git_commit_all(root, "doc")
    return root


def _link_key(item: dict) -> tuple:
    return (item["source_id"], item["link"]["target_doc"], item["link"]["target_id"])


def test_pdf_srs_54_result_lists_each_suspect_and_broken_link(tmp_path):
    """MELLON-PDF-SRS-54: the export tool returns each suspect and broken
    outgoing link of the document, as the suspects tool reports it; an OK link
    isn't among them."""
    root = _three_states_repo(tmp_path / "repo")
    exported, suspects = mcp_calls(
        root,
        [
            ("export_document", {"doc_id": "DOC", "out_path": str(tmp_path / "out.pdf")}),
            ("suspects", {"doc_id": "DOC"}),
        ],
        env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))},
    )
    assert exported["ok"], exported
    expected = {_link_key(s): s for s in suspects if s["state"] in ("suspect", "broken")}
    assert {k[2] for k in expected} == {"PAR-2", "PAR-9"}, "precondition: one suspect and one broken link"
    got = {_link_key(s): s for s in exported["suspect_links"]}
    assert set(got) == set(expected), f"links returned: {sorted(got)}"
    for key, item in got.items():
        for field in ("source_id", "link", "state", "current_hash", "reason"):
            assert item.get(field) == expected[key].get(field), f"{key}: {field} differs"


def test_pdf_srs_54_items_are_in_the_suspects_tool_form(tmp_path):
    """MELLON-PDF-SRS-54: each link the export tool returns is in the form the
    suspects tool gives: the same fields with the same values."""
    root = _three_states_repo(tmp_path / "repo")
    exported, suspects = mcp_calls(
        root,
        [
            ("export_document", {"doc_id": "DOC", "out_path": str(tmp_path / "out.pdf")}),
            ("suspects", {"doc_id": "DOC"}),
        ],
        env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))},
    )
    expected = {_link_key(s): s for s in suspects if s["state"] in ("suspect", "broken")}
    for item in exported["suspect_links"]:
        assert item == expected[_link_key(item)]


def test_pdf_srs_56_export_tool_exports_a_document_with_a_broken_link(tmp_path):
    """MELLON-PDF-SRS-56: the export tool exports a document that has a broken
    outgoing link: ok, and the PDF is written."""
    root = _linked_repo(tmp_path / "repo", broken=True)
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is True, res
    assert out.read_bytes()[:5] == b"%PDF-"


@pytest.mark.parametrize(
    "figure_program",
    [
        pytest.param(FIGURES_WRITE_NOTHING, id="writes-nothing"),
        pytest.param(
            FIGURES_WRITE_EMPTY_FILES,
            id="writes-empty-file",
        ),
    ],
)
def test_pdf_srs_61_result_lists_a_figure_the_figure_program_left_out(tmp_path, figure_program):
    """MELLON-PDF-SRS-61 (test case of MELLON-PDF-SRS-58): when the figure
    program exits with status 0 but writes no figure PDF, or an empty one, the
    export tool's result names the object whose figure isn't drawn."""
    root = _figure_repo(tmp_path / "repo")
    env = {
        "MELLON_TECTONIC": str(pdf_standin(tmp_path)),
        "MELLON_DRAWIO": str(make_program(tmp_path / "fig", "drawio", figure_program)),
    }
    res = mcp_export(root, "DOC", tmp_path / "out.pdf", env=env)
    assert res["ok"], res
    assert res.get("unrendered_figures") == ["DOC-1"], res


def test_pdf_srs_62_result_says_the_figure_program_was_not_found(tmp_path, monkeypatch):
    """MELLON-PDF-SRS-62: when Mellon can't find the figure program, the export
    tool's result says so (drawio_found false)."""
    root = _figure_repo(tmp_path / "repo")
    env = hide_drawio(monkeypatch, tmp_path)
    env["MELLON_TECTONIC"] = str(pdf_standin(tmp_path))
    res = mcp_export(root, "DOC", tmp_path / "out.pdf", env=env)
    assert res["ok"], res
    assert res.get("drawio_found") is False, res


def test_pdf_srs_62_result_doesnt_say_not_found_when_the_program_is_found(tmp_path):
    """MELLON-PDF-SRS-62, other side: with the figure program found (a stand-in
    that fails), the result doesn't say it wasn't found."""
    root = _figure_repo(tmp_path / "repo")
    env = {
        "MELLON_TECTONIC": str(pdf_standin(tmp_path)),
        "MELLON_DRAWIO": str(make_program(tmp_path / "fig", "drawio", FIGURES_FAIL)),
    }
    res = mcp_export(root, "DOC", tmp_path / "out.pdf", env=env)
    assert res["ok"], res
    assert res.get("drawio_found") is not False, res


def test_pdf_srs_65_export_tool_reports_a_failure_with_its_reason(tmp_path):
    """MELLON-PDF-SRS-65: an export that fails for a reason other than the PDF
    program (its output path is a folder, so it can't be written) returns an
    error result with the reason."""
    root = small_repo(tmp_path / "repo")
    taken = tmp_path / "taken-by-folder.pdf"
    taken.mkdir()
    res = mcp_export(root, "DOC", taken, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is False, res
    reason = str(res.get("error", ""))
    assert re.search(OS_REASON, reason, re.I), f"no reason given: {res}"
    assert taken.is_dir() and list(taken.iterdir()) == []


NEVER_ENDING_CMD = "@echo off\r\n:again\r\nping -n 2 127.0.0.1 >nul\r\ngoto again\r\n"


def _processes_running(fragment: str) -> list[str]:
    import subprocess

    if os.name == "nt":
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine }",
            ],
            capture_output=True,
            text=True,
        ).stdout
    else:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if fragment in line]


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("MELLON_SLOW_TESTS") != "1",
    reason="takes 5 minutes: set MELLON_SLOW_TESTS=1 to run it",
)
def test_pdf_srs_70_pdf_program_stopped_after_300_s(tmp_path):
    """MELLON-PDF-SRS-70: if the PDF program hasn't finished 300 s after its
    start, Mellon stops it and ends the export: not before 300 s, soon after,
    and the program no longer runs."""
    root = small_repo(tmp_path / "repo")
    bin_dir = tmp_path / "never-ending-pdf-program"
    bin_dir.mkdir()
    if os.name == "nt":
        program = bin_dir / "tectonic.cmd"
        program.write_text(NEVER_ENDING_CMD, encoding="ascii", newline="")
    else:
        program = bin_dir / "tectonic"
        program.write_text("#!/bin/sh\nexec sleep 100000\n", encoding="ascii", newline="\n")
        program.chmod(0o755)
    start = time.monotonic()
    res = mcp_export(root, "DOC", tmp_path / "out.pdf", env={"MELLON_TECTONIC": str(program)})
    elapsed = time.monotonic() - start
    assert res["ok"] is False, res
    assert elapsed >= 300, f"the export ended after {elapsed:.0f} s, before the 300 s limit"
    assert elapsed < 360, f"the export ended {elapsed:.0f} s after its start"
    time.sleep(3)
    assert _processes_running(str(bin_dir)) == [], "the PDF program still runs"


@needs_pdf
def test_pdf_srs_177_export_tool_changes_no_file_index_or_reference(tmp_path):
    """MELLON-PDF-SRS-177: an export through the export tool, to a path in the
    work tree outside the document folders, changes no working-copy file, the
    index or a reference: git status shows only the PDF."""
    root = _clean_repo_for_177(tmp_path / "repo")
    (root / "out").mkdir()
    before = _git_state(root, set())
    res = mcp_export(root, "DOC", root / "out" / "doc.pdf")
    assert res["ok"], res
    assert pdf_status(root) == {"out/doc.pdf": pygit2.GIT_STATUS_WT_NEW}, pdf_status(root)
    after = _git_state(root, {"out/doc.pdf"})
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert changed == [], f"the export changed: {changed}"


def test_pdf_srs_5_export_tool_exports_without_a_git_user_name(tmp_path):
    """MELLON-PDF-SRS-5 (T10): the export tool exports with no git user.name
    set, since an export makes no commit."""
    import subprocess

    root = small_repo(tmp_path / "repo")
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive or "",
        "HOMEPATH": str(home)[len(home.drive):],
        "XDG_CONFIG_HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / "none"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PROGRAMDATA": str(home),
        "MELLON_TECTONIC": str(pdf_standin(tmp_path)),
    }
    probe = subprocess.run(
        [sys.executable, "-c", "import pygit2, sys; print('user.name' in pygit2.Repository(sys.argv[1]).config)", str(root)],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    if probe.stdout.strip() != "False":
        pytest.skip(f"a git user.name is still visible to the server: {probe.stdout or probe.stderr}")
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out, env=env)
    assert res["ok"] is True, res
    assert out.read_bytes()[:5] == b"%PDF-"


def test_pdf_srs_5_export_tool_exports_while_another_document_fails_to_load(tmp_path):
    """MELLON-PDF-SRS-5 (T10): the export tool exports a document while another
    document of the repository, not a parent, fails to load."""
    root = small_repo(tmp_path / "repo")
    pdf_write_doc(root, "OTHER", [obj("OTHER-1", "Other.")])
    git_commit_all(root, "other")
    (root / "OTHER" / "OTHER.xml").write_text("<document id=", encoding="utf-8")
    out = tmp_path / "out.pdf"
    res = mcp_export(root, "DOC", out, env={"MELLON_TECTONIC": str(pdf_standin(tmp_path))})
    assert res["ok"] is True, res
    assert out.read_bytes()[:5] == b"%PDF-"
