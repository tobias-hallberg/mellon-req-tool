"""Tests for `mellon.diff.compare_document`.

Self-contained: every test builds a small git repo in tmp_path with a
sequence of doc-state commits, then asserts the structured diff between
two commits. No dependency on the `req-test` fixture.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Callable

import pygit2
import pytest
from lxml import etree

from mellon import diff, editor, git_ops
from mellon.diff import (
    LATEST,
    WORKING_COPY,
    AttributeChange,
    Change,
    DescriptionChange,
    DocumentDiff,
    FigureChange,
    LinkAckChange,
    LinkChange,
    ObjectDelta,
    Position,
    TableChange,
    compare_document,
    default_versions,
    document_versions,
    word_diff,
)
from mellon.git_ops import GitError, commit, create_issue_tag
from mellon.schema import Table, TableCell, TableColumn, TableRow
from tests.compare_support import (
    DECL,
    DOC,
    SVG_1,
    GitRepo,
    _ambiguous_prefix,
    _blank_lines,
    _build,
    _cdata,
    _char_refs,
    _commit_of,
    _crlf,
    _everything,
    _expand_empty,
    _no_declaration,
    _oid,
    _r2_commit,
    _reverse_attributes,
    _rewrite,
    _side_commit,
    _single_quotes,
    _space_inside_tags,
    _table,
    _tabs,
    build_other_only,
    build_snapshot_repo,
    build_spec,
    links_xml,
    snapshot,
    snapshot_difference,
)


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


@pytest.mark.parametrize(
    "ref, back", [("@", 0), ("@~1", 1), ("@^", 1), ("@^{commit}", 0), ("@~1^{/seed}", 1)]
)
def test_lone_at_is_head(tmp_git: Path, ref: str, back: int) -> None:
    # MELLON-VER-SRS-9: git reads @ on its own as HEAD, also before ~, ^ and :.
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    commit(tmp_git, "seed")
    _write_object(tmp_git, "DOC", "DOC-2", {"type": "Requirement"}, "y")
    commit(tmp_git, "add DOC-2")
    repo = pygit2.Repository(str(tmp_git))
    expected = repo.revparse_single(f"HEAD~{back}").peel(pygit2.Commit).id
    assert compare_document(tmp_git, "DOC", ref, "HEAD").commit_a == str(expected)


def test_lone_at_past_the_first_commit_names_the_typed_text(tmp_git: Path) -> None:
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    commit(tmp_git, "seed")
    with pytest.raises(GitError, match="cannot resolve '@~3'"):
        compare_document(tmp_git, "DOC", "@~3", "HEAD")


def test_each_version_reports_the_full_hash_of_its_commit(tmp_git: Path) -> None:
    # MELLON-VER-SRS-67, -87: an annotated tag resolves to its commit, whose
    # hash is reported, not the tag object's; the working copy has none.
    _write_object(tmp_git, "DOC", "DOC-1", {"type": "Requirement"}, "x")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.peel(pygit2.Commit).id
    create_issue_tag(repo, "DOC", "1", head)
    diff = compare_document(tmp_git, "DOC", "DOC-issue-1", WORKING_COPY)
    assert diff.commit_a == str(head)
    assert diff.short_a == str(head)[:7]
    assert diff.commit_b is None
    assert diff.short_b == WORKING_COPY


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


# --- whole documents: outline, tables, figures, working copy ----------------
#
# These build real documents with the editor, as the GUI and MCP do, so the
# manifest outline is there for move detection and display order.


_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    b'<rect width="20" height="10" fill="red"/></svg>\n'
)


@pytest.fixture
def spec(tmp_git: Path) -> Path:
    """SPEC: SPEC-0 (a heading) over SPEC-1..3, then SPEC-4 at the top
    level, committed as "seed"."""
    folder = editor.create_document(tmp_git, "SPEC", title="Spec")
    editor.create_object(folder, None, "Heading")
    for n in (1, 2, 3):
        editor.create_object(folder, "SPEC-0", f"Requirement {n}.")
    editor.create_object(folder, None, "Requirement 4.")
    commit(tmp_git, "seed")
    return folder


def _kinds(diff) -> dict[str, tuple[str, ...]]:
    return {c.obj_id: tuple(str(k) for k in c.kinds) for c in diff.changes}


def test_object_moved_to_another_parent(tmp_git: Path, spec: Path) -> None:
    editor.move_object(spec, "SPEC-3", parent_id="SPEC-4")
    commit(tmp_git, "move")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-3": ("Moved",)}
    change = diff.changes[0]
    assert change.before.position == Position("SPEC-0", "SPEC-2")
    assert change.after.position == Position("SPEC-4", None)
    assert change.delta is None
    assert diff.modified_objects == ()


def test_reordering_marks_only_the_object_that_moved(tmp_git: Path, spec: Path) -> None:
    editor.move_object(spec, "SPEC-3", parent_id="SPEC-0", before="SPEC-1")
    commit(tmp_git, "reorder")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-3": ("Moved",)}


def test_inserting_an_object_moves_nothing(tmp_git: Path, spec: Path) -> None:
    new_id = editor.create_object(spec, "SPEC-0", "Inserted.")
    editor.move_object(spec, new_id, parent_id="SPEC-0", before="SPEC-1")
    commit(tmp_git, "insert first")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {new_id: ("Added",)}
    assert diff.changes[0].after.position == Position("SPEC-0", None)


def test_changes_follow_the_outline_with_deleted_objects_in_place(
    tmp_git: Path, spec: Path
) -> None:
    editor.set_description(spec, "SPEC-4", "Requirement 4, changed.")
    editor.set_description(spec, "SPEC-1", "Requirement 1, changed.")
    editor.delete_object(spec, "SPEC-2")
    commit(tmp_git, "edit and delete")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert [c.obj_id for c in diff.changes] == ["SPEC-1", "SPEC-2", "SPEC-4"]
    assert _kinds(diff)["SPEC-2"] == ("Deleted",)
    # A deleted object keeps its text, from A.
    assert diff.changes[1].text == "Requirement 2."
    assert diff.changes[1].after is None


def test_modified_and_moved_has_both(tmp_git: Path, spec: Path) -> None:
    editor.set_description(spec, "SPEC-2", "Requirement 2, changed.")
    editor.move_object(spec, "SPEC-2", to_top=True)
    commit(tmp_git, "edit and move")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-2": ("Modified", "Moved")}
    assert diff.counts()[Change.MODIFIED] == 1
    assert diff.counts()[Change.MOVED] == 1


def test_table_change(tmp_git: Path, spec: Path) -> None:
    table = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell("a"), TableCell("b"))),),
    )
    editor.set_table(spec, "SPEC-1", table)
    commit(tmp_git, "table")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-1": ("Modified",)}
    assert diff.changes[0].delta.table == TableChange(None, table)


def test_figure_change(tmp_git: Path, spec: Path, tmp_path_factory) -> None:
    src = tmp_path_factory.mktemp("svg") / "fig.svg"
    src.write_bytes(_SVG)
    editor.set_figure(spec, "SPEC-1", src)
    commit(tmp_git, "figure")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-1": ("Modified",)}
    figure = diff.changes[0].delta.figure
    assert isinstance(figure, FigureChange)
    assert figure.svg_a is None
    assert figure.svg_b == (spec / "figures" / "SPEC-1.svg").read_bytes()


def _figure_kinds(tmp_git: Path, spec: Path, a: bytes, b: bytes) -> dict:
    """Commit SPEC-1's figure as `a`, then as `b` (by hand, as a git client
    could), and compare the two commits."""
    path = spec / "figures" / "SPEC-1.svg"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(a)
    commit(tmp_git, "figure a")
    path.write_bytes(b)
    commit(tmp_git, "figure b")
    return _kinds(compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD"))


def test_figure_comments_and_entities_are_no_change(tmp_git: Path, spec: Path) -> None:
    # MELLON-VER-SRS-22: Canonical XML 1.1 (without comments) expands
    # internal entities and drops comments, the DOCTYPE and the declaration.
    plain = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Speed</text></svg>'
    other = (
        b'<?xml version="1.0"?>\n<!DOCTYPE svg [<!ENTITY s "Speed">]>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg"><!-- drawn --><text>&s;</text></svg>\n'
    )
    assert _figure_kinds(tmp_git, spec, plain, other) == {}


@pytest.mark.parametrize(
    "a, b",
    [
        (b"<text>a b</text>", b"<text>a  b</text>"),
        # Whitespace-only text with no element beside it isn't between elements.
        (b"<text> </text>", b"<text></text>"),
    ],
)
def test_figure_whitespace_in_text_is_a_change(
    tmp_git: Path, spec: Path, a: bytes, b: bytes
) -> None:
    def svg(body: bytes) -> bytes:
        return b'<svg xmlns="http://www.w3.org/2000/svg">' + body + b"</svg>"

    assert _figure_kinds(tmp_git, spec, svg(a), svg(b)) == {"SPEC-1": ("Modified",)}


def test_figure_that_cant_be_parsed_is_compared_by_its_bytes(
    tmp_git: Path, spec: Path
) -> None:
    # Re-indenting a figure that isn't well-formed is still a change.
    broken = b'<svg xmlns="http://www.w3.org/2000/svg">\n  <rect\n'
    assert _figure_kinds(tmp_git, spec, broken, broken.replace(b"  ", b"\t")) == {
        "SPEC-1": ("Modified",)
    }


def test_only_an_ack_change_is_a_link_review(tmp_git: Path, spec: Path) -> None:
    link = {"target_doc": "SPEC", "target_id": "SPEC-4", "type": "refines"}
    _write_links(tmp_git, "SPEC", "SPEC-1", [{**link, "ack": "abc1234"}])
    commit(tmp_git, "link")
    _write_links(tmp_git, "SPEC", "SPEC-1", [{**link, "ack": "def5678"}])
    commit(tmp_git, "review")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-1": ("Link reviewed",)}
    assert diff.counts()[Change.MODIFIED] == 0
    # Still a field change in the id-sorted view.
    assert [d.obj_id for d in diff.modified_objects] == ["SPEC-1"]


def test_working_copy_includes_uncommitted_edits(tmp_git: Path, spec: Path) -> None:
    editor.set_description(spec, "SPEC-1", "Not committed yet.")
    new_id = editor.create_object(spec, None, "New, not committed.")
    diff = compare_document(tmp_git, "SPEC", LATEST, WORKING_COPY)
    assert _kinds(diff) == {"SPEC-1": ("Modified",), new_id: ("Added",)}
    assert diff.short_b == WORKING_COPY
    assert diff.changes[0].delta.description.text_b == "Not committed yet."


def test_working_copy_line_endings_alone_are_no_change(tmp_git: Path, spec: Path) -> None:
    path = spec / "objects" / "SPEC-1.xml"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    diff = compare_document(tmp_git, "SPEC", LATEST, WORKING_COPY)
    assert diff.changes == ()


def test_unreadable_object_is_marked_and_the_rest_still_compared(
    tmp_git: Path, spec: Path
) -> None:
    editor.set_description(spec, "SPEC-1", "Requirement 1, changed.")
    (spec / "objects" / "SPEC-2.xml").write_text("<object id=", encoding="utf-8")
    commit(tmp_git, "break SPEC-2")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-1": ("Modified",), "SPEC-2": ("Modified",)}
    broken = diff.changes[1]
    err_a, err_b = broken.errors
    assert err_a is None
    assert "SPEC-2.xml" in err_b and "XML parse error" in err_b
    assert broken.after.obj is None
    assert broken.before.obj.description == "Requirement 2."


def test_document_missing_at_from_lists_everything_as_added(tmp_git: Path) -> None:
    _write_object(tmp_git, "OTHER", "OTHER-1", {"type": "Requirement"}, "x")
    commit(tmp_git, "other")
    folder = editor.create_document(tmp_git, "SPEC", title="Spec")
    editor.create_object(folder, None, "One.")
    commit(tmp_git, "spec")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert (diff.present_a, diff.present_b) == (False, True)
    assert _kinds(diff) == {"SPEC-0": ("Added",)}


def test_unresolvable_ref_is_named(tmp_git: Path, spec: Path) -> None:
    with pytest.raises(GitError, match="no-such-version"):
        compare_document(tmp_git, "SPEC", "no-such-version", "HEAD")


def test_comparing_changes_nothing(tmp_git: Path, spec: Path) -> None:
    editor.set_description(spec, "SPEC-1", "Uncommitted.")
    repo = pygit2.Repository(str(tmp_git))

    def snapshot():
        files = {
            p.relative_to(tmp_git).as_posix(): p.read_bytes()
            for p in tmp_git.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(tmp_git).parts
        }
        index = (tmp_git / ".git" / "index").read_bytes()
        refs = {r: str(repo.references[r].target) for r in repo.references}
        return files, index, refs, str(repo.head.target)

    before = snapshot()
    compare_document(tmp_git, "SPEC", "HEAD", WORKING_COPY)
    compare_document(tmp_git, "SPEC", WORKING_COPY, "HEAD")
    assert snapshot() == before


def test_labels_come_from_the_manifest(tmp_git: Path, spec: Path) -> None:
    editor.add_attribute(spec, "status", "text", False, label="Status")
    editor.set_attribute(spec, "SPEC-1", "status", "Draft")
    commit(tmp_git, "status")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert diff.label("status") == "Status"
    assert diff.label("type") == "Type"
    assert diff.label("undeclared") == "undeclared"


def test_word_diff() -> None:
    assert word_diff("The VCU shall stop.", "The VCU must stop.") == (
        ("equal", "The VCU "),
        ("delete", "shall"),
        ("insert", "must"),
        ("equal", " stop."),
    )
    segments = word_diff("a b c", "a x c d")
    assert "".join(t for op, t in segments if op != "insert") == "a b c"
    assert "".join(t for op, t in segments if op != "delete") == "a x c d"


# --- versions -----------------------------------------------------------------


def _tag(root: Path, issue: str) -> None:
    repo = pygit2.Repository(str(root))
    create_issue_tag(repo, "SPEC", issue, repo.head.peel(pygit2.Commit).id)


def test_document_versions(tmp_git: Path, spec: Path) -> None:
    _tag(tmp_git, "1")
    editor.set_description(spec, "SPEC-1", "Changed.")
    commit(tmp_git, "change spec")
    _write_object(tmp_git, "OTHER", "OTHER-1", {"type": "Requirement"}, "x")
    commit(tmp_git, "other doc only")
    repo = pygit2.Repository(str(tmp_git))
    repo.branches.local.create("feature", repo.head.peel(pygit2.Commit))

    versions = document_versions(tmp_git, "SPEC")
    assert [v.kind for v in versions[:3]] == ["working-copy", "latest", "issue"]
    assert versions[0].ref == WORKING_COPY
    assert versions[1].ref == LATEST
    assert versions[2].ref == "SPEC-issue-1"
    assert versions[2].label.startswith("Issue 1 (")
    assert "refs/heads/feature" in [v.ref for v in versions if v.kind == "branch"]
    assert "Branch feature" in [v.label for v in versions if v.kind == "branch"]
    # Only commits that changed the document, newest first.
    commits = [v.label.split("  ", 1)[1] for v in versions if v.kind == "commit"]
    assert commits == ["change spec", "seed"]


def test_default_versions_without_an_issue(tmp_git: Path, spec: Path) -> None:
    assert default_versions(tmp_git, "SPEC") == (None, LATEST)


def test_default_versions_compare_the_latest_issue_with_the_latest_commit(
    tmp_git: Path, spec: Path
) -> None:
    _tag(tmp_git, "1")
    editor.set_description(spec, "SPEC-1", "Changed.")
    commit(tmp_git, "change")
    assert default_versions(tmp_git, "SPEC") == ("SPEC-issue-1", LATEST)


def test_default_versions_at_an_issue_compare_it_with_the_one_before(
    tmp_git: Path, spec: Path
) -> None:
    _tag(tmp_git, "1")
    editor.set_description(spec, "SPEC-1", "Changed.")
    commit(tmp_git, "change")
    _tag(tmp_git, "2")
    assert default_versions(tmp_git, "SPEC") == ("SPEC-issue-1", LATEST)
    # A later commit that leaves the document alone changes nothing.
    _write_object(tmp_git, "OTHER", "OTHER-1", {"type": "Requirement"}, "x")
    commit(tmp_git, "other doc only")
    assert default_versions(tmp_git, "SPEC") == ("SPEC-issue-1", LATEST)


def test_default_versions_at_the_only_issue_has_no_from(tmp_git: Path, spec: Path) -> None:
    _tag(tmp_git, "1")
    assert default_versions(tmp_git, "SPEC") == (None, LATEST)


@pytest.mark.parametrize("branch, label", [("refs/heads/shared", "Branch shared"),
                                           ("refs/remotes/origin/shared", "Branch origin/shared")])
def test_branch_offered_beside_a_tag_of_its_name_is_the_branch(tmp_path, branch, label):
    """MELLON-VER-SRS-7: each local and remote-tracking branch is offered. A tag with
    the branch's name, on another commit, doesn't change which commit the branch's
    entry names: comparing at it compares the branch's commit."""
    root = tmp_path / "r"
    _build(root)
    seed, edits = _oid(root, "HEAD~1"), _oid(root, "HEAD")
    repo = pygit2.Repository(str(root))
    repo.references.create(branch, pygit2.Oid(hex=seed))
    repo.references.create(f"refs/tags/{label.removeprefix('Branch ')}", pygit2.Oid(hex=edits))
    entries = [v for v in document_versions(root, "SPEC") if v.label == label]
    assert [v.kind for v in entries] == ["branch"], f"setup: one entry {label!r} expected: {entries}"
    assert compare_document(root, "SPEC", entries[0].ref, LATEST).commit_a == seed


