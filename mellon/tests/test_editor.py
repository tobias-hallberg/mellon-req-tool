"""Tests for editor commands: new, edit-attr, link, unlink."""

from __future__ import annotations

import errno
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pygit2
import pytest
from lxml import etree

from mellon import (
    EditorError,
    FilterRule,
    add_attr_option,
    add_attribute,
    add_link,
    add_parent,
    allocate_next_id,
    Table,
    TableCell,
    TableColumn,
    TableRow,
    change_attribute_required,
    clear_figure,
    clear_table,
    create_filter,
    create_object,
    create_view,
    delete_attribute,
    delete_filter,
    delete_object,
    delete_view,
    editor,
    figure_path,
    load_document,
    move_object,
    remove_attr_option,
    remove_link,
    remove_parent,
    rename_filter,
    rename_view,
    set_attribute,
    set_description,
    set_figure,
    set_filter_rules,
    set_metadata,
    set_table,
    set_view_visible,
    validate,
)
from mellon.service import DocumentService
from tests.test_repo import _write_merge_conflict
from tests.test_suspects import (
    DOC_MANIFEST,
    DOC_OBJ,
    PARENT_MANIFEST,
    PARENT_OBJ_V1,
    _commit_all,
    guard_paths_outside,
)
from tests.store_support import (  # MELLON-STORE-SRS blind tests
    DECL,
    DECL_LINE,
    DEFAULT_ATTRS,
    PartialWrites,
    _BAD_SLUGS,
    _ENUM_ATTRS,
    _read_text,
    _svg_file,
    _write_text,
    changed,
    git_commit_all,
    git_env,  # noqa: F401, F811 -- fixture
    git_init,
    make_doc,
    slug_doc,  # noqa: F401, F811 -- fixture
    snapshot,
    write,
)


def test_allocate_next_id_empty(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    # Fixture has DOC-0 and DOC-1.
    assert allocate_next_id(doc) == "DOC-2"


def test_create_object_top_level(doc_folder: Path) -> None:
    new_id = create_object(doc_folder, description="new top-level req")
    assert new_id == "DOC-2"
    assert (doc_folder / "objects" / "DOC-2.xml").exists()

    doc = load_document(doc_folder)
    assert "DOC-2" in doc.objects
    assert doc.objects["DOC-2"].description == "new top-level req"
    # Appears in outline at top level.
    assert doc.outline_path("DOC-2") == ("DOC-2",)


def test_create_object_as_child(doc_folder: Path) -> None:
    new_id = create_object(doc_folder, parent_id="DOC-0", description="child")
    doc = load_document(doc_folder)
    # Outline path should descend from DOC-0.
    assert doc.outline_path(new_id) == ("DOC-0", new_id)


def test_create_object_unknown_parent_fails(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="parent 'DOC-999' not found"):
        create_object(doc_folder, parent_id="DOC-999")


def test_set_attribute_existing(doc_folder: Path) -> None:
    set_attribute(doc_folder, "DOC-1", "status", "Approved")
    doc = load_document(doc_folder)
    assert doc.objects["DOC-1"].attrs["status"] == "Approved"


def test_set_attribute_new(doc_folder: Path) -> None:
    # rationale is not set on DOC-0 in the fixture.
    set_attribute(doc_folder, "DOC-0", "rationale", "needed because")
    doc = load_document(doc_folder)
    assert doc.objects["DOC-0"].attrs["rationale"] == "needed because"


def test_set_attribute_rejects_bad_enum(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not a valid option"):
        set_attribute(doc_folder, "DOC-1", "status", "Nonsense")


def test_set_attribute_empty_clears_enum(doc_folder: Path) -> None:
    # The grid's enum editor offers an empty entry; "" is never a bad option
    # (the validator and the service agree), so it clears the value.
    set_attribute(doc_folder, "DOC-1", "status", "")
    doc = load_document(doc_folder)
    assert doc.objects["DOC-1"].attrs["status"] == ""


def test_set_attribute_rejects_unknown_name(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not declared"):
        set_attribute(doc_folder, "DOC-1", "madeup", "x")


def test_round_trip_stays_valid(doc_folder: Path) -> None:
    """After editor operations the doc must still pass validate()."""
    new_id = create_object(doc_folder, parent_id="DOC-0", description="y")
    set_attribute(doc_folder, new_id, "status", "Draft")
    set_attribute(doc_folder, new_id, "type", "Requirement")
    doc = load_document(doc_folder)
    assert validate(doc) == []


def test_create_document_writes_default_rules(tmp_path: Path) -> None:
    from mellon.editor import create_document

    folder = create_document(tmp_path, "NEW")
    assert (folder / "NEW.xml").is_file()
    assert (folder / "NEW.rules.xml").is_file()
    doc = load_document(folder)
    assert len(doc.rules) > 0
    # Default type options are the three structural types only.
    type_attr = doc.attribute("type")
    assert type_attr is not None
    assert set(type_attr.options) == {"Heading", "Information", "Note"}
    assert doc.attribute("RequirementType") is None


def test_create_document_with_custom_schema_skips_rules(tmp_path: Path) -> None:
    from mellon.editor import create_document

    folder = create_document(
        tmp_path, "BARE",
        attr_schema=[{"name": "status", "kind": "enum", "required": True,
                      "options": ["Draft"]}],
    )
    assert (folder / "BARE.xml").is_file()
    # Custom schema means the caller hasn't opted in to default rules; we
    # don't presume which rules they want.
    assert not (folder / "BARE.rules.xml").exists()


def test_create_object_recreates_missing_objects_dir(tmp_path: Path) -> None:
    # git doesn't track an empty objects/, so a fresh clone of a new
    # document has none.
    from mellon.editor import create_document

    folder = create_document(tmp_path, "NEW")
    (folder / "objects").rmdir()
    new_id = create_object(folder, description="first")
    assert (folder / "objects" / f"{new_id}.xml").is_file()
    assert new_id in load_document(folder).objects


# ---- link / unlink ----

def _two_doc_repo(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    parent = tmp_path / "PARENT"
    (parent / "objects").mkdir(parents=True)
    (parent / "PARENT.xml").write_text(PARENT_MANIFEST, encoding="utf-8")
    (parent / "objects" / "PARENT-42.xml").write_text(PARENT_OBJ_V1, encoding="utf-8")

    doc = tmp_path / "DOC"
    (doc / "objects").mkdir(parents=True)
    (doc / "DOC.xml").write_text(DOC_MANIFEST, encoding="utf-8")
    (doc / "objects" / "DOC-1.xml").write_text(DOC_OBJ, encoding="utf-8")

    repo = pygit2.init_repository(str(tmp_path))
    c1 = _commit_all(repo, "initial")
    return tmp_path, parent, doc, c1


def test_add_link_sets_ack_from_git(tmp_path: Path) -> None:
    root, _parent, doc, c1 = _two_doc_repo(tmp_path)
    ack = add_link(doc, "DOC-1", "PARENT", "PARENT-42", "satisfies")
    assert ack == c1

    loaded = load_document(doc)
    (link,) = loaded.links["DOC-1"]
    assert link.target_doc == "PARENT"
    assert link.type == "satisfies"
    assert link.ack == c1


@pytest.mark.parametrize("target_id", ["../../../OUT-1", "PARENT-42/../../../../OUT-1"])
def test_trace_srs_79_add_link_reads_nothing_outside_the_repository(
    tmp_path: Path, monkeypatch, target_id: str
) -> None:
    """MELLON-TRACE-SRS-79: working out a new link's acknowledged commit opens
    nothing outside the repository, whatever the target ID says."""
    root, _parent, doc, _c1 = _two_doc_repo(tmp_path)
    _inside, outside = guard_paths_outside(monkeypatch, root)

    try:
        add_link(doc, "DOC-1", "PARENT", target_id, "satisfies")
    except EditorError:
        pass  # refusing the link is fine too; reaching outside isn't

    assert outside == []


def test_add_link_without_git(doc_folder: Path) -> None:
    # No git repo -> ack is None (link starts suspect).
    # Need a source that exists: DOC-0 has no outgoing links currently.
    ack = add_link(doc_folder, "DOC-0", "PARENT", "PARENT-99", "refines")
    assert ack is None

    loaded = load_document(doc_folder)
    (link,) = loaded.links["DOC-0"]
    assert link.ack is None


def test_add_link_rejects_duplicate(doc_folder: Path) -> None:
    add_link(doc_folder, "DOC-0", "PARENT", "PARENT-1", "refines")
    with pytest.raises(EditorError, match="already exists"):
        add_link(doc_folder, "DOC-0", "PARENT", "PARENT-1", "refines")


def test_add_link_rejects_non_parent_target(doc_folder: Path) -> None:
    # DOC's only declared parent is PARENT; STRANGER is not allowed.
    with pytest.raises(EditorError, match="not allowed to link"):
        add_link(doc_folder, "DOC-0", "STRANGER", "STRANGER-1", "satisfies")
    # And no links file should have been written.
    assert not (doc_folder / "links" / "DOC-0.xml").exists()


def test_add_link_allows_self_link(doc_folder: Path) -> None:
    # Self-links (target-doc == own doc id) are implicitly permitted even
    # though "DOC" is not in <parents>.
    add_link(doc_folder, "DOC-0", "DOC", "DOC-1", "refines")
    loaded = load_document(doc_folder)
    (link,) = loaded.links["DOC-0"]
    assert link.target_doc == "DOC"
    assert link.target_id == "DOC-1"


def test_add_link_top_level_doc_cannot_link_out(doc_folder: Path) -> None:
    # Strip <parents> from the manifest so DOC becomes top-level — it must
    # then refuse any cross-doc link.
    manifest = doc_folder / "DOC.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '<parents><parent doc="PARENT"/></parents>\n  ', ""
        ),
        encoding="utf-8",
    )
    with pytest.raises(EditorError, match="not allowed to link"):
        add_link(doc_folder, "DOC-0", "PARENT", "PARENT-1", "satisfies")


def test_remove_link_last_link_deletes_file(doc_folder: Path) -> None:
    # Fixture has one link on DOC-1 -> PARENT/PARENT-42.
    ok = remove_link(doc_folder, "DOC-1", "PARENT", "PARENT-42")
    assert ok is True
    assert not (doc_folder / "links" / "DOC-1.xml").exists()


def test_remove_link_keeps_file_when_other_links_remain(doc_folder: Path) -> None:
    add_link(doc_folder, "DOC-1", "PARENT", "PARENT-9", "refines")
    ok = remove_link(doc_folder, "DOC-1", "PARENT", "PARENT-42")
    assert ok is True
    loaded = load_document(doc_folder)
    (link,) = loaded.links["DOC-1"]
    assert link.target_id == "PARENT-9"


def test_remove_link_missing_returns_false(doc_folder: Path) -> None:
    assert remove_link(doc_folder, "DOC-0", "PARENT", "X") is False


# ---- move / reorder ----

def test_move_to_top_level(doc_folder: Path) -> None:
    # DOC-1 starts as a child of DOC-0.
    move_object(doc_folder, "DOC-1", to_top=True)
    doc = load_document(doc_folder)
    assert doc.outline_path("DOC-1") == ("DOC-1",)
    # DOC-0 should still exist at top level, without DOC-1 as child.
    assert doc.outline_path("DOC-0") == ("DOC-0",)


def test_move_under_new_parent(doc_folder: Path) -> None:
    create_object(doc_folder, description="sibling top-level")  # DOC-2
    move_object(doc_folder, "DOC-1", parent_id="DOC-2")
    doc = load_document(doc_folder)
    assert doc.outline_path("DOC-1") == ("DOC-2", "DOC-1")


def test_move_preserves_descendants(doc_folder: Path) -> None:
    # Give DOC-1 a child, then reparent DOC-1. Child must travel with it.
    create_object(doc_folder, parent_id="DOC-1", description="grandchild")  # DOC-2
    create_object(doc_folder, description="new home")  # DOC-3
    move_object(doc_folder, "DOC-1", parent_id="DOC-3")
    doc = load_document(doc_folder)
    assert doc.outline_path("DOC-1") == ("DOC-3", "DOC-1")
    assert doc.outline_path("DOC-2") == ("DOC-3", "DOC-1", "DOC-2")


def test_move_before_sibling(doc_folder: Path) -> None:
    create_object(doc_folder, description="a")  # DOC-2 (top level)
    create_object(doc_folder, description="b")  # DOC-3 (top level)
    # Top-level order now: DOC-0, DOC-2, DOC-3. Put DOC-3 before DOC-2.
    move_object(doc_folder, "DOC-3", before="DOC-2")
    doc = load_document(doc_folder)
    top_level = [n.ref_id for n in doc.outline]
    assert top_level == ["DOC-0", "DOC-3", "DOC-2"]


def test_move_after_sibling(doc_folder: Path) -> None:
    create_object(doc_folder, description="a")  # DOC-2
    create_object(doc_folder, description="b")  # DOC-3
    # Top-level: DOC-0, DOC-2, DOC-3. Put DOC-0 after DOC-2.
    move_object(doc_folder, "DOC-0", after="DOC-2")
    doc = load_document(doc_folder)
    top_level = [n.ref_id for n in doc.outline]
    assert top_level == ["DOC-2", "DOC-0", "DOC-3"]


def test_move_under_own_descendant_fails(doc_folder: Path) -> None:
    # DOC-1 is a child of DOC-0. Moving DOC-0 under DOC-1 would create a cycle.
    with pytest.raises(EditorError, match="descendant"):
        move_object(doc_folder, "DOC-0", parent_id="DOC-1")


def test_move_under_self_fails(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="under itself"):
        move_object(doc_folder, "DOC-0", parent_id="DOC-0")


def test_move_unknown_parent_fails(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found in outline"):
        move_object(doc_folder, "DOC-1", parent_id="DOC-999")


def test_move_unknown_sibling_fails(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="sibling 'DOC-999'"):
        move_object(doc_folder, "DOC-1", to_top=True, before="DOC-999")


def test_move_requires_intent(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="at least one of"):
        move_object(doc_folder, "DOC-1")


def test_move_keeps_doc_valid(doc_folder: Path) -> None:
    create_object(doc_folder, description="x")  # DOC-2
    set_attribute(doc_folder, "DOC-2", "type", "Information")
    set_attribute(doc_folder, "DOC-2", "status", "Draft")
    move_object(doc_folder, "DOC-1", parent_id="DOC-2")
    doc = load_document(doc_folder)
    assert validate(doc) == []


# ---- set_outline (declarative reorder) ----

from mellon.editor import set_outline


def test_set_outline_flat_swap(doc_folder: Path) -> None:
    # Fixture has DOC-0 + DOC-1 at top-level. Swap their order.
    set_outline(doc_folder, [{"id": "DOC-1", "children": []}, {"id": "DOC-0", "children": []}])
    doc = load_document(doc_folder)
    ids = list(doc.iter_outline_ids())
    assert ids == ["DOC-1", "DOC-0"]
    assert validate(doc) == []


def test_set_outline_nest_under_sibling(doc_folder: Path) -> None:
    # Nest DOC-1 under DOC-0.
    set_outline(
        doc_folder,
        [{"id": "DOC-0", "children": [{"id": "DOC-1", "children": []}]}],
    )
    doc = load_document(doc_folder)
    assert validate(doc) == []
    # DOC-1 is now a child of DOC-0 in the outline tree.
    assert doc.outline[0].ref_id == "DOC-0"
    assert doc.outline[0].children[0].ref_id == "DOC-1"


def test_set_outline_missing_id_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="omits objects"):
        set_outline(doc_folder, [{"id": "DOC-0", "children": []}])
    # Manifest is unchanged (still has both refs).
    doc = load_document(doc_folder)
    assert set(doc.iter_outline_ids()) == {"DOC-0", "DOC-1"}


def test_set_outline_extra_id_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="unknown objects"):
        set_outline(
            doc_folder,
            [
                {"id": "DOC-0", "children": []},
                {"id": "DOC-1", "children": []},
                {"id": "DOC-NOPE", "children": []},
            ],
        )


def test_set_outline_duplicate_id_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="multiple times"):
        set_outline(
            doc_folder,
            [
                {"id": "DOC-0", "children": []},
                {"id": "DOC-1", "children": []},
                {"id": "DOC-0", "children": []},
            ],
        )


