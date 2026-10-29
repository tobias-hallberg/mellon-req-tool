"""Tests for `mellon.diff.compare_document`.

Self-contained: every test builds a small git repo in tmp_path with a
sequence of doc-state commits, then asserts the structured diff between
two commits. No dependency on the `req-test` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from mellon.diff import (
    AttributeChange,
    DescriptionChange,
    DocumentDiff,
    LinkAckChange,
    LinkChange,
    ObjectDelta,
    compare_document,
)
from mellon.git_ops import GitError, commit, create_issue_tag


# --- builders ------------------------------------------------------------


def _object_xml(obj_id: str, attrs: dict[str, str], description: str) -> str:
    attr_lines = "\n".join(
        f'  <attr name="{k}">{v}</attr>' for k, v in attrs.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<object id="{obj_id}">\n'
        f"{attr_lines}\n"
        f"  <description>{description}</description>\n"
        "</object>\n"
    )


def _links_xml(source_id: str, links: list[dict]) -> str:
    """Each link is {target_doc, target_id, type, ack=None}."""
    parts = []
    for link in links:
        ack_attr = f' ack="{link["ack"]}"' if link.get("ack") else ""
        parts.append(
            f'  <link target-doc="{link["target_doc"]}" '
            f'target-id="{link["target_id"]}" '
            f'type="{link["type"]}"{ack_attr}/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<links source="{source_id}">\n'
        + "\n".join(parts)
        + "\n</links>\n"
    )


def _write_object(
    root: Path,
    doc_id: str,
    obj_id: str,
    attrs: dict[str, str],
    description: str,
) -> None:
    objects_dir = root / doc_id / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    (objects_dir / f"{obj_id}.xml").write_text(
        _object_xml(obj_id, attrs, description), encoding="utf-8"
    )


def _write_links(
    root: Path, doc_id: str, source_id: str, links: list[dict]
) -> None:
    links_dir = root / doc_id / "links"
    links_dir.mkdir(parents=True, exist_ok=True)
    (links_dir / f"{source_id}.xml").write_text(
        _links_xml(source_id, links), encoding="utf-8"
    )


def _delete(root: Path, *rel_parts: str) -> None:
    p = root.joinpath(*rel_parts)
    if p.exists():
        p.unlink()


@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    pygit2.init_repository(str(tmp_path), bare=False)
    return tmp_path


# --- core membership --------------------------------------------------------


def test_identical_refs_yields_empty_diff(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "Body")
    c1 = commit(tmp_git, "seed")

    diff = compare_document(tmp_git, "DOC", c1, c1)
    assert diff.is_empty
    assert diff.added_objects == ()
    assert diff.removed_objects == ()
    assert diff.modified_objects == ()
    assert diff.short_a == diff.short_b == c1


def test_object_added(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "seed")
    _write_object(tmp_git, "DOC", "DOC-2", {"type": "Requirement"}, "y")
    c2 = commit(tmp_git, "add DOC-2")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    assert diff.added_objects == ("DOC-2",)
    assert diff.removed_objects == ()
    assert diff.modified_objects == ()


def test_object_removed(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    _write_object(tmp_git, "DOC", "DOC-2", {"type": "Requirement"}, "y")
    c1 = commit(tmp_git, "seed")
    _delete(tmp_git, "DOC", "objects", "DOC-2.xml")
    c2 = commit(tmp_git, "remove DOC-2")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    assert diff.added_objects == ()
    assert diff.removed_objects == ("DOC-2",)
    assert diff.modified_objects == ()


def test_a_to_b_symmetry(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "seed")
    _write_object(tmp_git, "DOC", "DOC-2", {"type": "Requirement"}, "y")
    c2 = commit(tmp_git, "add DOC-2")

    forward = compare_document(tmp_git, "DOC", c1, c2)
    backward = compare_document(tmp_git, "DOC", c2, c1)
    assert forward.added_objects == backward.removed_objects
    assert forward.removed_objects == backward.added_objects


# --- attribute changes ------------------------------------------------------


def test_attribute_value_changed(tmp_git: Path) -> None:
    _write_object(
        tmp_git, "DOC", "DOC-1",
        {"type": "Requirement", "status": "Draft"}, "x",
    )
    c1 = commit(tmp_git, "seed")
    _write_object(
        tmp_git, "DOC", "DOC-1",
        {"type": "Requirement", "status": "Approved"}, "x",
    )
    c2 = commit(tmp_git, "approve")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    assert len(diff.modified_objects) == 1
    delta = diff.modified_objects[0]
    assert delta.obj_id == "DOC-1"
    assert delta.attrs == (
        AttributeChange(name="status", value_a="Draft", value_b="Approved"),
    )
    assert delta.description is None
    assert delta.links.is_empty


def test_attribute_added_and_removed(tmp_git: Path) -> None:
    _write_object(
        tmp_git, "DOC", "DOC-1",
        {"type": "Requirement", "rationale": "because"}, "x",
    )
    c1 = commit(tmp_git, "seed")
    _write_object(
        tmp_git, "DOC", "DOC-1",
        {"type": "Requirement", "verification-method": "test"}, "x",
    )
    c2 = commit(tmp_git, "swap attr")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    delta = diff.modified_objects[0]
    by_name = {a.name: a for a in delta.attrs}
    assert by_name["rationale"] == AttributeChange(
        name="rationale", value_a="because", value_b=None
    )
    assert by_name["verification-method"] == AttributeChange(
        name="verification-method", value_a=None, value_b="test"
    )


# --- description changes ----------------------------------------------------


def test_description_changed(tmp_git: Path) -> None:
    _write_object(
        tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "Old text"
    )
    c1 = commit(tmp_git, "seed")
    _write_object(
        tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "New text"
    )
    c2 = commit(tmp_git, "edit desc")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    delta = diff.modified_objects[0]
    assert delta.description == DescriptionChange(
        text_a="Old text", text_b="New text"
    )
    udiff = delta.description.unified_diff()
    assert "-Old text" in udiff
    assert "+New text" in udiff


# --- link changes -----------------------------------------------------------


def test_link_added(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "seed")
    _write_links(
        tmp_git, "DOC", "DOC-1",
        [{"target_doc": "PARENT", "target_id": "P-1", "type": "satisfies"}],
    )
    c2 = commit(tmp_git, "add link")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    delta = diff.modified_objects[0]
    assert delta.links.added == (
        LinkChange(
            target_doc="PARENT", target_id="P-1", type="satisfies", ack=None
        ),
    )
    assert delta.links.removed == ()
    assert delta.links.ack_changed == ()


def test_link_removed(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    _write_links(
        tmp_git, "DOC", "DOC-1",
        [{"target_doc": "PARENT", "target_id": "P-1", "type": "satisfies"}],
    )
    c1 = commit(tmp_git, "seed")
    _delete(tmp_git, "DOC", "links", "DOC-1.xml")
    c2 = commit(tmp_git, "remove link")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    delta = diff.modified_objects[0]
    assert delta.links.removed == (
        LinkChange(
            target_doc="PARENT", target_id="P-1", type="satisfies", ack=None
        ),
    )


def test_link_ack_changed_not_added_or_removed(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    _write_links(
        tmp_git, "DOC", "DOC-1",
        [{
            "target_doc": "PARENT", "target_id": "P-1",
            "type": "satisfies", "ack": "abc1234",
        }],
    )
    c1 = commit(tmp_git, "seed")
    _write_links(
        tmp_git, "DOC", "DOC-1",
        [{
            "target_doc": "PARENT", "target_id": "P-1",
            "type": "satisfies", "ack": "def5678",
        }],
    )
    c2 = commit(tmp_git, "re-ack link")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    delta = diff.modified_objects[0]
    assert delta.links.added == ()
    assert delta.links.removed == ()
    assert delta.links.ack_changed == (
        LinkAckChange(
            target_doc="PARENT", target_id="P-1", type="satisfies",
            ack_a="abc1234", ack_b="def5678",
        ),
    )


# --- whole-doc edge cases ---------------------------------------------------


def test_doc_added_wholesale(tmp_git: Path) -> None:
    _write_object(tmp_git, "OTHER", "OTHER-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "other only")
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "y")
    c2 = commit(tmp_git, "add DOC")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    assert diff.added_objects == ("DOC-1",)
    assert diff.removed_objects == ()


def test_doc_removed_wholesale(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    _write_object(tmp_git, "OTHER", "OTHER-1", {"type": "Requirement"}, "y")
    c1 = commit(tmp_git, "seed")
    # Remove DOC entirely.
    import shutil
    shutil.rmtree(tmp_git / "DOC")
    c2 = commit(tmp_git, "drop DOC")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    assert diff.added_objects == ()
    assert diff.removed_objects == ("DOC-1",)


def test_doc_absent_both_sides_raises(tmp_git: Path) -> None:
    _write_object(tmp_git, "OTHER", "OTHER-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "seed")
    _write_object(tmp_git, "OTHER", "OTHER-2", {"type": "Requirement"}, "y")
    c2 = commit(tmp_git, "extend OTHER")

    with pytest.raises(GitError, match="not present at"):
        compare_document(tmp_git, "DOC", c1, c2)


# --- ref handling -----------------------------------------------------------


def test_unresolvable_ref_raises(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "seed")
    with pytest.raises(GitError, match="cannot resolve"):
        compare_document(tmp_git, "DOC", c1, "deadbeef")


def test_issue_tag_as_ref(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    c1 = commit(tmp_git, "seed")
    _write_object(tmp_git, "DOC", "DOC-2", {"type": "Requirement"}, "y")
    c2 = commit(tmp_git, "add DOC-2")
    repo = pygit2.Repository(str(tmp_git))
    create_issue_tag(repo, "DOC", "1", repo.revparse_single(c1).peel(pygit2.Commit).id)
    create_issue_tag(repo, "DOC", "2", repo.revparse_single(c2).peel(pygit2.Commit).id)

    diff = compare_document(tmp_git, "DOC", "DOC-issue-1", "DOC-issue-2")
    assert diff.added_objects == ("DOC-2",)
    assert diff.removed_objects == ()
    assert diff.ref_a == "DOC-issue-1"
    assert diff.ref_b == "DOC-issue-2"
    assert diff.short_a == c1
    assert diff.short_b == c2


# --- combined change record -------------------------------------------------


def test_combined_changes_in_one_object(tmp_git: Path) -> None:
    _write_object(
        tmp_git, "DOC", "DOC-1",
        {"type": "Requirement", "status": "Draft"}, "Old",
    )
    _write_links(
        tmp_git, "DOC", "DOC-1",
        [{"target_doc": "P", "target_id": "P-1", "type": "satisfies"}],
    )
    c1 = commit(tmp_git, "seed")
    _write_object(
        tmp_git, "DOC", "DOC-1",
        {"type": "Requirement", "status": "Approved"}, "New",
    )
    _write_links(
        tmp_git, "DOC", "DOC-1",
        [
            {"target_doc": "P", "target_id": "P-1", "type": "satisfies"},
            {"target_doc": "P", "target_id": "P-2", "type": "satisfies"},
        ],
    )
    c2 = commit(tmp_git, "edit + extra link")

    diff = compare_document(tmp_git, "DOC", c1, c2)
    assert len(diff.modified_objects) == 1
    delta = diff.modified_objects[0]
    assert delta.attrs == (
        AttributeChange(name="status", value_a="Draft", value_b="Approved"),
    )
    assert delta.description == DescriptionChange(text_a="Old", text_b="New")
    assert len(delta.links.added) == 1
    assert delta.links.added[0].target_id == "P-2"
    assert delta.links.removed == ()