# --- files the loader accepts or rejects ------------------------------------


def test_uppercase_extension_follows_the_loader(tmp_git: Path, spec: Path) -> None:
    """`SPEC-9.XML` is an object where the loader loads it (Windows, where a
    glob ignores case) and not elsewhere; history and the working copy agree."""
    import os

    from mellon import load_document

    text = (spec / "objects" / "SPEC-1.xml").read_text(encoding="utf-8")
    (spec / "objects" / "SPEC-9.XML").write_text(
        text.replace("SPEC-1", "SPEC-9"), encoding="utf-8"
    )
    commit(tmp_git, "upper-case extension")
    loaded = "SPEC-9" in load_document(spec).objects
    assert loaded == (os.name == "nt")
    for ref_b in ("HEAD", WORKING_COPY):
        diff = compare_document(tmp_git, "SPEC", "HEAD~1", ref_b)
        assert _kinds(diff) == ({"SPEC-9": ("Added",)} if loaded else {})
        if loaded:
            assert diff.changes[0].text == "Requirement 1."


def test_unreadable_links_file_is_not_read_as_no_links(tmp_git: Path, spec: Path) -> None:
    link = {"target_doc": "SPEC", "target_id": "SPEC-4", "type": "refines", "ack": "abc1234"}
    _write_links(tmp_git, "SPEC", "SPEC-1", [link])
    commit(tmp_git, "link")
    (spec / "links" / "SPEC-1.xml").write_text("<links source=", encoding="utf-8")
    commit(tmp_git, "break links")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert _kinds(diff) == {"SPEC-1": ("Modified",)}
    change = diff.changes[0]
    assert change.after.obj is not None and change.after.error is None
    assert "links/SPEC-1.xml" in change.after.links_error
    assert change.errors[1] == change.after.links_error
    # Not every link "removed".
    assert change.delta is None or change.delta.links.is_empty


def test_missing_manifest_is_reported_not_silent(tmp_git: Path, spec: Path) -> None:
    (spec / "SPEC.xml").unlink()
    # The working copy: the document is still there, its objects too.
    diff = compare_document(tmp_git, "SPEC", "HEAD", WORKING_COPY)
    assert diff.present_b
    assert diff.changes == ()
    assert diff.outline_error_b == "SPEC/SPEC.xml is missing"
    commit(tmp_git, "drop manifest")
    diff = compare_document(tmp_git, "SPEC", "HEAD~1", "HEAD")
    assert diff.present_b and diff.changes == ()
    assert diff.outline_error_b == "SPEC/SPEC.xml is missing"
    assert diff.outline_error_a is None


def test_unparseable_manifest_is_reported(tmp_git: Path, spec: Path) -> None:
    (spec / "SPEC.xml").write_text("<document id=", encoding="utf-8")
    diff = compare_document(tmp_git, "SPEC", "HEAD", WORKING_COPY)
    assert "XML parse error" in diff.outline_error_b


def test_default_versions_before_the_first_commit(tmp_git: Path) -> None:
    editor.create_document(tmp_git, "SPEC", title="Spec")
    assert default_versions(tmp_git, "SPEC") == (None, WORKING_COPY)


def _in_place_by_definition(kept_a: list[str], kept_b: list[str]) -> set[str]:
    """MELLON-VER-SRS-23 worked out the long way: every set of siblings whose
    order is the same at both versions; the largest; of those, the one whose
    From positions, in order, come first."""
    from itertools import combinations

    at_b = {k: i for i, k in enumerate(kept_b)}
    for size in range(len(kept_a), 0, -1):
        for picked in combinations(range(len(kept_a)), size):  # in order, smallest first
            order_b = [at_b[kept_a[i]] for i in picked]
            if order_b == sorted(order_b):
                return {kept_a[i] for i in picked}
    return set()


def test_siblings_left_in_place_follow_the_definition_for_every_order() -> None:
    """MELLON-VER-SRS-23: for every order of up to 7 siblings, the ones not
    Moved are the largest same-order set, and on a tie the later at From is
    Moved. Regression: the old matcher kept the longest unbroken run, so
    A B C D -> B D C A moved three."""
    from itertools import permutations

    from mellon.diff import _in_place

    for n in range(2, 8):
        kept_a = [f"S-{i}" for i in range(n)]
        for kept_b in permutations(kept_a):
            expected = _in_place_by_definition(kept_a, list(kept_b))
            got = _in_place(kept_a, list(kept_b))
            assert got == expected, f"{kept_a} -> {list(kept_b)}: kept {sorted(got)}, expected {sorted(expected)}"


# --- written blind from MELLON-VER-SRS (round 1) -----------------------------
#
# Written from MELLON-VER-SRS, the definitions and the public signatures alone,
# without reading the code under test or the existing tests. Every expected
# value is worked out by hand from a requirement or a definition; the docstring
# of each test names the requirement it checks. Each test builds its own git
# repository under tmp_path with fixed commit times, so a run repeats exactly.


SVG_2 = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    b'<circle cx="10" cy="5" r="4" fill="blue"/></svg>\n'
)

BROKEN_OBJECT_1 = DECL + b'<object id="SPEC-1">\n  <description>Broken text\n'


# --------------------------------------------------------------------------
# Repository helpers
# --------------------------------------------------------------------------