def test_set_outline_deep_three_levels(doc_folder: Path) -> None:
    # Add a third object and arrange them three deep: DOC-0 > DOC-1 > DOC-2.
    create_object(doc_folder, description="z")  # DOC-2
    set_outline(
        doc_folder,
        [
            {
                "id": "DOC-0",
                "children": [
                    {
                        "id": "DOC-1",
                        "children": [{"id": "DOC-2", "children": []}],
                    }
                ],
            }
        ],
    )
    doc = load_document(doc_folder)
    assert doc.outline[0].ref_id == "DOC-0"
    assert doc.outline[0].children[0].ref_id == "DOC-1"
    assert doc.outline[0].children[0].children[0].ref_id == "DOC-2"


def test_set_outline_empty_doc_empty_ordering(tmp_path: Path) -> None:
    # Build a tiny doc with no objects and confirm empty ordering is a valid no-op.
    from mellon.editor import create_document as _create_doc
    folder = _create_doc(tmp_path, "EMPTY", title="Empty")
    set_outline(folder, [])
    doc = load_document(folder)
    assert doc.outline == ()


# ---- manifest edits ----

def test_set_metadata_updates_existing(doc_folder: Path) -> None:
    set_metadata(doc_folder, "title", "New Title")
    doc = load_document(doc_folder)
    assert doc.metadata.title == "New Title"


def test_set_metadata_creates_missing_field(doc_folder: Path, tmp_path: Path) -> None:
    # Write a manifest that has <metadata> but no <issue> child.
    minimal = tmp_path / "MIN"
    (minimal / "objects").mkdir(parents=True)
    (minimal / "MIN.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<document id="MIN">\n'
        "  <metadata><title>x</title></metadata>\n"
        "  <attributes/>\n"
        "  <outline/>\n"
        "</document>\n",
        encoding="utf-8",
    )
    set_metadata(minimal, "author", "alice")
    doc = load_document(minimal)
    assert doc.metadata.author == "alice"


def test_set_metadata_rejects_unknown_field(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="unknown metadata field"):
        set_metadata(doc_folder, "nonsense", "x")


def test_set_metadata_rejects_bad_date(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="ISO 8601 date"):
        set_metadata(doc_folder, "date", "2026-04-215")


def test_set_metadata_accepts_iso_date(doc_folder: Path) -> None:
    set_metadata(doc_folder, "date", "2026-04-25")
    assert load_document(doc_folder).metadata.date == "2026-04-25"


def test_set_metadata_accepts_empty_date(doc_folder: Path) -> None:
    set_metadata(doc_folder, "date", "")
    assert load_document(doc_folder).metadata.date == ""


def test_set_metadata_rejects_issue_field(doc_folder: Path) -> None:
    """Issue moved to git tags — set_metadata refuses the field outright."""
    with pytest.raises(EditorError, match="unknown metadata field"):
        set_metadata(doc_folder, "issue", "2")


def test_add_parent_adds_new(doc_folder: Path) -> None:
    add_parent(doc_folder, "ANOTHER")
    doc = load_document(doc_folder)
    assert "ANOTHER" in doc.parents
    assert "PARENT" in doc.parents  # existing one preserved


def test_add_parent_rejects_duplicate(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="already declared"):
        add_parent(doc_folder, "PARENT")


def test_add_parent_rejects_self(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="as a parent of itself"):
        add_parent(doc_folder, "DOC")


def test_remove_parent_removes_existing(doc_folder: Path) -> None:
    ok = remove_parent(doc_folder, "PARENT")
    assert ok is True
    doc = load_document(doc_folder)
    assert doc.parents == ()


def test_remove_parent_returns_false_when_absent(doc_folder: Path) -> None:
    assert remove_parent(doc_folder, "GHOST") is False


def test_add_attr_option_appends(doc_folder: Path) -> None:
    add_attr_option(doc_folder, "status", "Rejected")
    doc = load_document(doc_folder)
    assert "Rejected" in doc.attribute("status").options


def test_add_attr_option_rejects_duplicate(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="already declared"):
        add_attr_option(doc_folder, "status", "Draft")


