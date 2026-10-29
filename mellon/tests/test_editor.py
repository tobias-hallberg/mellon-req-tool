"""Tests for editor commands: new, edit-attr, link, unlink."""

from __future__ import annotations

import os
from pathlib import Path

import pygit2
import pytest

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
from tests.test_repo import _write_merge_conflict
from tests.test_suspects import (
    DOC_MANIFEST,
    DOC_OBJ,
    PARENT_MANIFEST,
    PARENT_OBJ_V1,
    _commit_all,
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
    # DOC-1 was the highest id, so max+1 allocation hands it out again.
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
    assert _snapshot(doc_folder) == before
    j.reapply()
    assert _snapshot(doc_folder) == after


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