def compare(r: GitRepo, ref_a: str, ref_b: str, doc_id: str = "SPEC") -> diff.DocumentDiff:
    return diff.compare_document(r.root, doc_id, ref_a, ref_b)


def change_for(d: diff.DocumentDiff, obj_id: str) -> diff.ObjectChange | None:
    found = [c for c in d.changes if c.obj_id == obj_id]
    assert len(found) <= 1, f"{obj_id} is listed {len(found)} times: {found}"
    return found[0] if found else None


def kinds_by_object(d: diff.DocumentDiff) -> dict[str, tuple[Change, ...]]:
    return {c.obj_id: tuple(c.kinds) for c in d.changes}


def moved_ids(d: diff.DocumentDiff) -> set[str]:
    return {c.obj_id for c in d.changes if Change.MOVED in c.kinds}


def not_well_formed(data: bytes) -> bool:
    try:
        etree.fromstring(data)
    except etree.XMLSyntaxError:
        return True
    return False


# --------------------------------------------------------------------------
# MELLON-VER-SRS-12, -13, -14: the versions the Compare dialog opens with
# --------------------------------------------------------------------------


def build_issue_repo(tmp_path: Path, n_commits: int) -> tuple[GitRepo, Path, list[str]]:
    """Commits c1..cn, each changing SPEC-0's description. OTHER is created in c1."""
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    obj = editor.create_object(f, None, "Version 1.")
    other = editor.create_document(r.root, "OTHER", title="Other")
    editor.create_object(other, None, "Other 1.")
    commits = [r.commit("c1")]
    for i in range(2, n_commits + 1):
        editor.set_description(f, obj, f"Version {i}.")
        commits.append(r.commit(f"c{i}"))
    return r, f, commits


def defaults(r: GitRepo) -> tuple[str | None, str | None, str | None]:
    """default_versions for SPEC as (from_ref, commit From names, whether To is the latest commit)."""
    from_ref, to_ref = diff.default_versions(r.root, "SPEC")
    from_commit = r.commit_of(from_ref) if from_ref is not None else None
    to_is_latest = to_ref != diff.WORKING_COPY and r.commit_of(to_ref) == str(r.repo.head.target)
    return from_ref, from_commit, to_is_latest


def test_ver12_from_is_current_issue_in_issue_order(tmp_path):
    """MELLON-VER-SRS-12: changed since its current issue, From = current issue, To = latest.

    Issues 1 on c1, 2.9 on c2, 2.10 on c3; c4 changes SPEC. In issue order 2.9
    comes before 2.10, so the current issue is 2.10 (on c3).
    """
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "1", c[0])
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    from_ref, from_commit, to_is_latest = defaults(r)
    assert (from_commit, to_is_latest) == (c[2], True), (
        f"expected From = SPEC-issue-2.10 ({c[2][:7]}) and To = latest commit, "
        f"got From {from_ref!r} ({(from_commit or '')[:7]}), To latest: {to_is_latest}"
    )


def test_ver12_issue_off_the_latest_commits_history_is_ignored(tmp_path):
    """MELLON-VER-SRS-12 (definition 'current issue'): an issue tag on a commit that isn't
    an ancestor of the latest commit isn't the current issue, even when it is higher."""
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "1", c[0])
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    side = r.side_commit("side", c[2], "SPEC/objects/SPEC-0.xml",
                         DECL + b'<object id="SPEC-0">\n  <description>Side.</description>\n</object>\n',
                         "side change")
    r.tag_issue("SPEC", "3", side)
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[2], (
        f"From should stay SPEC-issue-2.10 ({c[2][:7]}); SPEC-issue-3 is on a side branch. Got {from_ref!r}"
    )


def test_ver12_another_documents_issue_is_ignored(tmp_path):
    """MELLON-VER-SRS-12: OTHER-issue-5 on the latest commit isn't an issue of SPEC."""
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "1", c[0])
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    r.tag_issue("OTHER", "5", c[3])
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[2], (
        f"From should stay SPEC-issue-2.10 ({c[2][:7]}), not OTHER's issue; got {from_ref!r}"
    )


def test_ver12_shorter_issue_number_comes_first(tmp_path):
    """MELLON-VER-SRS-12 (definition 'issue order'): 2 comes before 2.0, so with issues
    2 on c1 and 2.0 on c2 and SPEC changed since, From = SPEC-issue-2.0."""
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "2", c[0])
    r.tag_issue("SPEC", "2.0", c[1])
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[1], f"From should be SPEC-issue-2.0 ({c[1][:7]}), got {from_ref!r}"


def test_ver13_unchanged_since_current_issue_from_is_issue_before(tmp_path):
    """MELLON-VER-SRS-13: issues 2.9 on c2 and 2.10 on the latest commit, SPEC unchanged
    since 2.10: From = the issue before 2.10, which is 2.9; To = latest commit."""
    r, _, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    from_ref, from_commit, to_is_latest = defaults(r)
    assert (from_commit, to_is_latest) == (c[1], True), (
        f"expected From = SPEC-issue-2.9 ({c[1][:7]}) and To = latest commit, "
        f"got From {from_ref!r}, To latest: {to_is_latest}"
    )


def test_ver13_issue_before_ignores_issue_off_the_history(tmp_path):
    """MELLON-VER-SRS-13 (definition 'issue before'): SPEC-issue-2.9.1 on a side-branch
    commit comes between 2.9 and 2.10 in issue order but isn't an ancestor of the
    latest commit, so From stays 2.9."""
    r, _, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    side = r.side_commit("side", c[1], "SPEC/objects/SPEC-0.xml",
                         DECL + b'<object id="SPEC-0">\n  <description>Side.</description>\n</object>\n',
                         "side change")
    r.tag_issue("SPEC", "2.9.1", side)
    r.tag_issue("SPEC", "2.10", c[2])
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[1], f"From should stay SPEC-issue-2.9 ({c[1][:7]}), got {from_ref!r}"


def test_ver13_change_then_byte_for_byte_restore_is_unchanged(tmp_path):
    """MELLON-VER-SRS-13 (definition 'changed since an issue'): after 2.10, a commit
    changing SPEC-0 and a commit restoring its file byte for byte leave SPEC unchanged
    since 2.10, so From = 2.9 and To = latest commit."""
    r, f, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    original = r.read("SPEC/objects/SPEC-0.xml")
    editor.set_description(f, "SPEC-0", "Changed in between.")
    r.commit("c4 change")
    r.write("SPEC/objects/SPEC-0.xml", original)
    r.commit("c5 restore")
    from_ref, from_commit, to_is_latest = defaults(r)
    assert (from_commit, to_is_latest) == (c[1], True), (
        f"expected From = SPEC-issue-2.9 ({c[1][:7]}), To = latest; got From {from_ref!r}, To latest: {to_is_latest}"
    )


def test_ver13_commit_to_another_document_leaves_spec_unchanged(tmp_path):
    """MELLON-VER-SRS-13 (rationale: a commit to another document doesn't change this
    one): after 2.10 only OTHER changes, so From = 2.9."""
    r, _, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    editor.set_description(r.root / "OTHER", "OTHER-0", "Other changed.")
    r.commit("c4 other only")
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[1], f"From should be SPEC-issue-2.9 ({c[1][:7]}), got {from_ref!r}"


def test_ver14_only_issue_on_unchanged_document_leaves_from_unset(tmp_path):
    """MELLON-VER-SRS-14 (boundary of -13): the only issue is the one the document still
    is, so there is no issue before; From is unset and To is the latest commit."""
    r, _, c = build_issue_repo(tmp_path, 2)
    r.tag_issue("SPEC", "1", c[1])
    from_ref, _, to_is_latest = defaults(r)
    assert (from_ref, to_is_latest) == (None, True), (
        f"expected From unset and To = latest commit, got From {from_ref!r}, To latest: {to_is_latest}"
    )


# --------------------------------------------------------------------------
# MELLON-VER-SRS-19: Added follows the object file, not the outline
# --------------------------------------------------------------------------


def test_ver19_object_file_without_outline_entry_is_added(tmp_path):
    """MELLON-VER-SRS-19 (definition 'doesn't exist at a version'): an object file
    SPEC-9.xml committed with no outline entry exists at To, so SPEC-9 is Added."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-9.xml",
            DECL + b'<object id="SPEC-9">\n  <attr name="type">Requirement</attr>\n'
                   b'  <description>Orphan.</description>\n</object>\n')
    c2 = r.commit("orphan object file")
    change = change_for(compare(r, c1, c2), "SPEC-9")
    assert change is not None and tuple(change.kinds) == (Change.ADDED,), (
        f"SPEC-9 should be exactly Added, got {change and change.kinds}"
    )


def test_ver19_outline_entry_without_object_file_is_not_added(tmp_path):
    """MELLON-VER-SRS-19 (definition 'doesn't exist at a version'): an outline entry for
    SPEC-10 with no object file means SPEC-10 doesn't exist at To, so it isn't Added."""
    r, _, c1 = build_spec(tmp_path)
    manifest = r.read("SPEC/SPEC.xml")
    assert b"  </outline>" in manifest, "setup: manifest outline not found"
    r.write("SPEC/SPEC.xml", manifest.replace(b"  </outline>", b'    <ref id="SPEC-10"/>\n  </outline>'))
    c2 = r.commit("dangling outline entry")
    change = change_for(compare(r, c1, c2), "SPEC-10")
    assert change is None or Change.ADDED not in change.kinds, (
        f"SPEC-10 has no object file at To and mustn't be Added, got {change.kinds}"
    )


@pytest.mark.parametrize("tail", ["²", "1" * 4301], ids=["superscript-two", "4301-digits"])
def test_object_id_without_a_number_is_added(tmp_path, tail):
    """MELLON-VER-SRS-19: an object file committed with no outline entry, whose ID
    ends in something that isn't a whole number of ASCII digits (a superscript two,
    or more digits than a number is read from), exists at To only: it is Added."""
    r, _, c1 = build_spec(tmp_path)
    obj_id = f"SPEC-{tail}"
    data = (DECL + f'<object id="{obj_id}">\n  <attr name="type">Requirement</attr>\n'
                   f"  <description>No number.</description>\n</object>\n".encode("utf-8"))
    c2 = r.side_commit("odd-id", c1, f"SPEC/objects/{obj_id}.xml", data, "object ID without a number")
    assert kinds_by_object(compare(r, c1, c2)) == {obj_id: (Change.ADDED,)}


# --------------------------------------------------------------------------
# MELLON-VER-SRS-21: each field alone makes an object Modified
# --------------------------------------------------------------------------


def _modified_only(r: GitRepo, c1: str, c2: str, obj_id: str) -> diff.ObjectChange:
    change = change_for(compare(r, c1, c2), obj_id)
    assert change is not None, f"{obj_id} isn't listed as changed"
    assert tuple(change.kinds) == (Change.MODIFIED,), f"{obj_id} should be exactly Modified, got {change.kinds}"
    assert change.delta is not None, f"{obj_id} is Modified but carries no field changes"
    return change


def test_ver21_attribute_value_changed(tmp_path):
    """MELLON-VER-SRS-21: status Draft -> Approved makes SPEC-1 Modified, reporting the attribute."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_attribute(f, "SPEC-1", "status", "Approved")
    c2 = r.commit("approve")
    change = _modified_only(r, c1, c2, "SPEC-1")
    attrs = {(a.name, a.value_a, a.value_b) for a in change.delta.attrs}
    assert ("status", "Draft", "Approved") in attrs, f"status Draft -> Approved not reported: {attrs}"


