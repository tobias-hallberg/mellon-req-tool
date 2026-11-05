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


# --- whole documents: outline, tables, figures, working copy ----------------
#
# These build real documents with the editor, as the GUI and MCP do, so the
# manifest outline is there for move detection and display order.

from mellon import editor  # noqa: E402
from mellon.diff import (  # noqa: E402
    LATEST,
    WORKING_COPY,
    Change,
    FigureChange,
    Position,
    TableChange,
    default_versions,
    document_versions,
    word_diff,
)
from mellon.schema import Table, TableCell, TableColumn, TableRow  # noqa: E402

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
    assert "feature" in [v.ref for v in versions if v.kind == "branch"]
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
