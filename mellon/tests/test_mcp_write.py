"""Tests for the mutation MCP tools.

Most tests use the `mcp_repo` fixture (tmp_path, no git). Tests that need a
real git history — `ack_suspects` — use `req_test_copy`, which copies the
committed req-test fixture into tmp_path so the test never mutates the
original.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mellon.mcp import server


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
    with pytest.raises(ValueError, match="No document 'NOPE'"):
        server.new_object("NOPE")


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
    with pytest.raises(PermissionError, match="read-only"):
        server.copy_object("CHILD", "CHILD-0")


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
    with pytest.raises(PermissionError, match="read-only"):
        server.bulk_set_attribute(
            "CHILD", ["CHILD-0"], "status", "Approved"
        )


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
    with pytest.raises(PermissionError, match="read-only"):
        server.reorder_outline(
            "PARENT",
            [
                {"id": "PARENT-0", "children": []},
                {"id": "PARENT-1", "children": []},
            ],
        )


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
    with pytest.raises(PermissionError):
        server.manage_view("PARENT", action="create", name="Dev", visible_attrs=["status"])
    with pytest.raises(PermissionError):
        server.manage_view("PARENT", action="rename", slug="dev", name="Anything")
    with pytest.raises(PermissionError):
        server.manage_view("PARENT", action="delete", slug="dev")
    with pytest.raises(PermissionError):
        server.manage_view("PARENT", action="edit", slug="dev", visible_attrs=["status"])


# --- read-only guard -------------------------------------------------


def test_read_only_blocks_writes(
    mcp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    with pytest.raises(PermissionError, match="read-only"):
        server.new_object("PARENT")
    with pytest.raises(PermissionError):
        server.set_attribute("PARENT", "PARENT-1", "status", "Draft")
    # Read tools remain callable.
    docs = server.list_documents()
    assert {d["doc_id"] for d in docs} == {"PARENT", "CHILD"}


# Every registered tool not listed here must refuse under --read-only, so a
# new write tool that forgets `_guard_write()` fails the test below.
_READ_TOOLS = frozenset({
    "list_documents", "show_document", "search_objects", "show_object",
    "inspect_object", "outline", "validate_document", "validate_repo",
    "repo_health", "suspects", "trace", "incoming_links", "coverage_report",
    "git_history", "show_object_at", "review_suspect_link",
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
        with pytest.raises(ToolError) as ei:
            asyncio.run(server.mcp.call_tool(tool, args))
        assert isinstance(ei.value.__cause__, PermissionError), ei.value
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
    with pytest.raises(PermissionError):
        server.create_document("SHOULD_FAIL")


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
    with pytest.raises(PermissionError):
        server.set_figure("PARENT", "PARENT-1", str(src))


def test_clear_figure_read_only_raises(mcp_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(server._State, "read_only", True)
    import pytest
    with pytest.raises(PermissionError):
        server.clear_figure("PARENT", "PARENT-1")


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
    with pytest.raises(PermissionError):
        server.set_table("PARENT", "PARENT-1", _TABLE_DICT)


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
    monkeypatch.setattr(server, "find_drawio", lambda: None)
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
    with pytest.raises(PermissionError, match="read-only"):
        server.export_document("CHILD", "out.pdf")


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