def test_ver21_attribute_unset_at_to(tmp_path):
    """MELLON-VER-SRS-21: the rationale attribute removed from SPEC-1's file (not set at
    To) makes SPEC-1 Modified, with rationale 'why' -> not set."""
    r, _, c1 = build_spec(tmp_path)
    data = r.read("SPEC/objects/SPEC-1.xml")
    line = b'  <attr name="rationale">why</attr>\n'
    assert line in data, "setup: rationale line not found"
    r.write("SPEC/objects/SPEC-1.xml", data.replace(line, b""))
    c2 = r.commit("unset rationale")
    change = _modified_only(r, c1, c2, "SPEC-1")
    attrs = {(a.name, a.value_a, a.value_b) for a in change.delta.attrs}
    assert ("rationale", "why", None) in attrs, f"rationale 'why' -> not set not reported: {attrs}"


def test_ver21_link_added(tmp_path):
    """MELLON-VER-SRS-21: a new outgoing link SPEC-1 -refines-> SPEC-4 makes SPEC-1 Modified."""
    r, f, c1 = build_spec(tmp_path)
    editor.add_link(f, "SPEC-1", "SPEC", "SPEC-4", "refines")
    c2 = r.commit("add link")
    change = _modified_only(r, c1, c2, "SPEC-1")
    added = {(lk.target_doc, lk.target_id, lk.type) for lk in change.delta.links.added}
    assert added == {("SPEC", "SPEC-4", "refines")}, f"added links reported: {added}"


def test_ver21_link_removed(tmp_path):
    """MELLON-VER-SRS-21: removing the link SPEC-1 -satisfies-> SPEC-2 makes SPEC-1 Modified."""
    r, f, c1 = build_spec(tmp_path)
    assert editor.remove_link(f, "SPEC-1", "SPEC", "SPEC-2"), "setup: link not removed"
    c2 = r.commit("remove link")
    change = _modified_only(r, c1, c2, "SPEC-1")
    removed = {(lk.target_doc, lk.target_id, lk.type) for lk in change.delta.links.removed}
    assert removed == {("SPEC", "SPEC-2", "satisfies")}, f"removed links reported: {removed}"


def test_ver21_link_type_changed_same_target(tmp_path):
    """MELLON-VER-SRS-21 (definition 'field': the outgoing links, each with its target and
    link type): satisfies -> refines to the same target, same ack, is Modified, not
    Link reviewed, and the old and the new link type are both reported."""
    r, _, c1 = build_spec(tmp_path)
    data = r.read("SPEC/links/SPEC-1.xml")
    assert data.count(b'type="satisfies"') == 1, "setup: link type not found"
    r.write("SPEC/links/SPEC-1.xml", data.replace(b'type="satisfies"', b'type="refines"'))
    c2 = r.commit("change link type")
    change = _modified_only(r, c1, c2, "SPEC-1")
    links = change.delta.links
    seen = ({(lk.target_id, lk.type) for lk in links.removed}, {(lk.target_id, lk.type) for lk in links.added})
    assert seen == ({("SPEC-2", "satisfies")}, {("SPEC-2", "refines")}), (
        f"expected removed satisfies and added refines to SPEC-2, got removed/added {seen}"
    )


def test_ver21_table_cell_changed(tmp_path):
    """MELLON-VER-SRS-21: one table cell 10 -> 20, with a table at both, makes SPEC-3 Modified."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_table(f, "SPEC-3", _table("20"))
    c2 = r.commit("table cell")
    change = _modified_only(r, c1, c2, "SPEC-3")
    t = change.delta.table
    assert t is not None and (t.table_a, t.table_b) == (_table("10"), _table("20")), (
        f"table change not reported as (speed,10) -> (speed,20): {t}"
    )


def test_ver21_table_removed(tmp_path):
    """MELLON-VER-SRS-21: removing SPEC-3's table makes it Modified, with no table at To."""
    r, f, c1 = build_spec(tmp_path)
    assert editor.clear_table(f, "SPEC-3"), "setup: table not removed"
    c2 = r.commit("table removed")
    change = _modified_only(r, c1, c2, "SPEC-3")
    t = change.delta.table
    assert t is not None and (t.table_a, t.table_b) == (_table("10"), None), f"table removal not reported: {t}"


def test_ver21_figure_replaced(tmp_path):
    """MELLON-VER-SRS-21: replacing SPEC-4's figure by a different SVG makes it Modified."""
    r, f, c1 = build_spec(tmp_path)
    svg = tmp_path / "fig2.svg"
    svg.write_bytes(SVG_2)
    editor.set_figure(f, "SPEC-4", svg)
    c2 = r.commit("figure replaced")
    change = _modified_only(r, c1, c2, "SPEC-4")
    fig = change.delta.figure
    assert fig is not None and (fig.svg_a, fig.svg_b) == (SVG_1, SVG_2), f"figure change not reported: {fig}"


def test_ver21_figure_removed(tmp_path):
    """MELLON-VER-SRS-21: removing SPEC-4's figure makes it Modified, with no figure at To."""
    r, f, c1 = build_spec(tmp_path)
    assert editor.clear_figure(f, "SPEC-4"), "setup: figure not removed"
    c2 = r.commit("figure removed")
    change = _modified_only(r, c1, c2, "SPEC-4")
    fig = change.delta.figure
    assert fig is not None and (fig.svg_a, fig.svg_b) == (SVG_1, None), f"figure removal not reported: {fig}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-22: line endings, indentation and attribute order are no change
# --------------------------------------------------------------------------


def _reserialize(data: bytes, indent: str, reorder=None) -> bytes:
    """`data` re-indented with `indent`, with each element's XML attributes reordered by
    `reorder(element)`; the declaration and the text inside elements are kept."""
    root = etree.fromstring(data)
    if reorder is not None:
        for el in root.iter():
            items = reorder(el, list(el.attrib.items()))
            for key in list(el.attrib):
                del el.attrib[key]
            for key, value in items:
                el.set(key, value)
    etree.indent(root, space=indent)
    return DECL + etree.tostring(root, encoding="UTF-8", xml_declaration=False) + b"\n"


@pytest.mark.parametrize("indent", ["    ", "\t"], ids=["four-spaces", "tab"])
def test_ver22_reindented_object_files_are_no_change(tmp_path, indent):
    """MELLON-VER-SRS-22: every object file of SPEC re-indented (4 spaces, or a tab), the
    text inside elements unchanged: no object changed and none Moved."""
    r, _, c1 = build_spec(tmp_path)
    for path in sorted((r.root / "SPEC" / "objects").glob("*.xml")):
        old = path.read_bytes()
        new = _reserialize(old, indent)
        assert new != old, f"setup: {path.name} unchanged by re-indenting"
        path.write_bytes(new)
    c2 = r.commit("re-indent objects")
    d = compare(r, c1, c2)
    assert d.changes == (), f"re-indenting must be no change, got {kinds_by_object(d)}"


