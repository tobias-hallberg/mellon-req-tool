from __future__ import annotations

import re
from pathlib import Path

import pytest

from mellon import (
    Document,
    Metadata,
    OutlineNode,
    RequirementObject,
    Table,
    TableCell,
    TableColumn,
    TableRow,
    add_attribute,
    load_document,
    set_attribute,
    validate,
)


def _codes(errors: list) -> set[str]:
    return {e.code for e in errors}


def test_valid_document_has_no_errors(doc_folder: Path) -> None:
    doc = load_document(doc_folder)
    assert validate(doc) == []


def test_outline_missing_object(doc_folder: Path) -> None:
    (doc_folder / "objects" / "DOC-1.xml").unlink()
    doc = load_document(doc_folder)
    assert "OUTLINE_MISSING_OBJECT" in _codes(validate(doc))


def test_object_not_in_outline(doc_folder: Path) -> None:
    (doc_folder / "objects" / "DOC-2.xml").write_text(
        """<?xml version="1.0"?>
<object id="DOC-2">
  <attr name="type">Functional</attr>
  <attr name="status">Draft</attr>
  <description>orphan</description>
</object>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "OBJECT_NOT_IN_OUTLINE" in _codes(validate(doc))


def test_bad_enum_value(doc_folder: Path) -> None:
    path = doc_folder / "objects" / "DOC-1.xml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '<attr name="type">Requirement</attr>',
            '<attr name="type">Nonsense</attr>',
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "ATTR_BAD_ENUM" in _codes(validate(doc))


def test_unknown_attribute(doc_folder: Path) -> None:
    path = doc_folder / "objects" / "DOC-1.xml"
    content = path.read_text(encoding="utf-8")
    content = content.replace(
        "<description>",
        '<attr name="unknown">x</attr>\n  <description>',
    )
    path.write_text(content, encoding="utf-8")
    doc = load_document(doc_folder)
    assert "ATTR_UNKNOWN" in _codes(validate(doc))


def test_missing_required_attribute(doc_folder: Path) -> None:
    path = doc_folder / "objects" / "DOC-1.xml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '<attr name="status">Draft</attr>\n', ""
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "ATTR_MISSING_REQUIRED" in _codes(validate(doc))


def test_required_attribute_cleared_to_empty_is_missing(doc_folder: Path) -> None:
    # Clearing an enum in the GUI writes an empty <attr>; that must not pass.
    set_attribute(doc_folder, "DOC-1", "status", "")
    assert "ATTR_MISSING_REQUIRED" in _codes(validate(load_document(doc_folder)))


def test_duplicate_ref_in_outline(doc_folder: Path) -> None:
    manifest = doc_folder / "DOC.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '<ref id="DOC-1"/>',
            '<ref id="DOC-1"/><ref id="DOC-1"/>',
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "OUTLINE_DUP" in _codes(validate(doc))


def test_intra_doc_link_target_missing(doc_folder: Path) -> None:
    (doc_folder / "links" / "DOC-1.xml").write_text(
        """<?xml version="1.0"?>
<links source="DOC-1">
  <link target-doc="DOC" target-id="DOC-999" type="refines"/>
</links>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "LINK_TARGET_MISSING" in _codes(validate(doc))


def test_link_to_non_parent_doc_rejected(doc_folder: Path) -> None:
    # DOC's only declared parent is PARENT; STRANGER is not in <parents>.
    (doc_folder / "links" / "DOC-1.xml").write_text(
        """<?xml version="1.0"?>
<links source="DOC-1">
  <link target-doc="STRANGER" target-id="STRANGER-1" type="satisfies"/>
</links>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "LINK_NOT_ALLOWED" in _codes(validate(doc))


def test_self_link_allowed_without_being_in_parents(doc_folder: Path) -> None:
    # A link from DOC-1 to another object in the same doc must not trigger
    # LINK_NOT_ALLOWED — self-links are implicitly permitted.
    (doc_folder / "links" / "DOC-1.xml").write_text(
        """<?xml version="1.0"?>
<links source="DOC-1">
  <link target-doc="DOC" target-id="DOC-0" type="refines"/>
</links>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "LINK_NOT_ALLOWED" not in _codes(validate(doc))


def test_top_level_doc_cannot_have_outgoing_cross_doc_links(
    doc_folder: Path,
) -> None:
    # Strip <parents> from the manifest so DOC becomes a top-level doc, then
    # add an outgoing cross-doc link — that must now be rejected.
    manifest = doc_folder / "DOC.xml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '<parents><parent doc="PARENT"/></parents>\n  ', ""
        ),
        encoding="utf-8",
    )
    # The fixture's existing DOC-1 link already targets PARENT, which is now
    # disallowed.
    doc = load_document(doc_folder)
    assert "LINK_NOT_ALLOWED" in _codes(validate(doc))


# --- Rules engine ----------------------------------------------------------