def test_add_attr_option_rejects_non_enum(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not enum"):
        add_attr_option(doc_folder, "rationale", "x")


def test_add_attr_option_rejects_unknown_attr(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not declared"):
        add_attr_option(doc_folder, "madeup", "x")


# ---- attribute schema mutations ----

def test_add_attribute_enum(doc_folder: Path) -> None:
    add_attribute(doc_folder, "priority", "enum", True, ("Must", "Should"))
    doc = load_document(doc_folder)
    attr = doc.attribute("priority")
    assert attr is not None
    assert attr.kind == "enum"
    assert attr.required is True
    assert attr.options == ("Must", "Should")


def test_add_attribute_text(doc_folder: Path) -> None:
    add_attribute(doc_folder, "comment", "text", False)
    doc = load_document(doc_folder)
    attr = doc.attribute("comment")
    assert attr is not None
    assert attr.kind == "text"
    assert attr.required is False
    assert attr.options == ()


def test_add_attribute_rejects_duplicate(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="already declared"):
        add_attribute(doc_folder, "status", "text", False)


def test_add_attribute_rejects_invalid_name(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="invalid attribute name"):
        add_attribute(doc_folder, "bad name!", "text", False)


def test_add_attribute_rejects_invalid_kind(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="invalid kind"):
        add_attribute(doc_folder, "x", "bogus", False)


def test_add_attribute_rejects_options_on_text(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="text attributes cannot have options"):
        add_attribute(doc_folder, "x", "text", False, ("a",))


@pytest.mark.parametrize("kind", ["integer", "real", "boolean", "date"])
def test_add_attribute_non_string_kinds(doc_folder: Path, kind: str) -> None:
    add_attribute(doc_folder, f"x_{kind}", kind, False)
    doc = load_document(doc_folder)
    attr = doc.attribute(f"x_{kind}")
    assert attr is not None
    assert attr.kind == kind
    assert attr.options == ()


@pytest.mark.parametrize("kind", ["integer", "real", "boolean", "date"])
def test_add_attribute_rejects_options_on_non_enum(doc_folder: Path, kind: str) -> None:
    with pytest.raises(EditorError, match=f"{kind} attributes cannot have options"):
        add_attribute(doc_folder, "x", kind, False, ("a",))


def test_delete_attribute_strips_manifest_and_objects(doc_folder: Path) -> None:
    # `rationale` is set on DOC-1 in the fixture (kind=text).
    delete_attribute(doc_folder, "rationale")
    doc = load_document(doc_folder)
    assert doc.attribute("rationale") is None
    assert "rationale" not in doc.objects["DOC-1"].attrs
    assert "rationale" not in doc.objects["DOC-0"].attrs


def test_delete_attribute_returns_every_written_file(doc_folder: Path) -> None:
    # Callers commit exactly these paths; only DOC-1 carries `rationale`.
    assert delete_attribute(doc_folder, "rationale") == [
        doc_folder / "DOC.xml",
        doc_folder / "objects" / "DOC-1.xml",
    ]


def test_delete_attribute_strips_rules(tmp_path: Path) -> None:
    from mellon.editor import create_document

    folder = create_document(
        tmp_path,
        "RDOC",
        title="rules host",
        attr_schema=None,  # use DEFAULT_ATTR_SCHEMA + DEFAULT_RULES
    )
    # Sanity: DEFAULT_RULES references "rationale" in several directives.
    rules_before = load_document(folder).rules
    assert any("rationale" in r.directives for r in rules_before)

    written = delete_attribute(folder, "rationale")
    assert written == [folder / "RDOC.xml", folder / "RDOC.rules.xml"]
    doc = load_document(folder)
    assert doc.attribute("rationale") is None
    for r in doc.rules:
        assert "rationale" not in r.directives


def test_delete_attribute_drops_emptied_rule(tmp_path: Path) -> None:
    # Rule whose only directive is on the attribute being deleted should be
    # removed entirely.
    from mellon.editor import create_document

    folder = create_document(
        tmp_path,
        "MIN",
        title="rules-min",
        attr_schema=[
            {"name": "type", "kind": "enum", "required": True, "options": ["X", "Y"]},
            {"name": "note", "kind": "text", "required": False},
        ],
    )
    # Replace the auto-rules file with one that has a single-directive rule on `note`.
    (folder / "MIN.rules.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<rules>\n"
        '  <rule><when type="X"/><then><attr name="note" status="required"/></then></rule>\n'
        '  <rule><when type="Y"/><then>'
        '<attr name="note" status="forbidden"/>'
        '<attr name="type" status="required"/>'
        "</then></rule>\n"
        "</rules>\n",
        encoding="utf-8",
    )
    delete_attribute(folder, "note")
    doc = load_document(folder)
    # First rule should be gone; second rule should remain with just `type`.
    assert len(doc.rules) == 1
    assert doc.rules[0].directives == {"type": "required"}


def test_delete_attribute_rejects_unknown(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not declared"):
        delete_attribute(doc_folder, "madeup")


def test_remove_attr_option_blanks_matching_values(doc_folder: Path) -> None:
    # DOC-1 has type=Requirement in the fixture.
    remove_attr_option(doc_folder, "type", "Requirement")
    doc = load_document(doc_folder)
    attr = doc.attribute("type")
    assert "Requirement" not in attr.options
    # Element stays so the user sees an empty value (validator will flag it).
    assert doc.objects["DOC-1"].attrs.get("type", "<missing>") == ""
    # DOC-0's type=Heading is untouched.
    assert doc.objects["DOC-0"].attrs["type"] == "Heading"


def test_remove_attr_option_returns_every_written_file(doc_folder: Path) -> None:
    # Callers commit exactly these paths; only DOC-1 has type=Requirement.
    assert remove_attr_option(doc_folder, "type", "Requirement") == [
        doc_folder / "DOC.xml",
        doc_folder / "objects" / "DOC-1.xml",
    ]


def test_remove_attr_option_unused_option(doc_folder: Path) -> None:
    add_attr_option(doc_folder, "status", "Reviewed")
    remove_attr_option(doc_folder, "status", "Reviewed")
    doc = load_document(doc_folder)
    assert "Reviewed" not in doc.attribute("status").options


def test_remove_attr_option_rejects_text_attr(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not enum"):
        remove_attr_option(doc_folder, "rationale", "x")


def test_remove_attr_option_rejects_unknown(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not declared"):
        remove_attr_option(doc_folder, "status", "Nonsense")


# ---- change_attribute_required ----


def test_change_required_false_to_true(doc_folder: Path) -> None:
    # `rationale` is kind=text, required=False in the fixture.
    assert load_document(doc_folder).attribute("rationale").required is False
    change_attribute_required(doc_folder, "rationale", True)
    assert load_document(doc_folder).attribute("rationale").required is True


def test_change_required_true_to_false(doc_folder: Path) -> None:
    # `status` is required=True in the fixture.
    assert load_document(doc_folder).attribute("status").required is True
    change_attribute_required(doc_folder, "status", False)
    assert load_document(doc_folder).attribute("status").required is False


def test_change_required_idempotent(doc_folder: Path) -> None:
    # Flipping to the current value is a no-op success.
    change_attribute_required(doc_folder, "status", True)
    assert load_document(doc_folder).attribute("status").required is True


def test_change_required_unknown_attr_fails(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not declared"):
        change_attribute_required(doc_folder, "madeup", True)


def test_change_required_creates_validation_gap(doc_folder: Path) -> None:
    # `rationale` is optional; some objects don't carry it. Flipping it to
    # required should make validate emit ATTR_MISSING_REQUIRED.
    change_attribute_required(doc_folder, "rationale", True)
    doc = load_document(doc_folder)
    errors = validate(doc)
    codes = {e.code for e in errors}
    assert "ATTR_MISSING_REQUIRED" in codes


# ---- delete ----

def test_delete_unlinked_leaf(doc_folder: Path) -> None:
    # DOC-1 is a leaf in the outline; fixture has one outgoing link to PARENT
    # (no incoming, so no blockers). This should succeed and clean up.
    report = delete_object(doc_folder, "DOC-1")
    assert report.performed is True
    assert report.incoming == ()
    assert not (doc_folder / "objects" / "DOC-1.xml").exists()
    assert not (doc_folder / "links" / "DOC-1.xml").exists()
    doc = load_document(doc_folder)
    assert "DOC-1" not in doc.objects
    assert doc.outline_path("DOC-1") is None


def test_delete_refuses_with_children(doc_folder: Path) -> None:
    # DOC-0 has DOC-1 as a child.
    with pytest.raises(EditorError, match="has children"):
        delete_object(doc_folder, "DOC-0")


def test_delete_unknown_id(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="no object"):
        delete_object(doc_folder, "DOC-999")


def test_delete_blocked_by_same_doc_incoming(doc_folder: Path) -> None:
    # Make DOC-0 link to DOC-1 first, then try to delete DOC-1.
    add_link(doc_folder, "DOC-0", "DOC", "DOC-1", "refines")
    report = delete_object(doc_folder, "DOC-1")
    assert report.performed is False
    assert len(report.incoming) == 1
    src_doc, src_id, ltype = report.incoming[0]
    assert (src_doc, src_id, ltype) == ("DOC", "DOC-0", "refines")
    # File must still exist since we refused.
    assert (doc_folder / "objects" / "DOC-1.xml").exists()


def test_delete_force_strips_same_doc_incoming(doc_folder: Path) -> None:
    add_link(doc_folder, "DOC-0", "DOC", "DOC-1", "refines")
    report = delete_object(doc_folder, "DOC-1", force=True)
    assert report.performed is True
    assert len(report.incoming) == 1
    # DOC-0 had exactly one (now-stripped) link, so its links file is gone.
    assert not (doc_folder / "links" / "DOC-0.xml").exists()
    assert not (doc_folder / "objects" / "DOC-1.xml").exists()
    doc = load_document(doc_folder)
    assert "DOC-0" not in doc.links


def test_delete_force_strips_cross_doc_incoming(tmp_path: Path) -> None:
    _root, parent, doc, _c1 = _two_doc_repo(tmp_path)
    # Build an incoming link from DOC/DOC-1 -> PARENT/PARENT-42, then delete
    # PARENT-42. DOC's links file should be cleaned up as part of the cascade.
    add_link(doc, "DOC-1", "PARENT", "PARENT-42", "satisfies")
    assert (doc / "links" / "DOC-1.xml").exists()

    report = delete_object(parent, "PARENT-42", force=True)
    assert report.performed is True
    assert ("DOC", "DOC-1", "satisfies") in report.incoming
    assert not (doc / "links" / "DOC-1.xml").exists()
    assert not (parent / "objects" / "PARENT-42.xml").exists()


@pytest.mark.parametrize("force", [False, True])
def test_delete_refuses_while_a_sibling_doc_fails_to_load(
    tmp_path: Path, force: bool
) -> None:
    _root, parent, doc, _c1 = _two_doc_repo(tmp_path)
    add_link(doc, "DOC-1", "PARENT", "PARENT-42", "satisfies")
    # DOC no longer loads, so its link to PARENT-42 can't be seen; deleting
    # PARENT-42 would leave it dangling once DOC is fixed.
    _write_merge_conflict(doc / "objects" / "DOC-1.xml")

    with pytest.raises(EditorError, match="fails to load.*DOC: .*merge conflict"):
        delete_object(parent, "PARENT-42", force=force)
    assert (parent / "objects" / "PARENT-42.xml").exists()
    assert (doc / "links" / "DOC-1.xml").exists()


def test_set_description_replaces_text(doc_folder: Path) -> None:
    set_description(doc_folder, "DOC-1", "new single-line text")
    doc = load_document(doc_folder)
    assert doc.objects["DOC-1"].description == "new single-line text"


def test_set_description_multiline(doc_folder: Path) -> None:
    set_description(doc_folder, "DOC-1", "line one\nline two\nline three")
    doc = load_document(doc_folder)
    # lxml strips leading/trailing whitespace on text() during our loader
    # (_text strips), so multiline content round-trips with internal newlines intact.
    assert "line one" in doc.objects["DOC-1"].description
    assert "line three" in doc.objects["DOC-1"].description


def test_set_description_unknown_id(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="no object"):
        set_description(doc_folder, "DOC-999", "x")


# --- views ----------------------------------------------------------------


def test_create_view_writes_file_and_round_trips(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main View", ("status", "type"))
    assert slug == "main-view"
    path = doc_folder / "views" / "main-view.xml"
    assert path.is_file()

    doc = load_document(doc_folder)
    assert len(doc.views) == 1
    assert doc.views[0].slug == "main-view"
    assert doc.views[0].name == "Main View"
    assert doc.views[0].visible == ("status", "type")


def test_create_view_dedupes_visible_attrs(doc_folder: Path) -> None:
    create_view(doc_folder, "Dup", ["status", "type", "status"])
    doc = load_document(doc_folder)
    assert doc.views[0].visible == ("status", "type")


def test_create_view_collision_appends_suffix(doc_folder: Path) -> None:
    create_view(doc_folder, "Main View", ("status",))
    second = create_view(doc_folder, "Main View!", ("type",))
    assert second == "main-view-2"
    third = create_view(doc_folder, "main view", ())
    assert third == "main-view-3"


def test_create_view_rejects_empty_name(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="cannot be empty"):
        create_view(doc_folder, "", ("status",))
    with pytest.raises(EditorError, match="cannot be empty"):
        create_view(doc_folder, "   ", ("status",))


def test_create_view_rejects_unsluggable_name(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="empty slug"):
        create_view(doc_folder, "!!!", ("status",))


def test_delete_view_removes_file(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Tmp", ("status",))
    delete_view(doc_folder, slug)
    assert not (doc_folder / "views" / f"{slug}.xml").exists()
    doc = load_document(doc_folder)
    assert doc.views == ()


def test_delete_view_missing_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found"):
        delete_view(doc_folder, "nope")


def test_rename_view_changes_name_only(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main View", ("status",))
    new_slug = rename_view(doc_folder, slug, "Main View")
    # Same slug, just whitespace/case kept; file stays the same.
    assert new_slug == "main-view"
    doc = load_document(doc_folder)
    assert doc.views[0].name == "Main View"


def test_rename_view_changes_slug(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main View", ("status",))
    new_slug = rename_view(doc_folder, slug, "QA View")
    assert new_slug == "qa-view"
    assert not (doc_folder / "views" / "main-view.xml").exists()
    assert (doc_folder / "views" / "qa-view.xml").is_file()
    doc = load_document(doc_folder)
    assert doc.views[0].name == "QA View"
    assert doc.views[0].slug == "qa-view"
    # Visible attrs preserved across rename.
    assert doc.views[0].visible == ("status",)


def test_rename_view_handles_slug_collision(doc_folder: Path) -> None:
    create_view(doc_folder, "QA View", ("status",))
    other = create_view(doc_folder, "Main View", ("type",))
    new_slug = rename_view(doc_folder, other, "QA View")
    assert new_slug == "qa-view-2"
    assert (doc_folder / "views" / "qa-view-2.xml").is_file()


def test_rename_view_rejects_empty(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main", ("status",))
    with pytest.raises(EditorError, match="cannot be empty"):
        rename_view(doc_folder, slug, "")


def test_rename_view_missing_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found"):
        rename_view(doc_folder, "nope", "X")


def test_set_view_visible_replaces_order(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main", ("status", "type"))
    set_view_visible(doc_folder, slug, ("type", "status"))
    doc = load_document(doc_folder)
    assert doc.views[0].visible == ("type", "status")
    # Display name is preserved across the rewrite.
    assert doc.views[0].name == "Main"


def test_set_view_visible_dedupes(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main", ("status",))
    set_view_visible(doc_folder, slug, ["type", "status", "type", "status"])
    doc = load_document(doc_folder)
    assert doc.views[0].visible == ("type", "status")


def test_set_view_visible_grows_and_shrinks(doc_folder: Path) -> None:
    slug = create_view(doc_folder, "Main", ("status",))
    set_view_visible(doc_folder, slug, ("status", "type"))
    assert load_document(doc_folder).views[0].visible == ("status", "type")
    set_view_visible(doc_folder, slug, ())
    assert load_document(doc_folder).views[0].visible == ()


def test_set_view_visible_missing_slug_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found"):
        set_view_visible(doc_folder, "nope", ("status",))


# --- filters --------------------------------------------------------------


def _attr_rule(attr: str, op: str, value: str = "") -> FilterRule:
    return FilterRule(kind="attr", attr=attr, op=op, value=value)


def test_create_filter_writes_file_and_round_trips(doc_folder: Path) -> None:
    slug = create_filter(
        doc_folder,
        "High priority",
        rules=(_attr_rule("status", "eq", "Approved"),),
    )
    assert slug == "high-priority"
    path = doc_folder / "filters" / "high-priority.xml"
    assert path.is_file()

    doc = load_document(doc_folder)
    assert len(doc.filters) == 1
    filt = doc.filters[0]
    assert filt.slug == "high-priority"
    assert filt.name == "High priority"
    assert filt.combinator == "all"
    assert filt.keep_ancestors is True
    assert len(filt.rules) == 1
    assert filt.rules[0].kind == "attr"
    assert filt.rules[0].attr == "status"
    assert filt.rules[0].op == "eq"
    assert filt.rules[0].value == "Approved"


def test_create_filter_collision_appends_suffix(doc_folder: Path) -> None:
    create_filter(doc_folder, "Approved", rules=(_attr_rule("status", "eq", "x"),))
    second = create_filter(
        doc_folder, "approved", rules=(_attr_rule("status", "eq", "y"),)
    )
    assert second == "approved-2"


def test_create_filter_rejects_empty_name(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="cannot be empty"):
        create_filter(doc_folder, "")


def test_create_filter_rejects_bad_combinator(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="combinator must be"):
        create_filter(doc_folder, "Bad", combinator="maybe")


def test_create_filter_rejects_bad_rule_kind(doc_folder: Path) -> None:
    bad = FilterRule(kind="weird", attr="status", op="eq", value="x")
    with pytest.raises(EditorError, match="rule kind must be"):
        create_filter(doc_folder, "Bad", rules=(bad,))


def test_create_filter_rejects_attr_rule_without_attr(doc_folder: Path) -> None:
    bad = FilterRule(kind="attr", op="eq", value="x")
    with pytest.raises(EditorError, match="missing attribute"):
        create_filter(doc_folder, "Bad", rules=(bad,))


def test_create_filter_link_rule_op_must_be_present_or_absent(
    doc_folder: Path,
) -> None:
    bad = FilterRule(kind="has-outgoing", attr="PARENT", op="eq", value="x")
    with pytest.raises(EditorError, match="op must be one of"):
        create_filter(doc_folder, "Bad", rules=(bad,))


def test_delete_filter_removes_file(doc_folder: Path) -> None:
    slug = create_filter(doc_folder, "Tmp")
    delete_filter(doc_folder, slug)
    assert not (doc_folder / "filters" / f"{slug}.xml").exists()
    assert load_document(doc_folder).filters == ()


def test_delete_filter_missing_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found"):
        delete_filter(doc_folder, "nope")


def test_rename_filter_changes_name_only(doc_folder: Path) -> None:
    slug = create_filter(doc_folder, "Approved")
    new_slug = rename_filter(doc_folder, slug, "Approved")
    assert new_slug == "approved"
    assert load_document(doc_folder).filters[0].name == "Approved"


def test_rename_filter_changes_slug(doc_folder: Path) -> None:
    slug = create_filter(
        doc_folder, "Approved", rules=(_attr_rule("status", "eq", "Approved"),)
    )
    new_slug = rename_filter(doc_folder, slug, "High Priority")
    assert new_slug == "high-priority"
    assert not (doc_folder / "filters" / "approved.xml").exists()
    assert (doc_folder / "filters" / "high-priority.xml").is_file()
    doc = load_document(doc_folder)
    assert doc.filters[0].slug == "high-priority"
    assert doc.filters[0].name == "High Priority"
    # Rules are preserved across the rename.
    assert len(doc.filters[0].rules) == 1


def test_rename_filter_handles_collision(doc_folder: Path) -> None:
    create_filter(doc_folder, "Approved")
    other = create_filter(doc_folder, "Pending")
    new_slug = rename_filter(doc_folder, other, "Approved")
    assert new_slug == "approved-2"


def test_rename_filter_missing_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found"):
        rename_filter(doc_folder, "nope", "x")


def test_set_filter_rules_replaces_rules(doc_folder: Path) -> None:
    slug = create_filter(
        doc_folder, "Approved", rules=(_attr_rule("status", "eq", "Approved"),)
    )
    new_rules = (
        _attr_rule("status", "ne", "Approved"),
        FilterRule(kind="has-outgoing", attr="PARENT", op="present"),
    )
    set_filter_rules(
        doc_folder,
        slug,
        combinator="any",
        keep_ancestors=False,
        rules=new_rules,
    )
    doc = load_document(doc_folder)
    filt = doc.filters[0]
    # Display name preserved.
    assert filt.name == "Approved"
    assert filt.combinator == "any"
    assert filt.keep_ancestors is False
    assert len(filt.rules) == 2
    assert filt.rules[1].kind == "has-outgoing"
    assert filt.rules[1].attr == "PARENT"
    assert filt.rules[1].op == "present"


def test_set_filter_rules_missing_slug_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="not found"):
        set_filter_rules(doc_folder, "nope", "all", True, ())


def test_set_filter_rules_validates_combinator(doc_folder: Path) -> None:
    slug = create_filter(doc_folder, "X")
    with pytest.raises(EditorError, match="combinator must be"):
        set_filter_rules(doc_folder, slug, "maybe", True, ())


# --- figures -------------------------------------------------------------


_VALID_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" fill="blue"/>
</svg>
"""


def _write_svg(path: Path, content: bytes = _VALID_SVG) -> Path:
    path.write_bytes(content)
    return path


def test_figure_path_helper(doc_folder: Path) -> None:
    assert figure_path(doc_folder, "DOC-1") == doc_folder / "figures" / "DOC-1.svg"


def test_set_figure_creates_file(tmp_path: Path, doc_folder: Path) -> None:
    src = _write_svg(tmp_path / "diagram.svg")
    set_figure(doc_folder, "DOC-1", src)
    dest = doc_folder / "figures" / "DOC-1.svg"
    assert dest.is_file()
    assert dest.read_bytes() == _VALID_SVG


def test_set_figure_populates_loader_field(
    tmp_path: Path, doc_folder: Path
) -> None:
    src = _write_svg(tmp_path / "diagram.svg")
    set_figure(doc_folder, "DOC-1", src)
    doc = load_document(doc_folder)
    assert doc.objects["DOC-1"].figure_path == "figures/DOC-1.svg"
    # Sibling object without a figure stays None.
    assert doc.objects["DOC-0"].figure_path is None


def test_set_figure_overwrites_existing(
    tmp_path: Path, doc_folder: Path
) -> None:
    first = _write_svg(tmp_path / "first.svg", _VALID_SVG)
    set_figure(doc_folder, "DOC-1", first)
    second_bytes = _VALID_SVG.replace(b'fill="blue"', b'fill="red"')
    second = _write_svg(tmp_path / "second.svg", second_bytes)
    set_figure(doc_folder, "DOC-1", second)
    dest = doc_folder / "figures" / "DOC-1.svg"
    assert dest.read_bytes() == second_bytes


def test_set_figure_rejects_wrong_extension(
    tmp_path: Path, doc_folder: Path
) -> None:
    src = tmp_path / "diagram.png"
    src.write_bytes(b"\x89PNG\r\n")
    with pytest.raises(EditorError, match=".svg"):
        set_figure(doc_folder, "DOC-1", src)
    assert not (doc_folder / "figures").exists()


def test_set_figure_rejects_missing_source(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="does not exist"):
        set_figure(doc_folder, "DOC-1", Path("nope.svg"))


def test_set_figure_rejects_invalid_xml(
    tmp_path: Path, doc_folder: Path
) -> None:
    bad = tmp_path / "bad.svg"
    bad.write_bytes(b"<not really xml")
    with pytest.raises(EditorError, match="well-formed XML"):
        set_figure(doc_folder, "DOC-1", bad)


def test_set_figure_rejects_non_svg_root(
    tmp_path: Path, doc_folder: Path
) -> None:
    bad = tmp_path / "bad.svg"
    bad.write_bytes(b"<?xml version='1.0'?><html><body/></html>")
    with pytest.raises(EditorError, match="root must be <svg>"):
        set_figure(doc_folder, "DOC-1", bad)


def test_set_figure_rejects_unknown_obj(
    tmp_path: Path, doc_folder: Path
) -> None:
    src = _write_svg(tmp_path / "diagram.svg")
    with pytest.raises(EditorError, match="DOC-99"):
        set_figure(doc_folder, "DOC-99", src)


def test_clear_figure_removes_file_and_empty_dir(
    tmp_path: Path, doc_folder: Path
) -> None:
    src = _write_svg(tmp_path / "diagram.svg")
    set_figure(doc_folder, "DOC-1", src)
    assert (doc_folder / "figures" / "DOC-1.svg").is_file()
    removed = clear_figure(doc_folder, "DOC-1")
    assert removed is True
    assert not (doc_folder / "figures" / "DOC-1.svg").exists()
    # `figures/` had only this file, so the directory is cleaned up.
    assert not (doc_folder / "figures").exists()


def test_clear_figure_preserves_dir_with_other_figures(
    tmp_path: Path, doc_folder: Path
) -> None:
    src = _write_svg(tmp_path / "diagram.svg")
    set_figure(doc_folder, "DOC-0", src)
    set_figure(doc_folder, "DOC-1", src)
    clear_figure(doc_folder, "DOC-1")
    # DOC-0's figure remains, so the directory stays.
    assert (doc_folder / "figures" / "DOC-0.svg").is_file()
    assert (doc_folder / "figures").is_dir()


def test_clear_figure_missing_returns_false(doc_folder: Path) -> None:
    assert clear_figure(doc_folder, "DOC-1") is False


# --- tables ---------------------------------------------------------------


def _sample_table() -> Table:
    return Table(
        columns=(TableColumn("left"), TableColumn("right")),
        rows=(
            TableRow((TableCell("Signal"), TableCell("Latency")), header=True),
            TableRow((TableCell("OVERTEMP"), TableCell("200 ms"))),
        ),
    )


def test_set_table_round_trips_via_loader(doc_folder: Path) -> None:
    t = _sample_table()
    set_table(doc_folder, "DOC-1", t)
    assert load_document(doc_folder).objects["DOC-1"].table == t


def test_set_table_placed_after_description(doc_folder: Path) -> None:
    set_table(doc_folder, "DOC-1", _sample_table())
    xml = (doc_folder / "objects" / "DOC-1.xml").read_text(encoding="utf-8")
    assert xml.index("<description>") < xml.index("<table>")


def test_set_table_canonical_two_space_indent(doc_folder: Path) -> None:
    set_table(doc_folder, "DOC-1", _sample_table())
    xml = (doc_folder / "objects" / "DOC-1.xml").read_text(encoding="utf-8")
    assert "\n  <table>" in xml
    assert "\n    <columns>" in xml
    assert "\n      <col " in xml


def test_set_table_omits_default_spans(doc_folder: Path) -> None:
    set_table(doc_folder, "DOC-1", _sample_table())
    xml = (doc_folder / "objects" / "DOC-1.xml").read_text(encoding="utf-8")
    assert "colspan" not in xml and "rowspan" not in xml


def test_set_table_emits_span_when_merged(doc_folder: Path) -> None:
    t = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow((TableCell("wide", colspan=2),)),),
    )
    set_table(doc_folder, "DOC-1", t)
    xml = (doc_folder / "objects" / "DOC-1.xml").read_text(encoding="utf-8")
    assert 'colspan="2"' in xml


def test_set_table_replaces_existing(doc_folder: Path) -> None:
    set_table(doc_folder, "DOC-1", _sample_table())
    replacement = Table(
        columns=(TableColumn(),), rows=(TableRow((TableCell("solo"),)),)
    )
    set_table(doc_folder, "DOC-1", replacement)
    reloaded = load_document(doc_folder).objects["DOC-1"].table
    assert reloaded == replacement
    xml = (doc_folder / "objects" / "DOC-1.xml").read_text(encoding="utf-8")
    assert xml.count("<table>") == 1


def test_set_table_rejects_inconsistent_geometry(doc_folder: Path) -> None:
    bad = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow((TableCell("only-one"),)),),
    )
    with pytest.raises(EditorError, match="geometry"):
        set_table(doc_folder, "DOC-1", bad)


def test_set_table_rejects_bad_align(doc_folder: Path) -> None:
    bad = Table(columns=(TableColumn("sideways"),), rows=(TableRow((TableCell("x"),)),))
    with pytest.raises(EditorError, match="alignment"):
        set_table(doc_folder, "DOC-1", bad)


def test_set_table_unknown_object_raises(doc_folder: Path) -> None:
    with pytest.raises(EditorError, match="no object"):
        set_table(doc_folder, "DOC-404", _sample_table())


def test_clear_table_removes_element(doc_folder: Path) -> None:
    set_table(doc_folder, "DOC-1", _sample_table())
    assert clear_table(doc_folder, "DOC-1") is True
    assert load_document(doc_folder).objects["DOC-1"].table is None


def test_clear_table_no_table_returns_false(doc_folder: Path) -> None:
    assert clear_table(doc_folder, "DOC-1") is False


# --- write-path safety ------------------------------------------------------


def test_create_object_maps_page_break_and_writes_lf(doc_folder: Path) -> None:
    new_id = create_object(doc_folder, description="Page 1 & <2>\fPage 2")
    assert b"\r\n" not in (doc_folder / "objects" / f"{new_id}.xml").read_bytes()
    doc = load_document(doc_folder)
    assert doc.objects[new_id].description == "Page 1 & <2>\nPage 2"


def test_add_link_first_link_escapes_link_type(doc_folder: Path) -> None:
    # DOC-0 has no links file yet, so this goes through the new-file branch.
    add_link(doc_folder, "DOC-0", "PARENT", "PARENT-1", 'refines & "clarifies" <x>')
    assert b"\r\n" not in (doc_folder / "links" / "DOC-0.xml").read_bytes()
    (link,) = load_document(doc_folder).links["DOC-0"]
    assert link.type == 'refines & "clarifies" <x>'


def test_set_description_maps_vertical_tab_to_newline(doc_folder: Path) -> None:
    set_description(doc_folder, "DOC-1", "Line one\x0bLine two")
    assert load_document(doc_folder).objects["DOC-1"].description == "Line one\nLine two"


def test_set_description_normalises_crlf_and_cr(doc_folder: Path) -> None:
    set_description(doc_folder, "DOC-1", "one\r\ntwo\rthree")
    assert b"&#13;" not in (doc_folder / "objects" / "DOC-1.xml").read_bytes()
    assert load_document(doc_folder).objects["DOC-1"].description == "one\ntwo\nthree"


_BAD_CELL_TABLE = Table(
    columns=(TableColumn(),), rows=(TableRow((TableCell("bad\x1b"),)),)
)


@pytest.mark.parametrize(
    "write",
    [
        lambda f: create_object(f, description="bad\x1b"),
        lambda f: set_description(f, "DOC-1", "bad\x1b"),
        lambda f: set_attribute(f, "DOC-1", "rationale", "bad\x1b"),
        lambda f: set_metadata(f, "title", "bad\x1b"),
        lambda f: add_link(f, "DOC-0", "PARENT", "PARENT-1", "bad\x1b"),
        lambda f: set_table(f, "DOC-1", _BAD_CELL_TABLE),
        lambda f: create_view(f, "bad\x1b", ()),
        lambda f: create_filter(f, "bad\x1b"),
    ],
    ids=[
        "create_object", "set_description", "set_attribute", "set_metadata",
        "add_link", "set_table", "create_view", "create_filter",
    ],
)
def test_control_characters_rejected_before_any_write(doc_folder: Path, write) -> None:
    before = {p: p.read_bytes() for p in doc_folder.rglob("*") if p.is_file()}
    with pytest.raises(EditorError, match=r"U\+001B"):
        write(doc_folder)
    assert {p: p.read_bytes() for p in doc_folder.rglob("*") if p.is_file()} == before


def test_failed_write_keeps_original_and_cleans_temp(
    doc_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    obj_path = doc_folder / "objects" / "DOC-1.xml"
    original = obj_path.read_bytes()

    def fail_replace(src, dst):
        raise PermissionError("file is locked by another process")

    monkeypatch.setattr("mellon.editor.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        set_description(doc_folder, "DOC-1", "new text")
    assert obj_path.read_bytes() == original
    assert sorted(p.name for p in obj_path.parent.iterdir()) == ["DOC-0.xml", "DOC-1.xml"]


def test_write_retries_while_target_is_briefly_held_open(
    doc_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    attempts = []

    def held_twice(src, dst):
        attempts.append(dst)
        if len(attempts) < 3:
            raise PermissionError("file is open in another process")
        real_replace(src, dst)

    monkeypatch.setattr("mellon.editor.os.replace", held_twice)
    monkeypatch.setattr("mellon.editor.time.sleep", lambda _s: None)
    set_description(doc_folder, "DOC-1", "new text")
    assert len(attempts) == 3
    assert load_document(doc_folder).objects["DOC-1"].description == "new text"


@pytest.mark.parametrize("name", ["../x.xml", "sub/x.xml", ".."])
def test_child_path_refuses_names_outside_folder(doc_folder: Path, name: str) -> None:
    from mellon.editor import _child_path

    with pytest.raises(EditorError, match="invalid file name"):
        _child_path(doc_folder / "links", name)


@pytest.mark.parametrize(
    "name",
    ["\\\\mellon-test-host\\share", "//mellon-test-host/share", "C:x.xml", "x.xml:stream", ""],
)
def test_child_path_refuses_a_share_or_drive_without_looking_it_up(
    tmp_path: Path, doc_folder: Path, monkeypatch, name: str
) -> None:
    """MELLON-STORE-SRS-223: a document ID such as //host/share is refused from
    its text alone; looking it up would reach the share."""
    from mellon.editor import _child_path

    _inside, outside = guard_paths_outside(monkeypatch, tmp_path)
    with pytest.raises(EditorError, match="invalid file name"):
        _child_path(doc_folder / "links", name)
    assert outside == []


@pytest.mark.parametrize(
    "call",
    [
        lambda f, s: delete_view(f, s),
        lambda f, s: rename_view(f, s, "Renamed"),
        lambda f, s: set_view_visible(f, s, ("status",)),
        lambda f, s: delete_filter(f, s),
        lambda f, s: rename_filter(f, s, "Renamed"),
        lambda f, s: set_filter_rules(f, s, "all", True, ()),
    ],
    ids=[
        "delete_view", "rename_view", "set_view_visible",
        "delete_filter", "rename_filter", "set_filter_rules",
    ],
)
def test_view_and_filter_slugs_cannot_escape_their_folder(doc_folder: Path, call) -> None:
    (doc_folder / "views").mkdir()
    (doc_folder / "filters").mkdir()
    target = doc_folder / "objects" / "DOC-1.xml"
    original = target.read_bytes()
    with pytest.raises(EditorError, match="invalid slug"):
        call(doc_folder, "../objects/DOC-1")
    assert target.read_bytes() == original


def test_clear_figure_requires_known_object(tmp_path: Path, doc_folder: Path) -> None:
    (doc_folder / "figures").mkdir()
    outside = _write_svg(tmp_path / "outside.svg")
    with pytest.raises(EditorError, match="no object"):
        clear_figure(doc_folder, "../../outside")
    assert outside.is_file()


def test_remove_link_source_id_cannot_escape_links_folder(tmp_path: Path, doc_folder: Path) -> None:
    other_links = tmp_path / "OTHER" / "links" / "OTHER-1.xml"
    other_links.parent.mkdir(parents=True)
    other_links.write_text(
        '<links source="OTHER-1">\n'
        '  <link target-doc="PARENT" target-id="PARENT-42" type="satisfies"/>\n'
        "</links>\n",
        encoding="utf-8",
    )
    with pytest.raises(EditorError, match="invalid file name"):
        remove_link(doc_folder, "../../OTHER/links/OTHER-1", "PARENT", "PARENT-42")
    assert other_links.is_file()


def test_delete_object_removes_figure_so_reused_id_starts_clean(
    tmp_path: Path, doc_folder: Path
) -> None:
    set_figure(doc_folder, "DOC-1", _write_svg(tmp_path / "diagram.svg"))
    fig = doc_folder / "figures" / "DOC-1.svg"
    report = delete_object(doc_folder, "DOC-1")
    assert not fig.exists()
    assert fig in report.removed_files
    # The fixture's manifest records no next object number, as an older
    # Mellon leaves it, so DOC-1, the highest id, is handed out again.
    assert create_object(doc_folder, description="unrelated") == "DOC-1"
    assert load_document(doc_folder).objects["DOC-1"].figure_path is None


def test_delete_object_removes_symlinked_figure(tmp_path: Path, doc_folder: Path) -> None:
    shared = _write_svg(tmp_path / "shared.svg")
    link = doc_folder / "figures" / "DOC-1.svg"
    link.parent.mkdir()
    try:
        link.symlink_to(shared)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this platform")
    report = delete_object(doc_folder, "DOC-1")
    assert link in report.removed_files
    assert not link.is_symlink()
    assert shared.is_file()


def test_delete_object_failing_figure_removal_changes_nothing(
    tmp_path: Path, doc_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_link(doc_folder, "DOC-0", "DOC", "DOC-1", "refines")
    set_figure(doc_folder, "DOC-1", _write_svg(tmp_path / "diagram.svg"))
    before = {p: p.read_bytes() for p in doc_folder.rglob("*") if p.is_file()}

    def held_open(*_args):
        raise PermissionError("figure is open in a viewer")

    monkeypatch.setattr("mellon.editor._remove_figure_file", held_open)
    with pytest.raises(PermissionError):
        delete_object(doc_folder, "DOC-1", force=True)
    assert {p: p.read_bytes() for p in doc_folder.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "remove_last_child",
    [
        lambda f: move_object(f, "DOC-1", to_top=True),
        lambda f: delete_object(f, "DOC-1"),
    ],
    ids=["move_object", "delete_object"],
)
def test_emptied_outline_parent_has_no_whitespace_residue(
    doc_folder: Path, remove_last_child
) -> None:
    # DOC-1 is DOC-0's only child in the fixture outline.
    remove_last_child(doc_folder)
    assert '<ref id="DOC-0"/>' in (doc_folder / "DOC.xml").read_text(encoding="utf-8")


# --- write journal: all-or-nothing operations, undo data --------------------


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _fail_writes(monkeypatch, when) -> None:
    """Make `_write_tree` raise OSError for the calls where
    ``when(call_number, path)`` is true (call numbers start at 1)."""
    from mellon import editor

    real = editor._write_tree
    calls = {"n": 0}

    def flaky(tree, path):
        calls["n"] += 1
        if when(calls["n"], path):
            raise OSError(f"disk full writing {path.name}")
        real(tree, path)

    monkeypatch.setattr(editor, "_write_tree", flaky)


@pytest.mark.parametrize(
    "operation",
    [
        # Both write the manifest, then DOC-1: the second write fails.
        lambda f: delete_attribute(f, "rationale"),
        lambda f: remove_attr_option(f, "type", "Requirement"),
    ],
    ids=["delete_attribute", "remove_attr_option"],
)
def test_multi_file_operation_failing_midway_changes_nothing(
    doc_folder: Path, monkeypatch, operation
) -> None:
    before = _snapshot(doc_folder)
    _fail_writes(monkeypatch, lambda n, _path: n == 2)
    with pytest.raises(OSError, match="disk full writing DOC-1.xml"):
        operation(doc_folder)
    assert _snapshot(doc_folder) == before


def test_delete_object_failing_last_puts_back_other_documents(
    mcp_repo: Path, monkeypatch
) -> None:
    """PARENT-1 has an incoming link from CHILD-0. By the time the manifest
    write fails, CHILD's link file and PARENT-1's own file are already
    gone; both come back."""
    before = _snapshot(mcp_repo)
    _fail_writes(monkeypatch, lambda _n, path: path.name == "PARENT.xml")
    with pytest.raises(OSError, match="disk full writing PARENT.xml"):
        delete_object(mcp_repo / "PARENT", "PARENT-1", force=True)
    assert _snapshot(mcp_repo) == before


def test_journal_reverts_and_reapplies(doc_folder: Path) -> None:
    from mellon.editor import journal

    before = _snapshot(doc_folder)
    with journal(rollback=False) as j:
        set_description(doc_folder, "DOC-1", "Changed.")
        set_description(doc_folder, "DOC-1", "Changed twice.")
        new_id = create_object(doc_folder, description="New.")
    after = _snapshot(doc_folder)
    assert sorted(p.name for p in j.paths) == sorted(
        ["DOC-1.xml", "DOC.xml", f"{new_id}.xml"]
    )
    j.revert()
    # Each file is back, except that the manifest keeps the next object
    # number the create raised (MELLON-EDIT-SRS-346).
    undone = _snapshot(doc_folder)
    manifest = doc_folder / "DOC.xml"
    assert {p: b for p, b in undone.items() if p != manifest} == {
        p: b for p, b in before.items() if p != manifest
    }
    assert _without_next_number(undone[manifest]) == _without_next_number(before[manifest])
    j.reapply()
    assert _snapshot(doc_folder) == after


def _without_next_number(manifest: bytes) -> bytes:
    root = etree.fromstring(manifest)
    root.attrib.pop("next-object-number", None)
    return etree.tostring(root)


def _next_number(folder: Path) -> str | None:
    return etree.parse(str(folder / f"{folder.name}.xml")).getroot().get("next-object-number")


def test_edit_srs_346_undoing_a_create_keeps_the_next_object_number(doc_folder: Path) -> None:
    """MELLON-EDIT-SRS-346: undo never lowers the next object number, so the
    undone object's ID isn't given to the next new object."""
    from mellon.editor import journal

    with journal(rollback=False) as step:
        first = create_object(doc_folder, description="First.")
    raised = _next_number(doc_folder)
    step.revert()

    assert first not in load_document(doc_folder).objects
    assert _next_number(doc_folder) == raised
    assert create_object(doc_folder, description="Second.") != first


def test_edit_srs_346_redo_and_undo_again_keep_the_number(doc_folder: Path) -> None:
    """MELLON-EDIT-SRS-346: the kept number doesn't stop a redo, or a second
    undo, from finding the files as the step left them."""
    from mellon.editor import journal

    with journal(rollback=False) as step:
        first = create_object(doc_folder, description="First.")
    raised = _next_number(doc_folder)
    step.revert()
    step.reapply()
    assert first in load_document(doc_folder).objects
    step.revert()

    assert first not in load_document(doc_folder).objects
    assert _next_number(doc_folder) == raised
    assert allocate_next_id(load_document(doc_folder)) != first


def test_edit_srs_346_undo_keeps_a_number_raised_since_the_step(doc_folder: Path) -> None:
    """MELLON-EDIT-SRS-346: undoing a step whose manifest holds a lower number
    than the manifest now keeps the higher one."""
    from mellon.editor import journal

    with journal(rollback=False) as step:
        set_description(doc_folder, "DOC-1", "Changed.")
        create_object(doc_folder, description="First.")
    with journal(rollback=False) as later:
        create_object(doc_folder, description="Second.")
    raised = _next_number(doc_folder)
    later.revert()
    step.revert()

    assert _next_number(doc_folder) == raised
    assert load_document(doc_folder).objects["DOC-1"].description != "Changed."


def test_journal_revert_refuses_after_an_outside_change(doc_folder: Path) -> None:
    from mellon.editor import journal

    with journal(rollback=False) as j:
        set_description(doc_folder, "DOC-1", "Mine.")
        set_description(doc_folder, "DOC-0", "Also mine.")
    # Someone else (another window, MCP, a git pull) edits DOC-0 afterwards.
    set_description(doc_folder, "DOC-0", "Theirs.")
    snap = _snapshot(doc_folder)
    with pytest.raises(EditorError, match="can't undo: changed on disk since"):
        j.revert()
    assert _snapshot(doc_folder) == snap  # nothing half-undone


def test_journal_figure_round_trip_and_unchanged_files(
    doc_folder: Path, tmp_path: Path
) -> None:
    """Undoing an attach removes the figure (and the emptied figures/
    folder); a file written back to its old bytes isn't part of the step."""
    from mellon.editor import journal

    svg = tmp_path / "f.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    set_description(doc_folder, "DOC-0", "Top heading")  # canonical bytes
    with journal(rollback=False) as j:
        set_figure(doc_folder, "DOC-1", svg)
        set_description(doc_folder, "DOC-0", "Temporary")
        set_description(doc_folder, "DOC-0", "Top heading")
    assert [p.name for p in j.paths] == ["DOC-1.svg"]
    j.revert()
    assert not (doc_folder / "figures").exists()
    j.reapply()
    assert figure_path(doc_folder, "DOC-1").read_bytes() == svg.read_bytes()


def test_inner_failure_rolls_back_only_itself(
    doc_folder: Path, monkeypatch
) -> None:
    from mellon.editor import journal

    with journal(rollback=False) as outer:
        set_description(doc_folder, "DOC-1", "Kept.")
        _fail_writes(monkeypatch, lambda n, _path: n == 2)
        with pytest.raises(OSError):
            delete_attribute(doc_folder, "rationale")
    doc = load_document(doc_folder)
    assert doc.objects["DOC-1"].description == "Kept."
    assert doc.attribute("rationale") is not None
    assert [p.name for p in outer.paths] == ["DOC-1.xml"]


# --- object IDs are never given out twice ----------------------------------
#
# Written blind, from MELLON-STORE-SRS-1, MELLON-EDIT-SRS-1, -2 and the
# definitions alone (object ID `<document ID>-<n>`, object number `n`); the
# expected values come from those, never from the code's output. The helpers
# are shared with test_loader, test_validator and test_merge.

DOC = "DOC"
ATTR = "next-object-number"


# --------------------------------------------------------------------------
# Helpers: reading and hand-editing the on-disk format
# --------------------------------------------------------------------------


def manifest_path(folder: Path) -> Path:
    return folder / f"{folder.name}.xml"


def manifest_root(folder: Path) -> etree._Element:
    return etree.parse(str(manifest_path(folder))).getroot()


def read_next(folder: Path) -> str | None:
    """The manifest's ``next-object-number`` attribute, or None when absent."""
    return manifest_root(folder).get(ATTR)


def write_next(folder: Path, value: str | None) -> None:
    """Hand-edit the manifest's number, as a person or an older Mellon would.

    ``None`` removes the attribute, which is how a manifest written by a
    Mellon from before MELLON-STORE-SRS-1 looks.
    """
    path = manifest_path(folder)
    tree = etree.parse(str(path))
    root = tree.getroot()
    if value is None:
        if ATTR in root.attrib:
            del root.attrib[ATTR]
    else:
        root.set(ATTR, value)
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8") + b"\n")


def number_of(obj_id: str, doc_id: str = DOC) -> int:
    """The object number ``n`` of an object ID ``<document ID>-<n>``."""
    match = re.fullmatch(re.escape(doc_id) + r"-([0-9]+)", obj_id)
    assert match, f"object ID {obj_id!r} is not of the form {doc_id}-<n>"
    return int(match.group(1))


def new_document(root: Path, doc_id: str = DOC) -> Path:
    return editor.create_document(root, doc_id, title=f"{doc_id} title")


def populate(folder: Path, count: int, *, label: str = "object") -> list[str]:
    return [editor.create_object(folder, description=f"{label} {i}") for i in range(count)]


def delete(folder: Path, obj_id: str) -> None:
    report = editor.delete_object(folder, obj_id, force=True)
    assert report.performed, f"setup: deleting {obj_id} was not performed"


def object_file(folder: Path, obj_id: str) -> Path:
    return folder / "objects" / f"{obj_id}.xml"


# --------------------------------------------------------------------------
# MELLON-STORE-SRS-1: where the number is kept
# --------------------------------------------------------------------------


def test_store_srs_1_create_records_number_on_document_element(tmp_path):
    """MELLON-STORE-SRS-1: after creating an object, the manifest's root
    ``document`` element carries the ``next-object-number`` attribute."""
    folder = new_document(tmp_path)
    editor.create_object(folder, description="first")
    root = manifest_root(folder)
    assert etree.QName(root).localname == "document", (
        f"manifest root element is {root.tag!r}, expected 'document'"
    )
    assert root.get(ATTR) is not None, (
        "after creating an object the manifest's document element has no next-object-number attribute"
    )

def test_store_srs_1_manifest_without_number_counts_as_0(tmp_path):
    """MELLON-STORE-SRS-1 with MELLON-EDIT-SRS-1: an empty document whose
    manifest has no number counts as 0, so its first object is number 0."""
    folder = new_document(tmp_path)
    write_next(folder, None)
    new_id = editor.create_object(folder, description="first")
    assert number_of(new_id) == 0, (
        f"first object of an empty document without next-object-number is {new_id}, expected {DOC}-0"
    )


MANIFEST_EDITS = {
    "set_metadata": lambda folder, ids: editor.set_metadata(folder, "title", "Renamed"),
    "move_object": lambda folder, ids: editor.move_object(folder, ids[2], before=ids[0]),
    "add_attribute": lambda folder, ids: editor.add_attribute(folder, "priority", "text", False),
    "delete_lowest_object": lambda folder, ids: delete(folder, ids[0]),
    "delete_highest_object": lambda folder, ids: delete(folder, ids[-1]),
}


@pytest.mark.parametrize("edit", sorted(MANIFEST_EDITS))
def test_store_srs_1_other_manifest_edits_keep_the_number(tmp_path, edit):
    """MELLON-STORE-SRS-1 ('The manifest shall hold the document's next object
    number'), and for deletion MELLON-EDIT-SRS-2's rationale ('Deleting an
    object leaves the number as it is'): an editor operation that rewrites the
    manifest for another reason leaves the recorded number unchanged.

    Assumption: an edit other than creating an object must not change the
    number; no requirement names one that does.
    """
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    write_next(folder, "42")
    MANIFEST_EDITS[edit](folder, ids)
    assert read_next(folder) == "42", (
        f"{edit} changed next-object-number from '42' to {read_next(folder)!r}"
    )


# --------------------------------------------------------------------------
# MELLON-EDIT-SRS-1: the number a new object gets
# --------------------------------------------------------------------------


def test_edit_srs_1_first_object_of_fresh_document_is_number_0(tmp_path):
    """MELLON-EDIT-SRS-1: a fresh document's next object number is 0 (none
    recorded counts as 0) and it has no objects, so the first object is 0.

    Assumption: with no objects, 'one more than the highest object number'
    contributes nothing, so only the next object number decides.
    """
    folder = new_document(tmp_path)
    new_id = editor.create_object(folder, description="first")
    assert new_id == f"{DOC}-0", f"first object of a fresh document is {new_id}, expected {DOC}-0"


def test_edit_srs_1_created_object_file_holds_the_returned_id(tmp_path):
    """MELLON-EDIT-SRS-1: the number given is the one on disk: the new object's
    file is ``objects/<id>.xml`` and its ``<object id>`` is that ID."""
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    delete(folder, ids[-1])
    new_id = editor.create_object(folder, description="after delete")
    path = object_file(folder, new_id)
    assert path.is_file(), f"no object file at {path} for the created object {new_id}"
    on_disk = etree.parse(str(path)).getroot().get("id")
    assert on_disk == new_id, f"object file {path.name} holds id {on_disk!r}, expected {new_id!r}"


def test_edit_srs_1_empty_document_with_recorded_number_uses_it(tmp_path):
    """MELLON-EDIT-SRS-1: an empty document whose manifest records 7 gives its
    first object number 7."""
    folder = new_document(tmp_path)
    write_next(folder, "7")
    new_id = editor.create_object(folder, description="first")
    assert new_id == f"{DOC}-7", f"with next-object-number 7 and no objects got {new_id}, expected {DOC}-7"


def test_edit_srs_1_after_deleting_highest_object_its_number_is_not_given_again(tmp_path):
    """MELLON-EDIT-SRS-1 with -2: objects 0, 1, 2 leave the number at 3; deleting
    2 leaves it at 3, so the next object is 3, not 2 (the defect being fixed)."""
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    highest = max(number_of(i) for i in ids)
    delete(folder, ids[-1])
    new_id = editor.create_object(folder, description="after delete")
    assert number_of(new_id) == highest + 1, (
        f"after deleting {ids[-1]} the new object is {new_id}, expected {DOC}-{highest + 1}"
    )


def test_edit_srs_1_after_deleting_several_highest_objects(tmp_path):
    """MELLON-EDIT-SRS-1 with -2: create 0..5, delete 5, 3 and 4; the next object
    is one more than the highest ever created, 6."""
    folder = new_document(tmp_path)
    ids = populate(folder, 6)
    highest = max(number_of(i) for i in ids)
    for obj_id in (ids[5], ids[3], ids[4]):
        delete(folder, obj_id)
    new_id = editor.create_object(folder, description="after deletes")
    assert number_of(new_id) == highest + 1, (
        f"after deleting the three highest objects the new object is {new_id}, expected {DOC}-{highest + 1}"
    )


def test_edit_srs_1_after_deleting_every_object(tmp_path):
    """MELLON-EDIT-SRS-1 with -2: create 0..2 and delete them all; the document
    is empty but its number is 3, so the next object is 3, not 0."""
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    highest = max(number_of(i) for i in ids)
    for obj_id in ids:
        delete(folder, obj_id)
    new_id = editor.create_object(folder, description="after emptying")
    assert number_of(new_id) == highest + 1, (
        f"after deleting every object the new object is {new_id}, expected {DOC}-{highest + 1}"
    )


def test_edit_srs_1_sub_object_after_deleting_highest_sibling(tmp_path):
    """MELLON-EDIT-SRS-1: numbering is per document, whatever the parent:
    a sub-object created after its highest-numbered sibling was deleted still
    gets one more than the highest ever created."""
    folder = new_document(tmp_path)
    parent = editor.create_object(folder, description="parent")
    children = [editor.create_object(folder, parent_id=parent, description=f"child {i}") for i in range(2)]
    highest = max(number_of(i) for i in [parent, *children])
    delete(folder, children[-1])
    new_id = editor.create_object(folder, parent_id=parent, description="new child")
    assert number_of(new_id) == highest + 1, (
        f"new sub-object after deleting {children[-1]} is {new_id}, expected {DOC}-{highest + 1}"
    )


def test_edit_srs_1_manifest_without_number_uses_highest_present(tmp_path):
    """MELLON-EDIT-SRS-1: a manifest written without next-object-number (counts
    as 0) with objects 0..2 present gives max(0, 2 + 1) = 3."""
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    highest = max(number_of(i) for i in ids)
    write_next(folder, None)
    new_id = editor.create_object(folder, description="older manifest")
    assert number_of(new_id) == highest + 1, (
        f"manifest without next-object-number and highest object {highest}: got {new_id}, "
        f"expected {DOC}-{highest + 1}"
    )


def test_edit_srs_1_object_numbers_compare_as_whole_numbers(tmp_path):
    """MELLON-EDIT-SRS-1 and the definition of object number (a whole number):
    with objects 0..10 and no recorded number, the highest is 10 (not '9' by
    text order), so the new object is 11."""
    folder = new_document(tmp_path)
    populate(folder, 11)
    write_next(folder, None)
    new_id = editor.create_object(folder, description="twelfth")
    assert new_id == f"{DOC}-11", f"with objects {DOC}-0..{DOC}-10 got {new_id}, expected {DOC}-11"


def test_edit_srs_1_document_id_with_hyphen_and_digits(tmp_path):
    """MELLON-EDIT-SRS-1 and the definitions (document ID may hold hyphens and
    digits; object ID is '<document ID>-<n>'): in document SYS-2 with objects
    SYS-2-0..SYS-2-2 and no recorded number, the new object is SYS-2-3."""
    doc_id = "SYS-2"
    folder = new_document(tmp_path, doc_id)
    populate(folder, 3)
    write_next(folder, None)
    new_id = editor.create_object(folder, description="fourth")
    assert new_id == f"{doc_id}-3", f"in document {doc_id} got {new_id}, expected {doc_id}-3"


# Objects 0..4 are present (highest 4, so highest + 1 = 5). Each case is
# (recorded number, expected new object number) by max(recorded, 5).
RECORDED_VS_HIGHEST = [
    ("0", 5),   # recorded 0 (as an older Mellon leaves it): highest wins
    ("3", 5),   # recorded lower than the highest present
    ("4", 5),   # just below highest + 1
    ("5", 5),   # exactly highest + 1
    ("6", 6),   # just above highest + 1: recorded wins
    ("10", 10),  # well above any object present
    ("9223372036854775808", 9223372036854775808),  # 2**63, past a 64-bit integer
]


@pytest.mark.parametrize(("recorded", "expected"), RECORDED_VS_HIGHEST)
def test_edit_srs_1_higher_of_recorded_and_highest_plus_one(tmp_path, recorded, expected):
    """MELLON-EDIT-SRS-1 (T2, T7, T8): with objects 0..4 present the new object
    gets max(recorded number, 5)."""
    folder = new_document(tmp_path)
    ids = populate(folder, 5)
    assert max(number_of(i) for i in ids) == 4, f"setup: expected objects {DOC}-0..{DOC}-4, got {ids}"
    write_next(folder, recorded)
    new_id = editor.create_object(folder, description="new")
    assert number_of(new_id) == expected, (
        f"with next-object-number {recorded} and highest object 4 got {new_id}, expected {DOC}-{expected}"
    )


@pytest.mark.parametrize("recorded", ["abc", "-1", "1.5", ""])
def test_edit_srs_1_invalid_recorded_number_still_gives_an_unused_id(tmp_path, recorded):
    """MELLON-EDIT-SRS-1 with MELLON-STORE-SRS-2's rationale ('creating objects
    stays safe because the highest object number present is also taken into
    account'): with objects 0..2 and a damaged number, the new object's number
    is at least 3, so no present ID is given out again.

    Assumption (requirement gap): creating still succeeds with a damaged
    number, and nothing fixes which number at or above 3 it gets.
    """
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    highest = max(number_of(i) for i in ids)
    write_next(folder, recorded)
    new_id = editor.create_object(folder, description="new")
    assert number_of(new_id) > highest, (
        f"with next-object-number {recorded!r} and highest object {highest} got {new_id}, "
        f"which is not above the highest present"
    )


# --------------------------------------------------------------------------
# MELLON-EDIT-SRS-2: the number recorded after creating
# --------------------------------------------------------------------------


def test_edit_srs_2_number_after_first_object_is_1(tmp_path):
    """MELLON-EDIT-SRS-2: after the first object (number 0) the manifest
    records 1."""
    folder = new_document(tmp_path)
    editor.create_object(folder, description="first")
    assert read_next(folder) == "1", f"after creating {DOC}-0 next-object-number is {read_next(folder)!r}, expected '1'"


def test_edit_srs_2_number_after_create_following_delete_of_highest(tmp_path):
    """MELLON-EDIT-SRS-2: create 0..2, delete 2, create (gets 3 by -1): the
    manifest then records 4."""
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    delete(folder, ids[-1])
    new_id = editor.create_object(folder, description="after delete")
    expected = str(number_of(new_id) + 1)
    assert read_next(folder) == expected, (
        f"after creating {new_id} next-object-number is {read_next(folder)!r}, expected {expected!r}"
    )


def test_edit_srs_2_number_written_when_manifest_had_none(tmp_path):
    """MELLON-EDIT-SRS-2: a manifest without the attribute gets one more than
    the new object's number when an object is created."""
    folder = new_document(tmp_path)
    populate(folder, 3)
    write_next(folder, None)
    new_id = editor.create_object(folder, description="older manifest")
    expected = str(number_of(new_id) + 1)
    assert read_next(folder) == expected, (
        f"after creating {new_id} in a manifest without the attribute, next-object-number is "
        f"{read_next(folder)!r}, expected {expected!r}"
    )


@pytest.mark.parametrize(("recorded", "expected_new"), RECORDED_VS_HIGHEST)
def test_edit_srs_2_number_is_one_more_than_new_object(tmp_path, recorded, expected_new):
    """MELLON-EDIT-SRS-2: whichever term of MELLON-EDIT-SRS-1 decided the new
    number, the manifest afterwards records that number plus one."""
    folder = new_document(tmp_path)
    populate(folder, 5)
    write_next(folder, recorded)
    new_id = editor.create_object(folder, description="new")
    expected = str(number_of(new_id) + 1)
    assert read_next(folder) == expected, (
        f"recorded {recorded}, created {new_id}: next-object-number is {read_next(folder)!r}, expected {expected!r}"
    )


@pytest.mark.parametrize("recorded", ["abc", "-1", "1.5", ""])
def test_edit_srs_2_damaged_number_replaced_on_create(tmp_path, recorded):
    """MELLON-EDIT-SRS-2: creating an object sets the number to one more than
    the new object's, so a damaged number is replaced by a whole number."""
    folder = new_document(tmp_path)
    populate(folder, 3)
    write_next(folder, recorded)
    new_id = editor.create_object(folder, description="new")
    expected = str(number_of(new_id) + 1)
    assert read_next(folder) == expected, (
        f"damaged number {recorded!r}, created {new_id}: next-object-number is "
        f"{read_next(folder)!r}, expected {expected!r}"
    )


# --------------------------------------------------------------------------
# MELLON-EDIT-SRS-1, -2 with generated inputs (T11)
# --------------------------------------------------------------------------

SEEDS = range(8)
STEPS = 30


def random_sequence(folder: Path, seed: int):
    """Drive a seeded random run of creates and deletes on one document.

    Yields ``(step, op, obj_id, highest_ever)`` after each operation, where
    ``highest_ever`` is the highest object number created so far. Deletes
    favour the highest-numbered object, which is the case that reused IDs.
    """
    rng = random.Random(seed)
    present: list[str] = []
    highest_ever: int | None = None
    for step in range(STEPS):
        roll = rng.random()
        if not present or roll < 0.5:
            obj_id = editor.create_object(folder, description=f"seed {seed} step {step}")
            present.append(obj_id)
            number = number_of(obj_id)
            yield step, "create", obj_id, highest_ever
            highest_ever = number if highest_ever is None else max(highest_ever, number)
        else:
            if roll < 0.8:
                obj_id = max(present, key=number_of)
            else:
                obj_id = rng.choice(present)
            delete(folder, obj_id)
            present.remove(obj_id)
            yield step, "delete", obj_id, highest_ever


@pytest.mark.parametrize("seed", SEEDS)
def test_edit_srs_1_generated_no_object_id_given_out_twice(tmp_path, seed):
    """MELLON-EDIT-SRS-1, -2 and the definition of object ID ('never given to a
    second object in the same document'): over a random run of creates and
    deletes, every created ID is new."""
    folder = new_document(tmp_path)
    issued: set[str] = set()
    for step, op, obj_id, _ in random_sequence(folder, seed):
        if op == "create":
            assert obj_id not in issued, f"seed {seed} step {step}: {obj_id} was given out a second time"
            issued.add(obj_id)


@pytest.mark.parametrize("seed", SEEDS)
def test_edit_srs_1_generated_new_number_is_one_more_than_highest_ever(tmp_path, seed):
    """MELLON-EDIT-SRS-1 with -2: since each create records its number plus one
    and deletes leave it, each new object's number is one more than the highest
    ever created (0 for the first)."""
    folder = new_document(tmp_path)
    for step, op, obj_id, highest_before in random_sequence(folder, seed):
        if op == "create":
            expected = 0 if highest_before is None else highest_before + 1
            assert number_of(obj_id) == expected, (
                f"seed {seed} step {step}: created {obj_id}, expected {DOC}-{expected}"
            )


@pytest.mark.parametrize("seed", SEEDS)
def test_edit_srs_2_generated_number_is_one_more_than_highest_ever(tmp_path, seed):
    """MELLON-EDIT-SRS-2 (and its rationale that deleting leaves the number):
    after every create and delete in a random run, the manifest records one
    more than the highest object number ever created."""
    folder = new_document(tmp_path)
    highest_ever: int | None = None
    for step, op, obj_id, _ in random_sequence(folder, seed):
        if op == "create":
            number = number_of(obj_id)
            highest_ever = number if highest_ever is None else max(highest_ever, number)
        expected = str(highest_ever + 1)
        assert read_next(folder) == expected, (
            f"seed {seed} step {step} ({op} {obj_id}): next-object-number is "
            f"{read_next(folder)!r}, expected {expected!r}"
        )


# Beyond the blind tests: the editor's own guarantees for the number.


def test_allocate_next_id_takes_the_recorded_number(doc_folder: Path) -> None:
    """`allocate_next_id` gives the id `create_object` will: the fixture has
    DOC-0 and DOC-1, so a recorded 7 wins and a recorded 1 doesn't."""
    write_next(doc_folder, "7")
    assert allocate_next_id(load_document(doc_folder)) == "DOC-7"
    write_next(doc_folder, "1")
    assert allocate_next_id(load_document(doc_folder)) == "DOC-2"


def test_create_object_records_ref_and_number_in_one_manifest_write(
    doc_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outline ref and the number land together, so no crash can leave
    a new object listed without its number recorded."""
    written: list[str] = []
    real = editor._write_tree

    def spy(tree, path):
        written.append(path.name)
        real(tree, path)

    monkeypatch.setattr(editor, "_write_tree", spy)
    new_id = create_object(doc_folder, description="new")
    assert written == [f"{new_id}.xml", "DOC.xml"]
    assert read_next(doc_folder) == "3"


def test_create_object_that_fails_leaves_the_number(doc_folder: Path) -> None:
    write_next(doc_folder, "5")
    before = (doc_folder / "DOC.xml").read_bytes()
    with pytest.raises(EditorError, match="not found"):
        create_object(doc_folder, parent_id="DOC-999")
    assert (doc_folder / "DOC.xml").read_bytes() == before
    assert not (doc_folder / "objects" / "DOC-5.xml").exists()


# MELLON-STORE-SRS blind tests


def test_store_srs_61_author_written_inside_metadata(tmp_path):
    """MELLON-STORE-SRS-61: an author set through Mellon is written as an author
    element inside the metadata element, and nowhere else."""
    folder = make_doc(tmp_path, metadata="<title>T</title>")
    editor.set_metadata(folder, "author", "New Author")
    root = etree.parse(str(folder / "DOC.xml")).getroot()
    authors = root.findall(".//author")
    assert [a.getparent().tag for a in authors] == ["metadata"], etree.tostring(root)
    assert authors[0].text == "New Author"
    assert root.find("metadata").getparent() is root


def test_store_srs_75_label_written_as_label_attribute(tmp_path):
    """MELLON-STORE-SRS-75: a label defined through Mellon is written as the label
    XML attribute of the attribute element."""
    folder = make_doc(tmp_path)
    editor.add_attribute(folder, "risk", "text", False, label="Risk Level")
    root = etree.parse(str(folder / "DOC.xml")).getroot()
    (el,) = [a for a in root.iter("attribute") if a.get("name") == "risk"]
    assert el.get("label") == "Risk Level", etree.tostring(el)


def test_store_srs_93_setting_wrong_case_option_is_refused(tmp_path):
    """MELLON-STORE-SRS-93: setting status to 'draft' through Mellon, with option
    Draft, is refused and the object file is unchanged."""
    folder = make_doc(tmp_path, attributes=_ENUM_ATTRS)
    f = folder / "objects" / "DOC-1.xml"
    before = f.read_bytes()
    with pytest.raises(EditorError):
        editor.set_attribute(folder, "DOC-1", "status", "draft")
    assert f.read_bytes() == before


_BOOL_ATTRS = DEFAULT_ATTRS + '    <attribute name="flag" kind="boolean"/>\n'


def _attr_text(path: Path, name: str) -> str | None:
    els = [a for a in etree.parse(str(path)).getroot().iter("attr") if a.get("name") == name]
    assert len(els) <= 1
    return els[0].text if els else None


@pytest.mark.parametrize("value", ["true", "false"])
def test_store_srs_97_boolean_written_lower_case(tmp_path, value):
    """MELLON-STORE-SRS-97: an object file holds a boolean value as true or false
    in lower case. Set through Mellon, the attr element's text is exactly the
    value."""
    folder = make_doc(tmp_path, attributes=_BOOL_ATTRS)
    editor.set_attribute(folder, "DOC-1", "flag", value)
    f = folder / "objects" / "DOC-1.xml"
    assert _attr_text(f, "flag") == value
    assert f'<attr name="flag">{value}</attr>'.encode() in f.read_bytes()


@pytest.mark.parametrize("value", ["True", "FALSE"])
def test_store_srs_97_boolean_not_written_in_other_case(tmp_path, value):
    """MELLON-STORE-SRS-97: a boolean value in the file is in lower case, so
    setting 'True' or 'FALSE' through Mellon either is refused or is written in
    lower case; it is never written as given."""
    folder = make_doc(tmp_path, attributes=_BOOL_ATTRS)
    f = folder / "objects" / "DOC-1.xml"
    try:
        editor.set_attribute(folder, "DOC-1", "flag", value)
    except EditorError:
        pass
    assert _attr_text(f, "flag") in (None, "true", "false"), f.read_text()


def _table_in_file(folder: Path, obj_id: str = "DOC-1"):
    return etree.parse(str(folder / "objects" / f"{obj_id}.xml")).getroot().find("table")


def test_store_srs_101_center_alignment_written(tmp_path):
    """MELLON-STORE-SRS-101: a table with a centred column, set through Mellon,
    is written with align="center" on that col element."""
    folder = make_doc(tmp_path)
    editor.set_table(folder, "DOC-1", Table(
        columns=(TableColumn(), TableColumn(align="center")),
        rows=(TableRow(cells=(TableCell(text="a"), TableCell(text="b"))),),
    ))
    cols = _table_in_file(folder).find("columns").findall("col")
    assert cols[1].get("align") == "center", etree.tostring(cols[1])


_ROWSPAN_TABLE = Table(
    columns=(TableColumn(), TableColumn()),
    rows=(
        TableRow(cells=(TableCell(text="spans", rowspan=2), TableCell(text="top"))),
        TableRow(cells=(TableCell(text="bottom"),)),
    ),
)


def test_store_srs_107_row_span_written(tmp_path):
    """MELLON-STORE-SRS-107: the same table set through Mellon is written with
    rowspan="2" on its first cell."""
    folder = make_doc(tmp_path)
    editor.set_table(folder, "DOC-1", _ROWSPAN_TABLE)
    first = _table_in_file(folder).find("row").find("cell")
    assert first.get("rowspan") == "2", etree.tostring(first)


def test_store_srs_108_span_attributes_only_above_one(tmp_path):
    """MELLON-STORE-SRS-108: Mellon writes colspan or rowspan only when its value
    is more than 1. The cell spanning 2 rows and 1 column has rowspan="2" and no
    colspan; the unmerged cells have neither."""
    folder = make_doc(tmp_path)
    editor.set_table(folder, "DOC-1", _ROWSPAN_TABLE)
    cells = list(_table_in_file(folder).iter("cell"))
    assert [c.text for c in cells] == ["spans", "top", "bottom"]
    assert (cells[0].get("rowspan"), cells[0].get("colspan")) == ("2", None)
    for c in cells[1:]:
        assert (c.get("rowspan"), c.get("colspan")) == (None, None), etree.tostring(c)


def test_store_srs_115_cells_stored_only_in_the_object_file(tmp_path, git_env):
    """MELLON-STORE-SRS-115: Mellon shall store a table's cells only in the object
    file of the table's object. Setting a table on DOC-1 changes only
    objects/DOC-1.xml (working copy and .git), and no other file holds the
    cell texts."""
    root = git_init(git_env, tmp_path / "r")
    folder = make_doc(root, n=3)
    git_commit_all(git_env, root, "seed")
    before = snapshot(root)
    editor.set_table(folder, "DOC-1", Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow(cells=(TableCell(text="CELLTEXT-ALPHA"), TableCell(text="CELLTEXT-BETA"))),),
    ))
    assert changed(before, snapshot(root)) == ["DOC/objects/DOC-1.xml"]
    holders = [p.relative_to(root).as_posix() for p in root.rglob("*")
               if p.is_file() and b"CELLTEXT-" in p.read_bytes()]
    assert holders == ["DOC/objects/DOC-1.xml"], holders


def test_store_srs_131_svg_without_namespace_is_refused(tmp_path):
    """MELLON-STORE-SRS-131: a figure file's root element is svg in the namespace
    http://www.w3.org/2000/svg. Attaching <svg><rect/></svg>, in no namespace,
    is refused and no figure file is written."""
    folder = make_doc(tmp_path / "r")
    src = write(tmp_path / "plain.svg", DECL + "<svg><rect/></svg>\n")
    with pytest.raises(EditorError):
        editor.set_figure(folder, "DOC-1", src)
    assert not (folder / "figures" / "DOC-1.svg").exists()


def test_store_srs_131_svg_in_svg_namespace_is_accepted(tmp_path):
    """MELLON-STORE-SRS-131 (other side of the boundary): an svg root in the SVG
    namespace is attached."""
    folder = make_doc(tmp_path / "r")
    src = _svg_file(tmp_path)
    editor.set_figure(folder, "DOC-1", src)
    assert (folder / "figures" / "DOC-1.svg").read_bytes() == src.read_bytes()


@pytest.mark.parametrize(
    "name, slug",
    [
        pytest.param("ABC", "abc", id="ascii"),
        pytest.param("  --Hello__World--  ", "hello-world", id="runs-and-trim"),
        pytest.param("ÄBC", "bc", id="a-umlaut"),
        pytest.param("xK", "x", id="kelvin-sign"),
        pytest.param("xİ", "x", id="dotted-capital-i"),
    ],
)
@pytest.mark.parametrize("what", ["view", "filter"])
def test_store_srs_153_slug_from_ascii_letters_only(tmp_path, what, name, slug):
    """MELLON-STORE-SRS-153: the slug comes from the name by turning A-Z into a-z,
    each run of other characters than a-z and 0-9 into one hyphen, and trimming
    hyphens. Only ASCII letters change case, so the Kelvin sign and the dotted
    capital I give no k or i."""
    folder = make_doc(tmp_path)
    if what == "view":
        got = editor.create_view(folder, name, ["status"])
    else:
        got = editor.create_filter(folder, name)
    assert got == slug
    assert (folder / f"{what}s" / f"{slug}.xml").is_file()


_SLUG_OPS = {
    "delete_view": lambda f, s: editor.delete_view(f, s),
    "rename_view": lambda f, s: editor.rename_view(f, s, "Renamed"),
    "set_view_visible": lambda f, s: editor.set_view_visible(f, s, ["rationale"]),
    "delete_filter": lambda f, s: editor.delete_filter(f, s),
    "rename_filter": lambda f, s: editor.rename_filter(f, s, "Renamed"),
    "set_filter_rules": lambda f, s: editor.set_filter_rules(f, s, "any", False, []),
}


@pytest.mark.parametrize("slug", _BAD_SLUGS, ids=repr)
@pytest.mark.parametrize("op", list(_SLUG_OPS))
def test_store_srs_155_bad_slug_refused(slug_doc, op, slug):
    """MELLON-STORE-SRS-155: if a slug given to Mellon doesn't match
    [a-z0-9]+(-[a-z0-9]+)*, Mellon shall refuse the request. Each bad slug is
    refused for delete, rename and edit of views and filters (with a view and a
    filter 'main' present), and no file changes."""
    root = slug_doc.parent
    before = snapshot(root)
    with pytest.raises(EditorError):
        _SLUG_OPS[op](slug_doc, slug)
    assert changed(before, snapshot(root)) == []


def test_store_srs_167_write_failing_partway_keeps_old_or_new_content(tmp_path, monkeypatch):
    """MELLON-STORE-SRS-167: if a write of a file fails, Mellon shall leave the
    file holding its content before the write or the full content written. A
    description write that fails halfway, as on a full disk, leaves DOC-1.xml
    whole."""
    folder = make_doc(tmp_path / "r")
    target = folder / "objects" / "DOC-1.xml"
    old = target.read_bytes()
    new_text = "A much longer description so that half of the file is a cut file " * 4
    inj = PartialWrites(monkeypatch, folder)
    with pytest.raises((EditorError, OSError)):
        editor.set_description(folder, "DOC-1", new_text)
    monkeypatch.undo()
    assert inj.fired, "test error: no write was intercepted"
    now = target.read_bytes()
    if now != old:
        assert load_document(folder).objects["DOC-1"].description == new_text, now


_KILL_BEFORE_REPLACE = r"""
import os, sys
from pathlib import Path
from mellon import editor
folder, obj_id, text = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
target = (folder / "objects" / (obj_id + ".xml")).resolve()
real = {"replace": os.replace, "rename": os.rename}
def guard(name):
    def call(src, dst, *a, **k):
        if Path(dst).resolve() == target:
            os._exit(17)
        return real[name](src, dst, *a, **k)
    return call
os.replace = guard("replace")
os.rename = guard("rename")
editor.set_description(folder, obj_id, text)
sys.exit(0)
"""


def test_store_srs_167_stop_before_replace_keeps_old_content(tmp_path):
    """MELLON-STORE-SRS-167: if Mellon stops during the write, the file holds its
    content before the write or the full content. A process killed after
    writing the new content and before it replaces DOC-1.xml leaves the old
    content in full."""
    folder = make_doc(tmp_path / "r")
    target = folder / "objects" / "DOC-1.xml"
    old = target.read_bytes()
    r = subprocess.run([sys.executable, "-c", _KILL_BEFORE_REPLACE, str(folder), "DOC-1", "New text"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 17, f"test error: the process wasn't stopped at the replace: {r.stderr}"
    assert target.read_bytes() == old


def test_store_srs_171_temporary_file_name_starts_with_dot(tmp_path, monkeypatch):
    """MELLON-STORE-SRS-171: Mellon shall begin the name of each temporary file it
    writes in a document folder with a dot. A write made to fail at its final
    replace shows the temporary file's name, which starts with a dot."""
    folder = (make_doc(tmp_path / "r")).resolve()
    seen: list[Path] = []

    def failing(src, dst, *a, **k):
        seen.append(Path(src).resolve())
        raise OSError(errno.EIO, "replace failed (injected)")

    monkeypatch.setattr(os, "replace", failing)
    monkeypatch.setattr(os, "rename", failing)
    with pytest.raises((EditorError, OSError)):
        editor.set_description(folder, "DOC-1", "New text")
    monkeypatch.undo()
    in_folder = [p for p in seen if folder in p.parents]
    assert in_folder, f"test error: no temporary file in the document folder: {seen}"
    assert all(p.name.startswith(".") for p in in_folder), [p.name for p in in_folder]


@pytest.fixture
def written_repo(tmp_path):
    """Everything written through Mellon: SYS, and DOC with a parent, a three-level
    outline, two attributes with options, a table, a link, a view and a filter."""
    root = tmp_path / "w"
    root.mkdir()
    svc = DocumentService(root)
    svc.create_document("SYS", title="System")
    sys_obj = svc.create_object("SYS", description="System requirement").value
    svc.create_document("DOC", title="Written", parents=["SYS"])
    folder = root / "DOC"
    editor.add_attribute(folder, "status", "enum", False, options=("Draft", "Approved"))
    editor.add_attribute(folder, "priority", "enum", False, options=("High", "Low"), label="Priority")
    top = editor.create_object(folder, None, "Top heading")
    mid = editor.create_object(folder, top, "Middle")
    low = editor.create_object(folder, mid, "Low")
    editor.set_attribute(folder, low, "status", "Draft")
    editor.set_table(folder, low, Table(
        columns=(TableColumn(), TableColumn(align="right")),
        rows=(TableRow(cells=(TableCell(text="h1"), TableCell(text="h2")), header=True),
              TableRow(cells=(TableCell(text="v1"), TableCell(text="v2")))),
    ))
    editor.add_link(folder, low, "SYS", sys_obj, "satisfies")
    editor.create_view(folder, "Dev view", ["status", "priority"])
    editor.create_filter(folder, "Drafts", rules=[FilterRule(kind="attr", attr="status", op="eq", value="Draft")])
    return root


def _data_files(root: Path) -> list[Path]:
    out = []
    for doc in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        out += sorted(p for p in doc.rglob("*.xml") if not p.name.startswith("."))
    return out


def test_store_srs_189_data_files_begin_with_declaration(written_repo):
    """MELLON-STORE-SRS-189: Mellon shall begin each data file it writes with
    <?xml version="1.0" encoding="UTF-8"?> as the whole first line."""
    files = _data_files(written_repo)
    kinds = {p.parent.name if p.parent.name in ("objects", "links", "views", "filters") else p.name for p in files}
    assert {"objects", "links", "views", "filters", "DOC.xml"} <= kinds, "precondition: each kind written"
    for p in files:
        assert p.read_bytes().split(b"\n", 1)[0] == DECL_LINE, p


def test_store_srs_190_two_space_indentation_per_level(written_repo):
    """MELLON-STORE-SRS-190: Mellon shall indent each element of a data file it
    writes by 2 spaces for each level of nesting: each element starts on a line
    of its own, after exactly 2 x depth spaces, the root at 0."""
    for p in _data_files(written_repo):
        data = p.read_bytes()
        lines = data.decode("utf-8").split("\n")
        root_el = etree.fromstring(data)
        starts: dict[int, int] = {}

        def walk(el, depth):
            if not isinstance(el.tag, str):
                return
            line = lines[el.sourceline - 1]
            starts[el.sourceline] = starts.get(el.sourceline, 0) + 1
            assert line.startswith(" " * (2 * depth) + "<" + el.tag), (p.name, el.tag, depth, line)
            for c in el:
                walk(c, depth + 1)

        walk(root_el, 0)
        shared = [n for n, k in starts.items() if k > 1]
        assert not shared, f"{p.name}: more than one element starts on line(s) {shared}"


def test_store_srs_192_data_files_end_with_one_lf(written_repo):
    """MELLON-STORE-SRS-192: Mellon shall end each data file it writes with
    exactly one LF after the root element's end tag."""
    for p in _data_files(written_repo):
        data = p.read_bytes()
        tag = etree.fromstring(data).tag
        assert data.endswith(f"</{tag}>\n".encode()), (p, data[-40:])
        assert not data.endswith(b"\n\n"), (p, data[-40:])


def test_store_srs_187_utf8_without_bom(tmp_path):
    """MELLON-STORE-SRS-187: Mellon shall write each data file in UTF-8 with no
    byte order mark. A title, description and attribute value holding the
    letters A-ring, euro sign and a CJK character are stored as their UTF-8
    bytes, and each file starts with '<?xml'."""
    text = "Å € 漢"
    folder = make_doc(tmp_path / "r")
    editor.set_metadata(folder, "title", text)
    editor.set_description(folder, "DOC-1", text)
    editor.set_attribute(folder, "DOC-1", "rationale", text)
    for p in (folder / "DOC.xml", folder / "objects" / "DOC-1.xml"):
        data = p.read_bytes()
        assert data.startswith(b"<?xml"), (p, data[:8])
        assert text.encode("utf-8") in data, p
    assert (folder / "objects" / "DOC-1.xml").read_bytes().count(text.encode("utf-8")) == 2


@pytest.fixture
def stable_doc(tmp_path):
    """DOC written through Mellon, so each data file is in the written form."""
    root = tmp_path / "s"
    root.mkdir()
    svc = DocumentService(root)
    svc.create_document("DOC", title="Stable")
    folder = root / "DOC"
    one = editor.create_object(folder, None, "First object")
    two = editor.create_object(folder, None, "Second object")
    three = editor.create_object(folder, None, "Third object")
    editor.set_attribute(folder, one, "rationale", "Because")
    editor.add_link(folder, one, "DOC", two, "refines")
    editor.create_view(folder, "Dev", ["rationale", "type"])
    editor.create_filter(folder, "Notes", combinator="any", keep_ancestors=False,
                         rules=[FilterRule(kind="attr", attr="type", op="eq", value="Note"),
                                FilterRule(kind="has-outgoing", attr="", op="present")])
    return folder, one, two, three


def _noop(kind: str, folder: Path, one: str, two: str, three: str):
    doc = load_document(folder)
    if kind == "manifest":
        editor.set_metadata(folder, "title", doc.metadata.title)
    elif kind == "object-attribute":
        editor.set_attribute(folder, one, "rationale", doc.objects[one].attrs["rationale"])
    elif kind == "object-description":
        editor.set_description(folder, one, doc.objects[one].description)
    elif kind == "view":
        (view,) = doc.views
        editor.set_view_visible(folder, view.slug, list(view.visible))
    elif kind == "filter":
        (f,) = doc.filters
        editor.set_filter_rules(folder, f.slug, f.combinator, f.keep_ancestors, list(f.rules))
    elif kind == "links":
        editor.add_link(folder, one, "DOC", three, "refines")
        editor.remove_link(folder, one, "DOC", three)
    else:  # pragma: no cover
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["manifest", "object-attribute", "object-description",
                                  "view", "filter", "links"])
def test_store_srs_208_unchanged_canonical_form_keeps_bytes(stable_doc, kind):
    """MELLON-STORE-SRS-208: if a data file is in the written form and an
    operation leaves its canonical form unchanged, Mellon shall leave its bytes
    unchanged: setting a value to its current value, or adding and removing the
    same link, changes no file."""
    folder, one, two, three = stable_doc
    before = snapshot(folder.parent)
    _noop(kind, folder, one, two, three)
    assert changed(before, snapshot(folder.parent)) == []


@pytest.mark.parametrize("where", ["attribute", "cell", "title"])
def test_store_srs_212_cr_and_crlf_become_lf(tmp_path, where):
    """MELLON-STORE-SRS-212: when Mellon writes stored text, it shall replace each
    CR LF pair and each other CR with one LF: 'a\\r\\nb\\rc' reads back as
    'a\\nb\\nc'."""
    folder = make_doc(tmp_path / "r")
    _write_text(folder, where, "a\r\nb\rc")
    assert _read_text(folder, where) == "a\nb\nc"


@pytest.mark.parametrize("where", ["description", "attribute", "cell"])
def test_store_srs_215_tab_is_kept(tmp_path, where):
    """MELLON-STORE-SRS-215: Mellon shall keep each tab in stored text: 'a\\tb'
    reads back with its tab."""
    folder = make_doc(tmp_path / "r")
    _write_text(folder, where, "a\tb")
    assert _read_text(folder, where) == "a\tb"