def test_ver22_reordered_link_attributes_are_no_change(tmp_path):
    """MELLON-VER-SRS-22: a <link> with its XML attributes in the order type, target-id,
    target-doc, ack is no change."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", c1[:7])]))
    c2 = r.commit("explicit ack")
    order = ["type", "target-id", "target-doc", "ack"]
    old = r.read("SPEC/links/SPEC-1.xml")
    new = _reserialize(old, "  ", lambda el, items: sorted(items, key=lambda kv: order.index(kv[0]))
                       if el.tag == "link" else items)
    assert b'<link type="satisfies" target-id="SPEC-2" target-doc="SPEC" ack=' in new, "setup: not reordered"
    r.write("SPEC/links/SPEC-1.xml", new)
    c3 = r.commit("reorder link attributes")
    d = compare(r, c2, c3)
    assert d.changes == (), f"reordering XML attributes must be no change, got {kinds_by_object(d)}"


def test_ver22_reindented_manifest_with_reordered_attributes_is_no_change(tmp_path):
    """MELLON-VER-SRS-22: the manifest re-indented with 4 spaces and the XML attributes of
    each <attribute> element reversed: no object changed and none Moved. (A <ref> has
    only its id attribute, so there is nothing to reorder on it.)"""
    r, _, c1 = build_spec(tmp_path)
    old = r.read("SPEC/SPEC.xml")
    new = _reserialize(old, "    ", lambda el, items: list(reversed(items)) if el.tag == "attribute" else items)
    assert b'<attribute label="Status" required="false" kind="enum" name="status">' in new, "setup: not reordered"
    r.write("SPEC/SPEC.xml", new)
    c2 = r.commit("re-indent manifest")
    d = compare(r, c1, c2)
    assert d.changes == (), f"re-indenting the manifest must be no change, got {kinds_by_object(d)}"


@pytest.mark.parametrize(
    "rel", ["SPEC/objects/SPEC-1.xml", "SPEC/links/SPEC-1.xml", "SPEC/SPEC.xml"],
    ids=["object-file", "links-file", "manifest"],
)
def test_ver22_crlf_line_endings_are_no_change(tmp_path, rel):
    """MELLON-VER-SRS-22: a file committed with CRLF line endings instead of LF is no change."""
    r, _, c1 = build_spec(tmp_path)
    r.repo.config["core.autocrlf"] = "false"
    data = r.read(rel)
    assert b"\r\n" not in data, "setup: file already has CRLF"
    r.write(rel, data.replace(b"\n", b"\r\n"))
    c2 = r.commit("CRLF")
    blob = r.repo.revparse_single(f"{c2}:{rel}")
    assert b"\r\n" in blob.data, "setup: git didn't keep the CRLF line endings"
    d = compare(r, c1, c2)
    assert d.changes == (), f"CRLF line endings in {rel} must be no change, got {kinds_by_object(d)}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-23: Moved
# --------------------------------------------------------------------------


def test_ver23_swapped_siblings_only_the_later_at_from_is_moved(tmp_path):
    """MELLON-VER-SRS-23 (definition 'Moved', tie rule): under SPEC-0, SPEC-1 SPEC-2 SPEC-3
    becomes SPEC-2 SPEC-1 SPEC-3. The largest same-order sets {1,3} and {2,3} are equally
    large, so SPEC-2, the one that comes later at From, is the only one Moved."""
    r, f, c1 = build_spec(tmp_path)
    editor.move_object(f, "SPEC-2", before="SPEC-1")
    c2 = r.commit("swap")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-2"}, f"expected only SPEC-2 Moved, got {sorted(moved_ids(d))}"


def test_ver23_tie_between_pairs_moves_the_later_pair(tmp_path):
    """MELLON-VER-SRS-23 (definition 'Moved', tie rule): A B C D -> C D A B. The largest
    same-order sets {A,B} and {C,D} are equally large; C and D come later at From, so
    exactly C and D are Moved."""
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    head = editor.create_object(f, None, "Heading")
    kids = [editor.create_object(f, head, f"Item {i}.") for i in range(4)]
    assert kids == ["SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"], f"setup: unexpected ids {kids}"
    c1 = r.commit("seed")
    editor.move_object(f, "SPEC-3", before="SPEC-1")
    editor.move_object(f, "SPEC-4", before="SPEC-1")
    c2 = r.commit("rotate")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-3", "SPEC-4"}, f"expected SPEC-3 and SPEC-4 Moved, got {sorted(moved_ids(d))}"


def test_ver23_largest_same_order_set_is_kept(tmp_path):
    """MELLON-VER-SRS-23 (definition 'Moved'): A B C D -> B D C A. The same-order pairs
    are {B,C} and {B,D}; no three keep their order, and A pairs with none. So two
    siblings stay; of the tie, D comes later at From and is Moved: exactly A and D."""
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    head = editor.create_object(f, None, "Heading")
    kids = [editor.create_object(f, head, f"Item {i}.") for i in range(4)]
    assert kids == ["SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"], f"setup: unexpected ids {kids}"
    c1 = r.commit("seed")
    editor.move_object(f, "SPEC-1", after="SPEC-4")
    editor.move_object(f, "SPEC-4", before="SPEC-3")
    c2 = r.commit("reorder")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-1", "SPEC-4"}, f"expected SPEC-1 and SPEC-4 Moved, got {sorted(moved_ids(d))}"


def test_ver23_heading_moved_with_sub_objects_moves_only_the_heading(tmp_path):
    """MELLON-VER-SRS-23: SPEC-0 moved, with SPEC-1..3 below it, under SPEC-4. SPEC-0's
    parent changed; its sub-objects keep their parent and order: only SPEC-0 is Moved."""
    r, f, c1 = build_spec(tmp_path)
    editor.move_object(f, "SPEC-0", parent_id="SPEC-4")
    c2 = r.commit("move heading")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-0"}, f"expected only SPEC-0 Moved, got {sorted(moved_ids(d))}"


def test_ver23_moved_together_with_link_reviewed(tmp_path):
    """MELLON-VER-SRS-23 (Moved goes with Link reviewed): SPEC-2 moved to the top level and
    only its link's acknowledged commit changed: its changes are exactly Moved and
    Link reviewed."""
    r, f, c1 = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-2.xml", links_xml("SPEC-2", [("SPEC", "SPEC-4", "satisfies", c1[:7])]))
    c2 = r.commit("link on SPEC-2")
    editor.move_object(f, "SPEC-2", to_top=True)
    r.write("SPEC/links/SPEC-2.xml", links_xml("SPEC-2", [("SPEC", "SPEC-4", "satisfies", c2[:7])]))
    c3 = r.commit("move and review")
    change = change_for(compare(r, c2, c3), "SPEC-2")
    kinds = tuple(change.kinds) if change else ()
    assert sorted(kinds) == sorted((Change.MOVED, Change.LINK_REVIEWED)), (
        f"SPEC-2 should be exactly Moved and Link reviewed, got {kinds}"
    )


# Generated inputs (T11): many cases of up to 8 siblings, each under its own heading
# of one document, permuted, with some siblings deleted and some inserted.

GEN_CASES = 300
GEN_SEED = 20261106


def _lcs_len(xs: list[str], ys: list[str]) -> int:
    prev = [0] * (len(ys) + 1)
    for x in xs:
        cur = [0]
        for j, y in enumerate(ys):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def _gen_manifest(outline: list[tuple[str, list[str]]]) -> bytes:
    lines = [
        '<document id="GEN">', "  <metadata>", "    <title>Generated</title>", "  </metadata>",
        "  <attributes>", '    <attribute name="type" kind="enum" required="true">',
        "      <option>Heading</option>", "      <option>Information</option>", "    </attribute>",
        "  </attributes>", "  <outline>",
    ]
    for head, kids in outline:
        if kids:
            lines.append(f'    <ref id="{head}">')
            lines += [f'      <ref id="{k}"/>' for k in kids]
            lines.append("    </ref>")
        else:
            lines.append(f'    <ref id="{head}"/>')
    lines += ["  </outline>", "</document>", ""]
    return DECL + "\n".join(lines).encode("utf-8")


def _gen_object(obj_id: str, kind: str) -> bytes:
    return DECL + (
        f'<object id="{obj_id}">\n  <attr name="type">{kind}</attr>\n'
        f"  <description>Text of {obj_id}.</description>\n</object>\n"
    ).encode("utf-8")


@pytest.fixture(scope="module")
def generated_moves(tmp_path_factory):
    """Returns (cases, kinds by object). Each case is a dict with the heading, the
    siblings at From and at To, the kept siblings in From and in To order, and whether
    the kept siblings were left in order (only insertions and deletions)."""
    rng = random.Random(GEN_SEED)
    cases = []
    next_id = 1000
    for k in range(GEN_CASES):
        n = rng.randint(1, 8)
        before = [f"GEN-{next_id + i}" for i in range(n)]
        next_id += n
        kept = [x for x in before if rng.random() >= 0.2]
        in_order = rng.random() < 0.25
        kept_to = kept[:]
        if not in_order:
            rng.shuffle(kept_to)
        inserted = [f"GEN-{next_id + i}" for i in range(rng.randint(0, 3))]
        next_id += len(inserted)
        after = kept_to[:]
        for x in inserted:
            after.insert(rng.randint(0, len(after)), x)
        cases.append({"head": f"GEN-{k}", "from": before, "to": after,
                      "kept_from": kept, "kept_to": kept_to, "in_order": in_order})

    def files(side: str) -> dict[str, bytes]:
        out = {"GEN/GEN.xml": _gen_manifest([(c["head"], c[side]) for c in cases])}
        for c in cases:
            out[f"GEN/objects/{c['head']}.xml"] = _gen_object(c["head"], "Heading")
            for x in c[side]:
                out[f"GEN/objects/{x}.xml"] = _gen_object(x, "Information")
        return out

    r = GitRepo(tmp_path_factory.mktemp("generated") / "repo")
    c1, c2 = r.commit_snapshots([("from", files("from")), ("to", files("to"))])
    in_tree = {e.name for e in r.repo[pygit2.Oid(hex=c2)].peel(pygit2.Commit).tree["GEN"]["objects"]}
    expected = {f"{x}.xml" for c in cases for x in [c["head"], *c["to"]]}
    assert in_tree == expected, "setup: the To commit doesn't hold the expected object files"
    return cases, kinds_by_object(compare(r, c1, c2, doc_id="GEN"))


def test_ver23_generated_moved_count_is_common_minus_lcs(generated_moves):
    """MELLON-VER-SRS-23 (definition 'Moved'), generated: in each case the number of Moved
    siblings is the number present at both versions minus the length of their longest
    common subsequence (the largest set whose order is the same at From and at To)."""
    cases, kinds = generated_moves
    bad = []
    for c in cases:
        moved = [x for x in c["kept_from"] if Change.MOVED in kinds.get(x, ())]
        expected = len(c["kept_from"]) - _lcs_len(c["kept_from"], c["kept_to"])
        if len(moved) != expected:
            bad.append(f"{c['from']} -> {c['to']}: Moved {moved}, expected {expected} Moved")
    assert not bad, f"{len(bad)} of {len(cases)} cases wrong, e.g.:\n" + "\n".join(bad[:5])


def test_ver23_generated_unmoved_siblings_keep_their_order(generated_moves):
    """MELLON-VER-SRS-23 (definition 'Moved'), generated: the siblings present at both
    versions that aren't Moved are in the same order at From and at To."""
    cases, kinds = generated_moves
    bad = []
    for c in cases:
        stay_from = [x for x in c["kept_from"] if Change.MOVED not in kinds.get(x, ())]
        stay_to = [x for x in c["kept_to"] if Change.MOVED not in kinds.get(x, ())]
        if stay_from != stay_to:
            bad.append(f"{c['from']} -> {c['to']}: not Moved {stay_from} at From but {stay_to} at To")
    assert not bad, f"{len(bad)} of {len(cases)} cases wrong, e.g.:\n" + "\n".join(bad[:5])


def test_ver23_generated_insertions_and_deletions_move_nothing(generated_moves):
    """MELLON-VER-SRS-23 (definition: inserting or deleting a sibling moves nothing),
    generated: in the cases with only insertions and deletions, nothing is Moved."""
    cases, kinds = generated_moves
    only = [c for c in cases if c["in_order"]]
    assert len(only) >= 30, f"setup: only {len(only)} insertion/deletion cases"
    bad = []
    for c in only:
        moved = sorted(x for x in set(c["from"]) | set(c["to"]) if Change.MOVED in kinds.get(x, ()))
        if moved:
            bad.append(f"{c['from']} -> {c['to']}: Moved {moved}")
    assert not bad, f"{len(bad)} of {len(only)} cases wrong, e.g.:\n" + "\n".join(bad[:5])


# --------------------------------------------------------------------------
# MELLON-VER-SRS-24: Link reviewed
# --------------------------------------------------------------------------