_RULES_HEADING_FORBIDS_RATIONALE = """\
<?xml version="1.0" encoding="UTF-8"?>
<rules>
  <rule>
    <when type="Heading"/>
    <then>
      <attr name="rationale" status="forbidden"/>
    </then>
  </rule>
  <rule>
    <when type="Requirement" has-outgoing-link="false"/>
    <then>
      <attr name="rationale" status="required"/>
    </then>
  </rule>
  <rule>
    <when type="Requirement" has-outgoing-link="true"/>
    <then>
      <attr name="rationale" status="optional"/>
    </then>
  </rule>
</rules>
"""


def _write_rules(doc_folder: Path, body: str = _RULES_HEADING_FORBIDS_RATIONALE) -> None:
    (doc_folder / "DOC.rules.xml").write_text(body, encoding="utf-8")


def test_rule_forbidden_present_flagged(doc_folder: Path) -> None:
    # The fixture's DOC-0 is a Heading with no rationale — fine. Add one
    # explicitly: the forbidden rule should flag it.
    _write_rules(doc_folder)
    obj0 = doc_folder / "objects" / "DOC-0.xml"
    obj0.write_text(
        obj0.read_text(encoding="utf-8").replace(
            "<description>",
            '<attr name="rationale">should not be here</attr>\n  <description>',
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "RULE_FORBIDDEN_PRESENT" in _codes(validate(doc))


def test_rule_required_missing_when_no_outgoing_link(doc_folder: Path) -> None:
    # The fixture's DOC-1 (Requirement) has rationale + an outgoing link.
    # Drop both: rules say Requirement-without-link must have rationale.
    _write_rules(doc_folder)
    (doc_folder / "links" / "DOC-1.xml").unlink()
    obj1 = doc_folder / "objects" / "DOC-1.xml"
    obj1.write_text(
        obj1.read_text(encoding="utf-8").replace(
            '<attr name="rationale">because</attr>\n  ', ""
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "RULE_REQUIRED_MISSING" in _codes(validate(doc))


def test_rule_required_satisfied_when_outgoing_link_present(doc_folder: Path) -> None:
    # DOC-1 (Functional) keeps its outgoing link and removes its rationale.
    # Rules say rationale is optional in this case → no rule error.
    _write_rules(doc_folder)
    obj1 = doc_folder / "objects" / "DOC-1.xml"
    obj1.write_text(
        obj1.read_text(encoding="utf-8").replace(
            '<attr name="rationale">because</attr>\n  ', ""
        ),
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    codes = _codes(validate(doc))
    assert "RULE_REQUIRED_MISSING" not in codes
    assert "RULE_FORBIDDEN_PRESENT" not in codes


def test_no_rules_file_means_no_rule_errors(doc_folder: Path) -> None:
    # Sanity: without any rules.xml the validator emits no RULE_* codes.
    doc = load_document(doc_folder)
    codes = _codes(validate(doc))
    assert "RULE_REQUIRED_MISSING" not in codes
    assert "RULE_FORBIDDEN_PRESENT" not in codes


# ---- non-string attribute kinds ----


@pytest.mark.parametrize(
    "kind,good,bad,bad_code",
    [
        ("integer", "42", "abc", "ATTR_BAD_INTEGER"),
        ("integer", "-7", "3.14", "ATTR_BAD_INTEGER"),
        ("real", "3.14", "abc", "ATTR_BAD_REAL"),
        ("real", "-2", "1.2.3", "ATTR_BAD_REAL"),
        ("boolean", "true", "yes", "ATTR_BAD_BOOLEAN"),
        ("boolean", "false", "True", "ATTR_BAD_BOOLEAN"),
        ("date", "2026-04-25", "25/04/2026", "ATTR_BAD_DATE"),
        ("date", "2026-12-31", "not-a-date", "ATTR_BAD_DATE"),
    ],
)
def test_non_string_kind_value_checks(
    doc_folder: Path, kind: str, good: str, bad: str, bad_code: str
) -> None:
    add_attribute(doc_folder, "extra", kind, False)
    set_attribute(doc_folder, "DOC-1", "extra", good)
    assert bad_code not in _codes(validate(load_document(doc_folder)))

    set_attribute(doc_folder, "DOC-1", "extra", bad)
    assert bad_code in _codes(validate(load_document(doc_folder)))


def test_non_string_kind_empty_value_only_triggers_missing_required(
    doc_folder: Path,
) -> None:
    # Empty value with required=true → ATTR_MISSING_REQUIRED only, no
    # ATTR_BAD_INTEGER.
    add_attribute(doc_folder, "weight", "integer", True)
    # Existing objects don't carry "weight"; that's the missing-required gap.
    codes = _codes(validate(load_document(doc_folder)))
    assert "ATTR_MISSING_REQUIRED" in codes
    assert "ATTR_BAD_INTEGER" not in codes


# ---- semantic / cross-field rules ----


def test_rule_attr_equality_condition_fires(doc_folder: Path) -> None:
    # DOC-1 has status=Draft, no rationale on DOC-0 — flip DOC-1 status to
    # Approved and require rationale only when status=Approved.
    path = doc_folder / "objects" / "DOC-1.xml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '<attr name="status">Draft</attr>',
            '<attr name="status">Approved</attr>',
        ),
        encoding="utf-8",
    )
    # Drop the existing rationale on DOC-1 so the rule should flag it.
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '<attr name="rationale">because</attr>\n  ', ""
        ),
        encoding="utf-8",
    )
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when status="Approved"/>
    <then><attr name="rationale" status="required"/></then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    codes = _codes(validate(doc))
    assert "RULE_REQUIRED_MISSING" in codes


def test_rule_attr_equality_condition_does_not_fire_when_unmatched(
    doc_folder: Path,
) -> None:
    # DOC-0/DOC-1 have status=Draft → rule for status=Approved shouldn't fire.
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when status="Approved"/>
    <then><attr name="rationale" status="required"/></then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    assert "RULE_REQUIRED_MISSING" not in _codes(validate(doc))


def test_rule_link_required_missing_flagged(doc_folder: Path) -> None:
    # DOC-1 links to PARENT; DOC-0 doesn't. Rule "every Functional must link
    # to PARENT" should fire on no Functional object that has no PARENT link.
    # Make DOC-0 Functional so it triggers the missing-link.
    (doc_folder / "objects" / "DOC-0.xml").write_text(
        """<?xml version="1.0"?>
<object id="DOC-0">
  <attr name="type">Functional</attr>
  <attr name="status">Draft</attr>
  <description>orphan</description>
</object>
""",
        encoding="utf-8",
    )
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when type="Functional"/>
    <then><link target-doc="PARENT" status="required"/></then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    codes = _codes(validate(doc))
    assert "RULE_LINK_REQUIRED_MISSING" in codes


def test_rule_link_required_satisfied_when_link_present(doc_folder: Path) -> None:
    # DOC-1 already links to PARENT — same rule should NOT flag it.
    (doc_folder / "DOC.rules.xml").write_text(
        """<?xml version="1.0"?>
<rules>
  <rule>
    <when type="Functional"/>
    <then><link target-doc="PARENT" status="required"/></then>
  </rule>
</rules>
""",
        encoding="utf-8",
    )
    doc = load_document(doc_folder)
    # DOC-1 (Functional) has a PARENT link in the fixture; DOC-0 is Heading
    # so the rule doesn't apply to it.
    assert "RULE_LINK_REQUIRED_MISSING" not in _codes(validate(doc))


# ---- document metadata ----


def _set_manifest_date(doc_folder: Path, value: str) -> None:
    manifest = doc_folder / "DOC.xml"
    text = manifest.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"<date>[^<]*</date>", f"<date>{value}</date>", text, count=1
    )
    assert n == 1, "manifest fixture should have exactly one <date> element"
    manifest.write_text(new_text, encoding="utf-8")