def test_ver24_first_acknowledgement_is_link_reviewed(tmp_path):
    """MELLON-VER-SRS-24: a link with no acknowledged commit at From that gets one at To,
    nothing else changed: SPEC-1 is exactly Link reviewed."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-4", "refines", None)]))
    c2 = r.commit("no ack")
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-4", "refines", c2[:7])]))
    c3 = r.commit("first ack")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.LINK_REVIEWED,), (
        f"SPEC-1 should be exactly Link reviewed, got {change and change.kinds}"
    )


def test_ver24_ack_and_description_changed_together_is_modified(tmp_path):
    """MELLON-VER-SRS-24 (definition 'Link reviewed': its ONLY change is to the acks):
    the ack and the description changed in one commit: SPEC-1 is exactly Modified."""
    r, f, _ = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", "1111111")]))
    c2 = r.commit("ack 1")
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", c2[:7])]))
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    c3 = r.commit("ack 2 and text")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.MODIFIED,), (
        f"SPEC-1 should be exactly Modified, got {change and change.kinds}"
    )


def test_ver24_two_links_reacknowledged_is_one_link_reviewed_change(tmp_path):
    """MELLON-VER-SRS-24 (and -53's data): both of SPEC-1's links re-acknowledged: one
    Link reviewed change for SPEC-1 that lists both links with their ack at From and To."""
    r, _, _ = build_spec(tmp_path)
    links_a = [("SPEC", "SPEC-2", "satisfies", "1111111"), ("SPEC", "SPEC-4", "refines", "2222222")]
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", links_a))
    c2 = r.commit("acks at From")
    links_b = [("SPEC", "SPEC-2", "satisfies", c2[:7]), ("SPEC", "SPEC-4", "refines", c2[:7])]
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", links_b))
    c3 = r.commit("acks at To")
    d = compare(r, c2, c3)
    change = change_for(d, "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.LINK_REVIEWED,), (
        f"SPEC-1 should be listed once, exactly Link reviewed, got {change and change.kinds}"
    )
    acks = {(a.target_doc, a.target_id, a.type, a.ack_a, a.ack_b) for a in change.delta.links.ack_changed}
    assert acks == {("SPEC", "SPEC-2", "satisfies", "1111111", c2[:7]),
                    ("SPEC", "SPEC-4", "refines", "2222222", c2[:7])}, f"reviewed links listed: {acks}"


def test_ack_change_beside_an_unchanged_unreadable_object_file_is_link_reviewed(tmp_path):
    """MELLON-VER-SRS-24 with -27: SPEC-1's object file is broken with the same bytes at
    both versions, so it isn't a change; its link's acknowledged commit changes. That
    is SPEC-1's only change: exactly Link reviewed, and not reported unreadable."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", "1111111")]))
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("ack 1, SPEC-1 broken")
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", c2[:7])]))
    c3 = r.commit("ack 2")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    got = (tuple(change.kinds), change.errors) if change else None
    assert got == ((Change.LINK_REVIEWED,), (None, None)), f"SPEC-1 (kinds, errors): {got}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-26: document at neither version
# --------------------------------------------------------------------------


def test_ver26_error_names_document_and_both_hashes(tmp_path):
    """MELLON-VER-SRS-26: SPEC between c1 and c2 (given as 7-hex hashes) in a repository
    holding only OTHER: an error naming SPEC and both versions as given."""
    r, c1, c2 = build_other_only(tmp_path)
    with pytest.raises(Exception) as info:
        compare(r, c1[:7], c2[:7])
    message = str(info.value)
    missing = [w for w in ("SPEC", c1[:7], c2[:7]) if w not in message]
    assert not missing, f"error {message!r} doesn't name {missing}"


def test_ver26_error_names_document_and_both_names(tmp_path):
    """MELLON-VER-SRS-26: SPEC between the tag 'rel-a' and the branch 'next' in a
    repository holding only OTHER: an error naming SPEC, 'rel-a' and 'next'."""
    r, c1, c2 = build_other_only(tmp_path)
    repo = r.repo
    git_ops.create_tag(repo, "rel-a", pygit2.Oid(hex=c1), "release a")
    repo.branches.local.create("next", repo[pygit2.Oid(hex=c2)])
    with pytest.raises(Exception) as info:
        compare(r, "rel-a", "next")
    message = str(info.value)
    missing = [w for w in ("SPEC", "rel-a", "next") if w not in message]
    assert not missing, f"error {message!r} doesn't name {missing}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-27, -28: unreadable files
# --------------------------------------------------------------------------


def build_broken_at_from(tmp_path: Path) -> tuple[GitRepo, Path, str, str]:
    """c2: SPEC-1's object file not well-formed. c3: SPEC-1 valid again with the text
    'Fixed text.', and SPEC-2's description changed to 'Requirement 2, changed.'."""
    r, f, _ = build_spec(tmp_path)
    good = r.read("SPEC/objects/SPEC-1.xml")
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("break SPEC-1")
    r.write("SPEC/objects/SPEC-1.xml", good)
    editor.set_description(f, "SPEC-1", "Fixed text.")
    editor.set_description(f, "SPEC-2", "Requirement 2, changed.")
    c3 = r.commit("fix SPEC-1, change SPEC-2")
    return r, f, c2, c3


def test_ver27_object_file_broken_at_from_is_unreadable_at_from(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1's file isn't well-formed at From: SPEC-1 is reported
    unreadable at From and readable at To."""
    r, _, c2, c3 = build_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None, "SPEC-1, unreadable at From, isn't listed"
    err_a, err_b = change.errors
    assert (err_a is not None, err_b is None) == (True, True), (
        f"expected unreadable at From only, got errors {change.errors}"
    )


def test_ver27_object_broken_at_from_has_its_fields_at_to(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1, unreadable at From, still has its fields at To."""
    r, _, c2, c3 = build_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-1")
    obj = change.after.obj if change and change.after else None
    assert obj is not None and obj.description == "Fixed text.", f"SPEC-1's fields at To not available: {obj}"


def test_ver28_other_objects_still_reported_when_one_is_unreadable(tmp_path):
    """MELLON-VER-SRS-28: with SPEC-1 unreadable at From, SPEC-2's change is still
    reported: exactly Modified, description 'Requirement 2.' -> 'Requirement 2, changed.'."""
    r, _, c2, c3 = build_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-2")
    got = (tuple(change.kinds), change.delta.description and
           (change.delta.description.text_a, change.delta.description.text_b)) if change else None
    assert got == ((Change.MODIFIED,), ("Requirement 2.", "Requirement 2, changed.")), (
        f"SPEC-2's change isn't reported as expected: {got}"
    )


def test_ver27_object_file_broken_differently_at_both_is_unreadable_at_both(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1's file isn't well-formed at From nor (differently) at To:
    it is reported unreadable at both."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("break SPEC-1")
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1 + b"  <attr name=\n")
    c3 = r.commit("break SPEC-1 differently")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    errors = change.errors if change else None
    assert errors is not None and None not in errors, f"expected unreadable at both versions, got {errors}"


def test_ver27_object_file_broken_identically_at_both_isnt_listed(tmp_path):
    """MELLON-VER-SRS-27: 'If an object's file or links file can't be read at From or at
    To, and its content isn't the same at both, ...' SPEC-1's file is broken with the
    same bytes at both, and only SPEC-2 changes: SPEC-1 isn't listed."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("break SPEC-1")
    # By hand: the editor can't load SPEC while SPEC-1's file is broken.
    data = r.read("SPEC/objects/SPEC-2.xml")
    old = b"<description>Requirement 2.</description>"
    assert old in data, "setup: SPEC-2 description not found"
    r.write("SPEC/objects/SPEC-2.xml", data.replace(old, b"<description>Requirement 2, changed.</description>"))
    c3 = r.commit("change SPEC-2 only")
    d = compare(r, c2, c3)
    assert change_for(d, "SPEC-1") is None, f"SPEC-1 is the same at both and shouldn't be listed: {change_for(d, 'SPEC-1')}"
    assert change_for(d, "SPEC-2") is not None, "setup: SPEC-2's change should be listed"


def test_ver27_links_file_broken_at_to_is_unreadable_at_to(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1's links file isn't well-formed at To: SPEC-1 is
    reported unreadable at To."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", DECL + b'<links source="SPEC-1">\n  <link target-doc="SPEC"\n')
    c2 = r.commit("break links")
    change = change_for(compare(r, c1, c2), "SPEC-1")
    errors = change.errors if change else None
    assert errors is not None and errors[0] is None and errors[1] is not None, (
        f"expected SPEC-1 unreadable at To (its links), got {errors}"
    )


def test_ver21_figure_not_well_formed_at_to_is_still_reported(tmp_path):
    """MELLON-VER-SRS-21 with -27: SPEC-4's figure replaced by an SVG that isn't
    well-formed. The figure field differs, so SPEC-4 must be listed. Requirement gap:
    -27 names only the object file and the links file, so whether this is Modified or
    'unreadable at To' isn't decided; the test only checks that SPEC-4 is reported."""
    r, f, c1 = build_spec(tmp_path)
    # By hand, as a git client could: the editor refuses an SVG that isn't well-formed.
    editor.figure_path(f, "SPEC-4").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect\n')
    c2 = r.commit("broken figure")
    change = change_for(compare(r, c1, c2), "SPEC-4")
    assert change is not None and (Change.MODIFIED in change.kinds or change.errors[1] is not None), (
        f"SPEC-4's figure changed but SPEC-4 isn't reported: {change}"
    )


def build_merge_in_progress(tmp_path: Path) -> GitRepo:
    """A merge that stopped on SPEC-1: main and side both changed its description; side
    also changed SPEC-2 to 'Side two.', which merged cleanly into the working copy."""
    r, f, c1 = build_spec(tmp_path)
    repo = r.repo
    main = repo.head.shorthand
    repo.branches.local.create("side", repo[pygit2.Oid(hex=c1)])
    editor.set_description(f, "SPEC-1", "Main text.")
    r.commit("main edit")
    repo = r.repo
    repo.checkout(repo.branches.local["side"])
    editor.set_description(f, "SPEC-1", "Side text.")
    editor.set_description(f, "SPEC-2", "Side two.")
    side = r.commit("side edit")
    repo = r.repo
    repo.checkout(repo.branches.local[main])
    repo.merge(pygit2.Oid(hex=side))
    assert b"<<<<<<<" in r.read("SPEC/objects/SPEC-1.xml"), "setup: no conflict markers in SPEC-1"
    assert (r.root / ".git" / "MERGE_HEAD").exists(), "setup: no merge in progress"
    return r


def test_ver27_conflict_markers_in_working_copy_are_unreadable_at_to(tmp_path):
    """MELLON-VER-SRS-27: during a merge in progress, SPEC-1's file in the working copy
    holds conflict markers: latest commit -> working copy reports SPEC-1 unreadable at To."""
    r = build_merge_in_progress(tmp_path)
    change = change_for(compare(r, diff.LATEST, diff.WORKING_COPY), "SPEC-1")
    errors = change.errors if change else None
    assert errors is not None and errors[1] is not None, f"SPEC-1 should be unreadable at To, got {errors}"


def test_ver28_merge_in_progress_other_objects_still_reported(tmp_path):
    """MELLON-VER-SRS-28: during that merge, SPEC-2's cleanly merged change is still
    reported: exactly Modified, 'Requirement 2.' -> 'Side two.'."""
    r = build_merge_in_progress(tmp_path)
    change = change_for(compare(r, diff.LATEST, diff.WORKING_COPY), "SPEC-2")
    got = (tuple(change.kinds), change.delta.description and
           (change.delta.description.text_a, change.delta.description.text_b)) if change else None
    assert got == ((Change.MODIFIED,), ("Requirement 2.", "Side two.")), f"SPEC-2's change not reported: {got}"


def test_links_file_unreadable_the_same_at_both_isnt_reported(tmp_path):
    """MELLON-VER-SRS-27: '... and its content isn't the same at both, Mellon shall
    report the object as unreadable at that version.' SPEC-1's links file is broken
    with the same bytes at both versions, and its description changes: SPEC-1 is
    exactly Modified (-21), with the description at both, and unreadable at neither."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", DECL + b'<links source="SPEC-1">\n  <link target-doc="SPEC"\n')
    c2 = r.commit("break SPEC-1's links")
    # By hand: the editor can't load SPEC while SPEC-1's links file is broken.
    data = r.read("SPEC/objects/SPEC-1.xml")
    old = b"<description>The VCU shall stop.</description>"
    assert old in data, "setup: SPEC-1 description not found"
    r.write("SPEC/objects/SPEC-1.xml", data.replace(old, b"<description>The VCU must stop.</description>"))
    c3 = r.commit("change SPEC-1's text only")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    got = (tuple(change.kinds), change.errors, change.delta and change.delta.description) if change else None
    assert got == ((Change.MODIFIED,), (None, None),
                   DescriptionChange("The VCU shall stop.", "The VCU must stop.")), f"SPEC-1: {got}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-29: unreadable manifest
# --------------------------------------------------------------------------


def build_manifest_broken_at_from(tmp_path: Path) -> tuple[GitRepo, str, str]:
    """c2: the manifest isn't well-formed. c3: the manifest valid again, with SPEC-3 moved
    under SPEC-4, and SPEC-1's description changed."""
    r, f, _ = build_spec(tmp_path)
    good = r.read("SPEC/SPEC.xml")
    broken = good[: len(good) // 2]
    assert not_well_formed(broken), "setup: truncated manifest still parses"
    r.write("SPEC/SPEC.xml", broken)
    c2 = r.commit("break manifest")
    r.write("SPEC/SPEC.xml", good)
    editor.move_object(f, "SPEC-3", parent_id="SPEC-4")
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    c3 = r.commit("fix manifest, move SPEC-3, change SPEC-1")
    return r, c2, c3


def test_ver29_manifest_broken_at_from_is_reported(tmp_path):
    """MELLON-VER-SRS-29: the manifest can't be read at From: Mellon reports that."""
    r, c2, c3 = build_manifest_broken_at_from(tmp_path)
    d = compare(r, c2, c3)
    assert (d.outline_error_a is not None, d.outline_error_b is None) == (True, True), (
        f"expected an outline error at From only, got {d.outline_error_a!r}, {d.outline_error_b!r}"
    )


def test_ver29_manifest_broken_at_from_reports_no_object_moved(tmp_path):
    """MELLON-VER-SRS-29: with the manifest unreadable at From, SPEC-3 (moved under SPEC-4
    at To) isn't reported Moved, nor is any other object."""
    r, c2, c3 = build_manifest_broken_at_from(tmp_path)
    d = compare(r, c2, c3)
    assert moved_ids(d) == set(), f"no object may be Moved without the From outline, got {sorted(moved_ids(d))}"


def test_ver29_manifest_broken_at_from_other_changes_still_reported(tmp_path):
    """MELLON-VER-SRS-29 with -21: SPEC-1's description change is still reported, and as
    exactly Modified."""
    r, c2, c3 = build_manifest_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.MODIFIED,), (
        f"SPEC-1 should be exactly Modified, got {change and change.kinds}"
    )


def build_manifest_broken_at_to(tmp_path: Path) -> tuple[GitRepo, str, str]:
    """c2: SPEC-1's description changed, SPEC-3 moved under SPEC-4, then the manifest
    overwritten with one that isn't well-formed."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    editor.move_object(f, "SPEC-3", parent_id="SPEC-4")
    good = r.read("SPEC/SPEC.xml")
    broken = good[: len(good) // 2]
    assert not_well_formed(broken), "setup: truncated manifest still parses"
    r.write("SPEC/SPEC.xml", broken)
    c2 = r.commit("change SPEC-1, break manifest")
    return r, c1, c2


def test_ver29_manifest_broken_at_to_is_reported(tmp_path):
    """MELLON-VER-SRS-29: the manifest can't be read at To: Mellon reports that."""
    r, c1, c2 = build_manifest_broken_at_to(tmp_path)
    d = compare(r, c1, c2)
    assert (d.outline_error_a is None, d.outline_error_b is not None) == (True, True), (
        f"expected an outline error at To only, got {d.outline_error_a!r}, {d.outline_error_b!r}"
    )


def test_ver29_manifest_broken_at_to_object_is_modified_only(tmp_path):
    """MELLON-VER-SRS-29: with the manifest unreadable at To, SPEC-1 is exactly Modified
    (not Moved), and no object is Moved."""
    r, c1, c2 = build_manifest_broken_at_to(tmp_path)
    d = compare(r, c1, c2)
    change = change_for(d, "SPEC-1")
    got = (tuple(change.kinds) if change else None, sorted(moved_ids(d)))
    assert got == ((Change.MODIFIED,), []), f"expected SPEC-1 exactly Modified and nothing Moved, got {got}"


def test_ver29_manifest_missing_with_folder_present_is_reported(tmp_path):
    """MELLON-VER-SRS-29 (definition 'can't be read': the folder is there and the manifest
    isn't): Mellon reports the manifest as unreadable at To."""
    r, _, c1 = build_spec(tmp_path)
    r.delete("SPEC/SPEC.xml")
    c2 = r.commit("manifest removed")
    d = compare(r, c1, c2)
    assert d.outline_error_b is not None, "a missing manifest in a present folder should be reported at To"


def test_ver29_manifest_missing_objects_are_not_deleted(tmp_path):
    """MELLON-VER-SRS-20 with -29 (definition 'doesn't exist at a version': an object
    exists while its object file is there): with only the manifest gone at To, no
    object is reported Deleted."""
    r, _, c1 = build_spec(tmp_path)
    r.delete("SPEC/SPEC.xml")
    c2 = r.commit("manifest removed")
    d = compare(r, c1, c2)
    deleted = sorted(c.obj_id for c in d.changes if Change.DELETED in c.kinds)
    assert deleted == [], f"object files are all still there, but Deleted: {deleted}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-47: word marking
# --------------------------------------------------------------------------


def _sides(segments) -> tuple[list[str], list[str], list[str], list[str]]:
    """(From words, To words, deleted words, inserted words) read from the segments."""
    tags = {tag for tag, _ in segments}
    assert tags <= {"equal", "delete", "insert"}, f"unexpected segment tags {tags}"
    frm, to, deleted, inserted = [], [], [], []
    for tag, text in segments:
        words = _words(text)
        if tag == "equal":
            frm += words
            to += words
        elif tag == "delete":
            frm += words
            deleted += words
        else:
            to += words
            inserted += words
    return frm, to, deleted, inserted


VOCAB = ["a", "b", "c", "the", "VCU", "shall", "stop.", "stop!", "<", "&", "x1", "x1."]
VOCAB_NO_PUNCTUATION = ["a", "b", "c", "the", "VCU", "shall", "stop", "<", "&", "x1"]
SPACES = [" ", "  ", "\t", "\n", " \n  "]

# definitions.md, Word (decided 2026-11-07): a run of letters, digits and
# underscores, or one other character that isn't whitespace.
_WORD_RE = re.compile(r"\w+|[^\w\s]")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _text(rng: random.Random, words: list[str]) -> str:
    text = "".join(w + rng.choice(SPACES) for w in words).rstrip(" \t\n") if words else ""
    if rng.random() < 0.2:
        text = rng.choice(SPACES) + text
    if rng.random() < 0.2:
        text += rng.choice(SPACES)
    return text


def _word_pairs(vocab: list[str], n: int = 400, seed: int = 4747) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    pairs = []
    for _ in range(n):
        a = [rng.choice(vocab) for _ in range(rng.randint(0, 10))]
        if rng.random() < 0.3:
            b = [rng.choice(vocab) for _ in range(rng.randint(0, 10))]
        else:
            b = a[:]
            for _ in range(rng.randint(1, 4)):
                op = rng.choice(["insert", "delete", "replace"])
                if op == "insert" or not b:
                    b.insert(rng.randint(0, len(b)), rng.choice(vocab))
                elif op == "delete":
                    b.pop(rng.randrange(len(b)))
                else:
                    b[rng.randrange(len(b))] = rng.choice(vocab)
        pairs.append((_text(rng, a), _text(rng, b)))
    return pairs


def _side_errors(vocab: list[str], side: int) -> list[str]:
    bad = []
    for a, b in _word_pairs(vocab):
        words = _sides(diff.word_diff(a, b))[side]
        if words != _words((a, b)[side]):
            bad.append(f"{a!r} -> {b!r}: {'From' if side == 0 else 'To'} read as {words}")
    return bad


def test_ver47_generated_from_is_unmarked_and_deleted_words():
    """MELLON-VER-SRS-47, generated: for each pair, the unmarked and deleted words, in
    order, are exactly the words of From, so no word of From is left out or split."""
    bad = _side_errors(VOCAB, 0)
    assert not bad, f"{len(bad)} pairs wrong, e.g.:\n" + "\n".join(bad[:5])


def test_ver47_generated_to_is_unmarked_and_inserted_words():
    """MELLON-VER-SRS-47, generated: for each pair, the unmarked and inserted words, in
    order, are exactly the words of To; with the From test this makes the unmarked
    words a common subsequence, the same at From and at To."""
    bad = _side_errors(VOCAB, 1)
    assert not bad, f"{len(bad)} pairs wrong, e.g.:\n" + "\n".join(bad[:5])


@pytest.mark.parametrize("side", [0, 1], ids=["from", "to"])
def test_ver47_generated_without_punctuation_unmarked_words_are_common(side):
    """MELLON-VER-SRS-47, generated, with words that hold no punctuation: From is its
    unmarked and deleted words in order, and To its unmarked and inserted words."""
    bad = _side_errors(VOCAB_NO_PUNCTUATION, side)
    assert not bad, f"{len(bad)} pairs wrong, e.g.:\n" + "\n".join(bad[:5])


@pytest.mark.parametrize(
    "a, b, deleted, inserted",
    [
        ("", "New text", [], ["New", "text"]),
        ("Old text", "", ["Old", "text"], []),
        ("stop.", "stop!", ["."], ["!"]),
    ],
    ids=["empty-to-text", "text-to-empty", "punctuation"],
)
def test_ver47_edge_cases_marked_words(a, b, deleted, inserted):
    """MELLON-VER-SRS-47: '' -> 'New text' marks both words inserted and nothing at From;
    'Old text' -> '' marks both deleted; 'stop.' -> 'stop!' marks only '.' and '!', each
    a word of its own (definition of Word), so 'stop' is unmarked at both."""
    _, _, got_deleted, got_inserted = _sides(diff.word_diff(a, b))
    assert (got_deleted, got_inserted) == (deleted, inserted), (
        f"{a!r} -> {b!r}: deleted {got_deleted}, inserted {got_inserted}"
    )


def test_ver47_only_the_changed_word_is_marked():
    """MELLON-VER-SRS-47: 'a < b & c' -> 'a < d & c' marks only b (deleted) and d
    (inserted): a, <, & and c weren't deleted or inserted, and the rationale asks that
    few words be marked. The one common subsequence of length 4 makes this unique."""
    _, _, deleted, inserted = _sides(diff.word_diff("a < b & c", "a < d & c"))
    assert (deleted, inserted) == (["b"], ["d"]), f"deleted {deleted}, inserted {inserted}"


def test_ver47_compared_description_words_cover_both_texts(tmp_path):
    """MELLON-VER-SRS-47 through a comparison: SPEC-1 'The VCU shall stop.' -> 'The VCU
    must stop.': the reported words give the From text from unmarked and deleted
    words, and the To text from unmarked and inserted words."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    c2 = r.commit("must")
    change = change_for(compare(r, c1, c2), "SPEC-1")
    frm, to, _, _ = _sides(change.delta.description.words())
    assert (frm, to) == (_words("The VCU shall stop."), _words("The VCU must stop.")), (
        f"words read From {frm}, To {to}"
    )


# --------------------------------------------------------------------------
# MELLON-VER-SRS-56: comparing changes nothing
# --------------------------------------------------------------------------


COMPARISONS = {
    "commit-to-commit": (lambda c: ("SPEC", c["c1"], c["c2"]), False),
    "issue-to-latest": (lambda c: ("SPEC", "SPEC-issue-1", diff.LATEST), False),
    "typed-prefix": (lambda c: ("SPEC", c["c1"][:6], c["c2"]), False),
    "latest-to-working-copy": (lambda c: ("SPEC", diff.LATEST, diff.WORKING_COPY), False),
    "working-copy-to-issue": (lambda c: ("SPEC", diff.WORKING_COPY, "SPEC-issue-1"), False),
    "unreadable-files": (lambda c: ("SPEC", c["c2"], c["c3"]), False),
    "bad-name": (lambda c: ("SPEC", "no-such-version", diff.LATEST), True),
    "document-at-neither": (lambda c: ("NOPE", c["c1"], c["c2"]), True),
}


@pytest.mark.parametrize("name", list(COMPARISONS))
def test_ver56_comparing_changes_no_file_index_or_reference(tmp_path, name):
    """MELLON-VER-SRS-56: a comparison (or a failing one) leaves every working-copy file,
    the git index, HEAD and every reference as they were."""
    r, commits = build_snapshot_repo(tmp_path)
    make_args, fails = COMPARISONS[name]
    doc_id, ref_a, ref_b = make_args(commits)
    before = snapshot(r.root)
    if fails:
        with pytest.raises(Exception):
            diff.compare_document(r.root, doc_id, ref_a, ref_b)
    else:
        diff.compare_document(r.root, doc_id, ref_a, ref_b)
    changed = snapshot_difference(before, snapshot(r.root))
    assert not changed, f"comparing {name} changed: {changed[:10]}"


# ==========================================================================
# MELLON-VER-SRS-9 / -10: typed versions
# ==========================================================================


def test_srs10_ambiguous_prefix_refused_below_dialog(tmp_path):
    """MELLON-VER-SRS-10: below the dialog, comparing at an ambiguous prefix raises an error
    naming it instead of comparing one of the two objects."""
    root = tmp_path / "r"
    _build(root)
    prefix = _ambiguous_prefix(root, "commit")
    with pytest.raises(Exception) as err:
        compare_document(root, "SPEC", prefix, LATEST)
    assert prefix in str(err.value), f"the error should name {prefix!r}: {err.value!r}"


def _shared_name_repo(root: Path, *, same_commit: bool = False) -> tuple[str, str]:
    """The standard repository with a tag and a branch both named `shared`: the tag
    on seed, the branch on edits (or on seed too). Returns (seed, edits)."""
    _build(root)
    seed, edits = _oid(root, "HEAD~1"), _oid(root, "HEAD")
    repo = pygit2.Repository(str(root))
    repo.references.create("refs/tags/shared", pygit2.Oid(hex=seed))
    repo.references.create("refs/heads/shared", pygit2.Oid(hex=seed if same_commit else edits))
    return seed, edits


@pytest.mark.parametrize("typed", ["shared", "shared~0", "shared^{commit}"])
def test_name_of_two_refs_at_different_commits_is_refused(tmp_path, typed):
    """MELLON-VER-SRS-10: 'If a typed name resolves to no commit or to more than one,
    ... an error message naming the typed name.' `shared` is a tag on seed and a
    branch on edits, so it resolves to two commits, also inside an expression."""
    root = tmp_path / "r"
    _shared_name_repo(root)
    with pytest.raises(GitError) as err:
        compare_document(root, "SPEC", typed, LATEST)
    assert repr(typed) in str(err.value), f"the error should name {typed!r}: {err.value}"


def test_name_of_a_ref_and_a_hash_prefix_is_refused(tmp_path):
    """MELLON-VER-SRS-10: a typed name that is a branch on edits and also the hash prefix
    of seed resolves to two commits: an error naming it."""
    root = tmp_path / "r"
    _build(root)
    seed, edits = _oid(root, "HEAD~1"), _oid(root, "HEAD")
    pygit2.Repository(str(root)).references.create(f"refs/heads/{seed[:7]}", pygit2.Oid(hex=edits))
    with pytest.raises(GitError) as err:
        compare_document(root, "SPEC", seed[:7], LATEST)
    assert repr(seed[:7]) in str(err.value), f"the error should name {seed[:7]!r}: {err.value}"


def test_name_of_two_refs_at_one_commit_is_accepted(tmp_path):
    """MELLON-VER-SRS-9, -10: a tag and a branch both named `shared`, both on seed:
    the name resolves to one commit, so it is compared as seed."""
    root = tmp_path / "r"
    seed, _ = _shared_name_repo(root, same_commit=True)
    assert compare_document(root, "SPEC", "shared", LATEST).commit_a == seed


@pytest.mark.parametrize("typed, side", [("refs/tags/shared", "seed"), ("refs/heads/shared", "edits"),
                                         ("refs/heads/shared~1", "seed"), ("@~1", "seed")])
def test_full_ref_names_beside_a_shared_name_resolve(tmp_path, typed, side):
    """MELLON-VER-SRS-9: with `shared` both a tag (seed) and a branch (edits), the full
    ref names, an expression on one, and @~1 each resolve to their one commit."""
    root = tmp_path / "r"
    seed, edits = _shared_name_repo(root)
    expected = {"seed": seed, "edits": edits}[side]
    assert compare_document(root, "SPEC", typed, LATEST).commit_a == expected


# ==========================================================================
# MELLON-VER-SRS-14 / -15 / -16: unset versions
# ==========================================================================


def test_srs14_only_issue_off_the_line_of_history_defaults(tmp_path):
    """MELLON-VER-SRS-14 (git level): default_versions gives From unset, To = latest commit
    when the only issue isn't on the line of history."""
    root = tmp_path / "r"
    _build(root, issue=False)
    side = _side_commit(root, "HEAD~1", "side branch work")
    git_ops.create_issue_tag(pygit2.Repository(str(root)), "SPEC", "1", pygit2.Oid(hex=side))
    from_ref, to_ref = default_versions(root, "SPEC")
    assert from_ref is None, f"From should be unset: {from_ref!r}"
    assert to_ref != WORKING_COPY and _commit_of(root, to_ref) == _oid(root, "HEAD"), f"To: {to_ref!r}"


# --- written blind from MELLON-VER-SRS (round 2) -----------------------------
#
# MELLON-VER-SRS-22 on rung 2, the comparison over real git commits. Written
# from the requirements, docs/definitions.md and the public signatures alone,
# without the code under test or the bodies of other tests; every expected
# value comes from a requirement or a definition. Repositories are built in
# tmp_path with fixed commit signatures; no real clock, no network.


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-22: differences gone in the canonical form are no change
# ---------------------------------------------------------------------------


def _collapse_empty(s: str) -> str:
    return re.sub(r"<([A-Za-z_][\w.:-]*)([^<>]*)></\1>", r"<\1\2/>", s)


def _one_line(s: str) -> str:
    return re.sub(r">\s+<", "><", s)


def _no_final_newline(s: str) -> str:
    return s.rstrip("\n")


XML_REWRITES: list[tuple[str, Callable[[str], str]]] = [
    ("CRLF line endings", _crlf),
    ("tab indentation", _tabs),
    ("no indentation, one line", _one_line),
    ("blank lines between elements", _blank_lines),
    ("no final newline", _no_final_newline),
    ("XML attributes in reverse order", _reverse_attributes),
    ("single quotes", _single_quotes),
    ("empty elements as <x></x>", _expand_empty),
    ("empty elements as <x/>", _collapse_empty),
    ("whitespace inside tags", _space_inside_tags),
    ("no XML declaration", _no_declaration),
    ("character references", _char_refs),
    ("CDATA sections", _cdata),
    ("all of these at once", _everything),
]

FIGURE_REWRITES: list[tuple[str, Callable[[str], str]]] = [
    ("CRLF line endings", _crlf),
    ("tab indentation", _tabs),
    ("XML attributes in reverse order", _reverse_attributes),
    ("single quotes", _single_quotes),
    ("empty elements as <x></x>", _expand_empty),
    ("whitespace inside tags", _space_inside_tags),
]


def _reported(d: diff.DocumentDiff) -> list[str]:
    """Everything a comparison reports: each object change, and outline problems."""
    out = [f"{c.obj_id}: {', '.join(str(k) for k in c.kinds)} {c.errors}" for c in d.changes]
    for side, err in (("From", d.outline_error_a), ("To", d.outline_error_b)):
        if err:
            out.append(f"manifest at {side}: {err}")
    return out


@pytest.mark.parametrize("case, rewrite", XML_REWRITES, ids=[c[0] for c in XML_REWRITES])
def test_22_canonical_only_rewrite_of_document_files_is_no_change(canon_repo, case, rewrite):
    """MELLON-VER-SRS-22: every manifest, object, links and rules file rewritten in a
    way its canonical form doesn't show (Canonical XML 1.1 also drops the XML
    declaration and resolves character references and CDATA). Comparing the two
    commits reports no object and no problem."""
    root, info = canon_repo
    files = _rewrite(root, "**/*.xml", rewrite)
    c2 = _r2_commit(root, f"rewrite: {case}")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    assert _reported(d) == [], f"{case} in {files} is gone in the canonical form, but Compare reports {_reported(d)}"


@pytest.mark.parametrize("case, rewrite", FIGURE_REWRITES, ids=[c[0] for c in FIGURE_REWRITES])
def test_22_canonical_only_rewrite_of_figure_is_no_change(canon_repo, case, rewrite):
    """MELLON-VER-SRS-22: the figure is a file too (an SVG, so XML). A rewrite its
    canonical form doesn't show marks no object Modified."""
    root, info = canon_repo
    files = _rewrite(root, "figures/*.svg", rewrite)
    c2 = _r2_commit(root, f"figure rewrite: {case}")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    assert _reported(d) == [], f"{case} in {files} is gone in the canonical form, but Compare reports {_reported(d)}"


def _desc_word(root, folder, ids):
    editor.set_description(folder, ids[0], "Top title.")
    return {ids[0]: ("Modified",)}


def _desc_inner_space(root, folder, ids):
    editor.set_description(folder, ids[1], "Line one.\nLine  two.")
    return {ids[1]: ("Modified",)}


def _ack(root, folder, ids):
    path = folder / "links" / f"{ids[1]}.xml"
    seed = str(pygit2.Repository(str(root)).head.target)
    data = path.read_bytes()
    new = re.sub(rb'ack="[0-9a-f]+"', f'ack="{seed}"'.encode(), data)
    assert new != data, "test setup: no ack in the links file"
    path.write_bytes(new)
    return {ids[1]: ("Link reviewed",)}


def _swap(root, folder, ids):
    # From: SPEC-1, SPEC-2, SPEC-3 under SPEC-0; To: SPEC-1, SPEC-3, SPEC-2. Two
    # largest same-order sets ({1,2}, {1,3}); the later at From (SPEC-3) is Moved.
    editor.move_object(folder, ids[3], before=ids[2])
    return {ids[3]: ("Moved",)}


REAL_CHANGES = [
    ("a word in a description", _desc_word),
    ("whitespace inside a description", _desc_inner_space),
    ("an acknowledged commit", _ack),
    ("the outline order", _swap),
]


@pytest.mark.parametrize("case, change", REAL_CHANGES, ids=[c[0] for c in REAL_CHANGES])
def test_22_real_change_in_rewritten_files_is_still_reported(canon_repo, case, change):
    """MELLON-VER-SRS-22's control (with -21, -23, -24): a change the canonical form
    does show is reported although every file was also rewritten in each way that
    doesn't show. Whitespace inside a description's text isn't between elements,
    so it stays in the canonical form and is a change."""
    root, info = canon_repo
    expected = change(root, root / DOC, info["ids"])
    _rewrite(root, "**/*.xml", _everything)
    c2 = _r2_commit(root, f"real change: {case}")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    assert _kinds(d) == expected, f"{case}: expected exactly {expected}, Compare reports {_reported(d)}"


def test_22_real_figure_change_in_rewritten_figure_is_still_reported(canon_repo):
    """MELLON-VER-SRS-22's control for the figure (with -21): a figure whose colour
    changed is Modified although its file was also rewritten in ways that don't show."""
    root, info = canon_repo
    path = root / DOC / "figures" / f"{info['ids'][2]}.svg"
    path.write_bytes(path.read_bytes().replace(b"#ff0000", b"#0000ff"))
    for _, rewrite in FIGURE_REWRITES:
        _rewrite(root, "figures/*.svg", rewrite)
    c2 = _r2_commit(root, "real figure change")
    d = diff.compare_document(root, DOC, info["c1"], c2)
    expected = {info["ids"][2]: ("Modified",)}
    assert _kinds(d) == expected, f"expected exactly {expected}, Compare reports {_reported(d)}"


# MELLON-STORE-SRS-229 (CV-TRACE-07): Compare's working copy doesn't follow a
# linked folder or file in the document.
from tests.trace_support import Repo, dir_link, file_link  # noqa: E402


def make_repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "repo")
    r.add_doc("D", {"D-1": "one"})
    r.commit("D")
    return r


def test_store_srs_229_compare_refuses_a_linked_subfolder(tmp_path: Path) -> None:
    r = make_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dir_link(r.root / "D" / "links", elsewhere)
    with pytest.raises(GitError, match="junction|symbolic link"):
        compare_document(r.root, "D", "HEAD", diff.WORKING_COPY)


def test_store_srs_229_compare_doesnt_read_a_linked_file(tmp_path: Path) -> None:
    r = make_repo(tmp_path)
    outside = tmp_path / "outside.xml"
    outside.write_bytes((r.root / "D" / "objects" / "D-1.xml").read_bytes().replace(b"one", b"OUTSIDE"))
    (r.root / "D" / "objects" / "D-1.xml").unlink()
    file_link(r.root / "D" / "objects" / "D-1.xml", outside)
    result = compare_document(r.root, "D", "HEAD", diff.WORKING_COPY)
    assert "OUTSIDE" not in repr(result)