def test_validate_flags_bad_metadata_date(doc_folder: Path) -> None:
    _set_manifest_date(doc_folder, "2026-04-215")
    assert "META_BAD_DATE" in _codes(validate(load_document(doc_folder)))


def test_validate_passes_iso_metadata_date(doc_folder: Path) -> None:
    _set_manifest_date(doc_folder, "2026-04-25")
    assert "META_BAD_DATE" not in _codes(validate(load_document(doc_folder)))


def test_validate_passes_empty_metadata_date(doc_folder: Path) -> None:
    _set_manifest_date(doc_folder, "")
    assert "META_BAD_DATE" not in _codes(validate(load_document(doc_folder)))


# --- tables ---------------------------------------------------------------


def _doc_with_table(table: Table) -> Document:
    """A minimal in-memory document carrying `table` on its one object.

    Tables are validated by `_check_tables`, but the loader rejects malformed
    tables outright — so the only way to exercise the validator is to build a
    Document directly, bypassing the loader.
    """
    obj = RequirementObject(id="DOC-0", attrs={}, table=table)
    return Document(
        id="DOC",
        metadata=Metadata(),
        parents=(),
        attributes=(),
        outline=(OutlineNode("DOC-0"),),
        objects={"DOC-0": obj},
        links={},
    )


def test_validate_clean_table_ok() -> None:
    table = Table(
        columns=(TableColumn("left"), TableColumn("right")),
        rows=(TableRow((TableCell("a"), TableCell("b"))),),
    )
    assert validate(_doc_with_table(table)) == []


def test_validate_flags_table_geometry() -> None:
    bad = Table(
        columns=(TableColumn(), TableColumn()),
        rows=(TableRow((TableCell("only-one"),)),),
    )
    assert "TABLE_GEOMETRY" in _codes(validate(_doc_with_table(bad)))


def test_validate_flags_bad_align() -> None:
    bad = Table(columns=(TableColumn("sideways"),), rows=(TableRow((TableCell("x"),)),))
    assert "TABLE_BAD_ALIGN" in _codes(validate(_doc_with_table(bad)))


def test_validate_flags_bad_span() -> None:
    bad = Table(
        columns=(TableColumn(),), rows=(TableRow((TableCell("x", colspan=0),)),)
    )
    assert "TABLE_BAD_SPAN" in _codes(validate(_doc_with_table(bad)))


def test_validate_flags_no_columns() -> None:
    bad = Table(columns=(), rows=(TableRow((TableCell("x"),)),))
    assert "TABLE_NO_COLUMNS" in _codes(validate(_doc_with_table(bad)))
