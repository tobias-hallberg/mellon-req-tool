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
    editor,
    load_document,
    set_attribute,
    validate,
)
from tests.test_editor import number_of, new_document, populate, write_next


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

    # Written into the object file by hand: set_attribute writes a boolean's
    # true or false in lower case (MELLON-STORE-SRS-97), and validation must
    # still catch "True" in a file another program wrote.
    path = doc_folder / "objects" / "DOC-1.xml"
    text = path.read_text(encoding="utf-8")
    assert f'<attr name="extra">{good}</attr>' in text
    path.write_text(
        text.replace(f'<attr name="extra">{good}</attr>', f'<attr name="extra">{bad}</attr>'),
        encoding="utf-8", newline="\n",
    )
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


# --- the next object number (MELLON-STORE-SRS-2) ----------------------------
#
# Written blind from the requirement; see test_editor's object ID tests.


@pytest.fixture
def valid_folder(tmp_path) -> Path:
    """A document with objects 0..2 and the number 3, which validates clean."""
    folder = new_document(tmp_path)
    ids = populate(folder, 3)
    for obj_id in ids:  # the default schema makes `type` required
        editor.set_attribute(folder, obj_id, "type", "Information")
    write_next(folder, str(max(number_of(i) for i in ids) + 1))
    errors = validate(load_document(folder))
    assert errors == [], f"setup: the document should validate clean before the number is changed, got {errors}"
    return folder


@pytest.mark.parametrize(
    "value",
    [
        "-1",  # just below the limit 0
        "-7",  # negative
        "abc",  # not a number
        "1.5",  # not whole
        "",  # empty
        "NaN",  # not a number
        " ",  # whitespace only
    ],
)
def test_store_srs_2_invalid_number_is_a_validation_error(valid_folder, value):
    """MELLON-STORE-SRS-2: a next object number that isn't a whole number of 0
    or more makes the document (which still loads) report a validation error."""
    write_next(valid_folder, value)
    doc = load_document(valid_folder)
    errors = validate(doc)
    assert errors, f"next-object-number {value!r} produced no validation error"


@pytest.mark.parametrize(
    "value",
    [
        "0",  # exactly on the limit (and lower than the highest object present)
        "1",  # just above the limit (still lower than the highest present)
        "3",  # one more than the highest object present
        "9223372036854775808",  # 2**63
        "123456789012345678901234567890",  # a large whole number
    ],
)
def test_store_srs_2_whole_number_of_0_or_more_is_valid(valid_folder, value):
    """MELLON-STORE-SRS-2: a whole number of 0 or more is no validation error,
    even one lower than the highest object number present (EDIT-SRS-1's
    rationale: an older Mellon could leave it so).

    Assumption: the requirement names the only number condition that is an
    error, so no other check fires on these values.
    """
    write_next(valid_folder, value)
    errors = validate(load_document(valid_folder))
    assert errors == [], f"next-object-number {value!r} produced validation errors {errors}"


def test_store_srs_2_absent_number_is_valid(valid_folder):
    """MELLON-STORE-SRS-2 with -1: an absent number counts as 0, which is a
    whole number of 0 or more, so no validation error."""
    write_next(valid_folder, None)
    errors = validate(load_document(valid_folder))
    assert errors == [], f"a manifest without next-object-number produced validation errors {errors}"


# Beyond the blind tests: the code, and what counts as a whole number.


@pytest.mark.parametrize("value", ["+3", " 3", "3 ", "1_000", "٣", "0x10"])
def test_validate_flags_bad_next_number(doc_folder: Path, value: str) -> None:
    """Only ASCII digits make a whole number here, though Python's `int`
    also takes a sign, spaces, underscores and other scripts' digits."""
    write_next(doc_folder, value)
    errors = [
        e for e in validate(load_document(doc_folder))
        if e.code == "DOC_BAD_NEXT_NUMBER"
    ]
    assert len(errors) == 1
    assert repr(value) in errors[0].message


def test_validate_passes_next_number_with_leading_zeros(doc_folder: Path) -> None:
    write_next(doc_folder, "007")
    assert "DOC_BAD_NEXT_NUMBER" not in _codes(validate(load_document(doc_folder)))


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


def _blind_codes(folder) -> list[str]:
    """Sorted validation error codes for the document in `folder`."""
    return sorted(e.code for e in validate(load_document(folder)))


def _blind_rule_codes(codes) -> list[str]:
    return sorted(c for c in codes if c.startswith("RULE_"))


def _blind_with_value(doc, obj_id: str, name: str, value: str):
    """`doc` with one object's attribute value replaced in memory. Reading a file removes
    whitespace at the start and end of an attribute value (MELLON-STORE-SRS-217), so a value
    with surrounding whitespace can only be given to validation this way."""
    import dataclasses

    obj = doc.objects[obj_id]
    new_obj = dataclasses.replace(obj, attrs={**obj.attrs, name: value})
    return dataclasses.replace(doc, objects={**doc.objects, obj_id: new_obj})


def _blind_with_metadata_date(doc, value: str):
    import dataclasses

    return dataclasses.replace(doc, metadata=dataclasses.replace(doc.metadata, date=value))


def _blind_is_gregorian(text: str) -> bool:
    """MELLON-CHECK-SRS-21/-42's form, worked out independently: ten ASCII characters
    YYYY-MM-DD naming a Gregorian date from 0001-01-01 to 9999-12-31."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return False
    try:
        datetime.date(int(text[:4]), int(text[5:7]), int(text[8:]))
    except ValueError:
        return False
    return True


def _blind_date_samples(seed: int, count: int) -> list[str]:
    """Date-shaped strings: random year, month and day (in and out of range), some with one
    character dropped, one digit inserted, or a separator changed."""
    rng = random.Random(seed)
    samples: set[str] = set()
    while len(samples) < count:
        y = rng.choice([0, 1, 4, 100, 400, 1900, 2000, 2023, 2024, 2026, 2100, 9999,
                        rng.randint(0, 9999)])
        text = f"{y:04d}-{rng.randint(0, 13):02d}-{rng.randint(0, 32):02d}"
        r = rng.random()
        if r < 0.12:
            i = rng.randrange(len(text))
            text = text[:i] + text[i + 1:]
        elif r < 0.22:
            i = rng.randrange(len(text) + 1)
            text = text[:i] + str(rng.randint(0, 9)) + text[i:]
        elif r < 0.30:
            text = text.replace("-", rng.choice("/."), 1)
        samples.add(text)
    return sorted(samples)


def _blind_edits(rng: random.Random, word: str) -> str:
    """`word` unchanged, or with one letter's case flipped, one character dropped or
    doubled, or a space added."""
    r = rng.random()
    if r < 0.25 or not word:
        return word
    i = rng.randrange(len(word))
    if r < 0.5:
        return word[:i] + word[i].swapcase() + word[i + 1:]
    if r < 0.65:
        return word[:i] + word[i + 1:]
    if r < 0.8:
        return word[:i] + word[i] + word[i:]
    return rng.choice([" " + word, word + " ", word[:i] + " " + word[i:]])


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


@pytest.mark.parametrize("directive", [None, "optional", "forbidden", "required"])
def test_check_srs_13_required_attribute_missing_whatever_rule_directive(tmp_path, directive):
    """MELLON-CHECK-SRS-13: "If a required attribute is not set on an object, Mellon shall
    report the validation error ATTR_MISSING_REQUIRED, whatever directive a rule gives for
    the attribute." Condition: status is required by the manifest and not set; a rule with an
    empty condition makes it optional, forbids it or requires it (or there is no rule)."""
    attrs = (
        ("type", "enum", True, ("Heading", "Requirement")),
        ("status", "enum", True, ("Draft", "Approved")),
        ("rationale", "text", False, ()),
    )
    rules = None if directive is None else [({}, {"status": directive}, ())]
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement"}},
                              attrs=attrs, rules=rules)
    codes = _blind_codes(folder)
    assert codes.count("ATTR_MISSING_REQUIRED") == 1, codes
    if directive == "forbidden":
        assert "RULE_FORBIDDEN_PRESENT" not in codes, (
            f"status isn't set, so a forbidding rule isn't broken: {codes}")


@pytest.mark.parametrize("value", ["draft", "DRAFT"])
def test_check_srs_16_enum_near_miss_is_bad_enum(tmp_path, value):
    """MELLON-CHECK-SRS-16: "If an object's non-blank value for an enumeration attribute
    isn't exactly one of the attribute's options, letter case included, Mellon shall report
    the validation error ATTR_BAD_ENUM." Condition: the value differs from the option Draft
    only in letter case."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "status": value}})
    assert _blind_codes(folder) == ["ATTR_BAD_ENUM"]


@pytest.mark.parametrize("value", ["Draft ", " Draft"])
def test_check_srs_16_enum_value_with_surrounding_space_is_bad_enum(tmp_path, value):
    """MELLON-CHECK-SRS-16: "If an object's non-blank value for an enumeration attribute
    isn't exactly one of the attribute's options ... ATTR_BAD_ENUM." Condition: the object's
    value (given in memory, since reading a file trims it) is Draft with a space."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "status": "Draft"}})
    doc = _blind_with_value(load_document(folder), "DOC-0", "status", value)
    assert sorted(e.code for e in validate(doc)) == ["ATTR_BAD_ENUM"]


def test_check_srs_16_generated_values_bad_enum_iff_not_exact_option(tmp_path):
    """MELLON-CHECK-SRS-16: "If an object's non-blank value for an enumeration attribute
    isn't exactly one of the attribute's options, letter case included, Mellon shall report
    the validation error ATTR_BAD_ENUM." Condition: generated non-blank values (options with
    a letter's case flipped, a character dropped or doubled, or a space added), given in
    memory: ATTR_BAD_ENUM exactly for those that aren't an option."""
    options = ("Draft", "Approved", "In Review")
    attrs = (("type", "enum", True, ("Heading", "Requirement")),
             ("status", "enum", False, options))
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "status": "Draft"}},
                              attrs=attrs)
    doc = load_document(folder)
    rng = random.Random(16)
    samples = sorted({_blind_edits(rng, rng.choice(options)) for _ in range(400)})
    samples = [v for v in samples if v.strip()]
    assert sum(v in options for v in samples) == 3 and len(samples) > 40, samples
    mismatches = []
    for value in samples:
        expected = [] if value in options else ["ATTR_BAD_ENUM"]
        got = sorted(e.code for e in validate(_blind_with_value(doc, "DOC-0", "status", value)))
        if got != expected:
            mismatches.append((value, got, expected))
    assert not mismatches, mismatches[:10]


@pytest.mark.parametrize("value", ["Draft", "Approved"])
def test_check_srs_16_enum_exact_option_is_not_bad_enum(tmp_path, value):
    """MELLON-CHECK-SRS-16: "... isn't exactly one of the attribute's options, letter case
    included ... ATTR_BAD_ENUM." Condition: the value is exactly an option, so no error."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "status": value}})
    assert _blind_codes(folder) == []


_BLIND_BOOL_ATTRS = _BLIND_DEFAULT_ATTRS + (("flag", "boolean", False, ()),)


@pytest.mark.parametrize("value", ["TRUE", "False", "1", "0"])
def test_check_srs_20_boolean_not_lower_case_true_false_is_bad_boolean(tmp_path, value):
    """MELLON-CHECK-SRS-20: "If an object's non-blank value for a boolean attribute isn't
    exactly true or false, in lower case, Mellon shall report the validation error
    ATTR_BAD_BOOLEAN." Condition: other letter case, or digits."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "flag": value}},
                              attrs=_BLIND_BOOL_ATTRS)
    assert _blind_codes(folder) == ["ATTR_BAD_BOOLEAN"]


@pytest.mark.parametrize("value", [" true", "true "])
def test_check_srs_20_boolean_with_surrounding_space_is_bad_boolean(tmp_path, value):
    """MELLON-CHECK-SRS-20: "... isn't exactly true or false, in lower case ...
    ATTR_BAD_BOOLEAN." Condition: the object's value (given in memory, since reading a file
    trims it) is true with a space."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "flag": "true"}},
                              attrs=_BLIND_BOOL_ATTRS)
    doc = _blind_with_value(load_document(folder), "DOC-0", "flag", value)
    assert sorted(e.code for e in validate(doc)) == ["ATTR_BAD_BOOLEAN"]


def test_check_srs_20_generated_values_bad_boolean_iff_not_true_or_false(tmp_path):
    """MELLON-CHECK-SRS-20: "If an object's non-blank value for a boolean attribute isn't
    exactly true or false, in lower case, Mellon shall report the validation error
    ATTR_BAD_BOOLEAN." Condition: generated non-blank values (true and false with edits, and
    random strings of their letters and digits), given in memory."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "flag": "true"}},
                              attrs=_BLIND_BOOL_ATTRS)
    doc = load_document(folder)
    rng = random.Random(20)
    samples = {_blind_edits(rng, rng.choice(["true", "false"])) for _ in range(300)}
    samples |= {"".join(rng.choice("truefalsTRUEFALS01 ") for _ in range(rng.randint(1, 6)))
                for _ in range(300)}
    samples = sorted(v for v in samples if v.strip())
    assert {"true", "false"} <= set(samples) and len(samples) > 100
    mismatches = []
    for value in samples:
        expected = [] if value in ("true", "false") else ["ATTR_BAD_BOOLEAN"]
        got = sorted(e.code for e in validate(_blind_with_value(doc, "DOC-0", "flag", value)))
        if got != expected:
            mismatches.append((value, got, expected))
    assert not mismatches, mismatches[:10]


@pytest.mark.parametrize("value", ["true", "false"])
def test_check_srs_20_boolean_true_false_is_not_bad_boolean(tmp_path, value):
    """MELLON-CHECK-SRS-20: "... isn't exactly true or false, in lower case ...
    ATTR_BAD_BOOLEAN." Condition: exactly true or false, so no error."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "flag": value}},
                              attrs=_BLIND_BOOL_ATTRS)
    assert _blind_codes(folder) == []


_BLIND_VALID_DATES = ["2026-02-10", "2024-02-29", "0001-01-01", "9999-12-31"]


_BLIND_INVALID_DATES = [
    "2026-02-30", "2023-02-29", "2026-13-01", "2026-00-10", "2026-02-00", "0000-01-01",
    "20260210", "2026-W06-2", "2026-041", "2026-02-10T00:00", "2026-2-10",
    "٢٠٢٦-٠٢-١٠",  # Arabic-Indic digits
    "２０２６-０２-１０",  # full-width digits
    "10000-01-01",
]


# Reading a file trims an attribute value (MELLON-STORE-SRS-217), so these are given in memory.
_BLIND_SPACED_DATES = [" 2026-02-10", "2026-02-10 "]


_BLIND_DATE_ATTRS = _BLIND_DEFAULT_ATTRS + (("due", "date", False, ()),)


@pytest.mark.parametrize("value", _BLIND_INVALID_DATES, ids=ascii)
def test_check_srs_21_date_not_gregorian_yyyy_mm_dd_is_bad_date(tmp_path, value):
    """MELLON-CHECK-SRS-21: "If an object's non-blank value for a date attribute isn't ten
    ASCII characters YYYY-MM-DD naming a Gregorian date from 0001-01-01 to 9999-12-31, Mellon
    shall report the validation error ATTR_BAD_DATE." Condition: a day that doesn't exist,
    another ISO 8601 form, a space, non-ASCII digits, or a year out of range."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "due": value}},
                              attrs=_BLIND_DATE_ATTRS)
    assert _blind_codes(folder) == ["ATTR_BAD_DATE"]


@pytest.mark.parametrize("value", _BLIND_VALID_DATES)
def test_check_srs_21_gregorian_date_in_range_is_not_bad_date(tmp_path, value):
    """MELLON-CHECK-SRS-21: "... isn't ten ASCII characters YYYY-MM-DD naming a Gregorian
    date from 0001-01-01 to 9999-12-31 ... ATTR_BAD_DATE." Condition: a real date, a leap
    day and both ends of the range, so no error."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "due": value}},
                              attrs=_BLIND_DATE_ATTRS)
    assert _blind_codes(folder) == []


@pytest.mark.parametrize("value", _BLIND_SPACED_DATES, ids=ascii)
def test_check_srs_21_date_with_surrounding_space_is_bad_date(tmp_path, value):
    """MELLON-CHECK-SRS-21: "... isn't ten ASCII characters YYYY-MM-DD ... ATTR_BAD_DATE."
    Condition: the object's value (given in memory, since reading a file trims it) is a
    date with a space, eleven characters."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "due": "2026-02-10"}},
                              attrs=_BLIND_DATE_ATTRS)
    doc = _blind_with_value(load_document(folder), "DOC-0", "due", value)
    assert sorted(e.code for e in validate(doc)) == ["ATTR_BAD_DATE"]


def test_check_srs_21_generated_dates_bad_iff_not_gregorian_yyyy_mm_dd(tmp_path):
    """MELLON-CHECK-SRS-21: "If an object's non-blank value for a date attribute isn't ten
    ASCII characters YYYY-MM-DD naming a Gregorian date from 0001-01-01 to 9999-12-31, Mellon
    shall report the validation error ATTR_BAD_DATE." Condition: 600 generated date-shaped
    values, given in memory, against an independent Gregorian calendar."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "due": "2026-02-10"}},
                              attrs=_BLIND_DATE_ATTRS)
    doc = load_document(folder)
    samples = _blind_date_samples(21, 600)
    valid = sum(_blind_is_gregorian(v) for v in samples)
    assert 50 < valid < len(samples) - 50, f"generator: {valid} valid of {len(samples)}"
    mismatches = []
    for value in samples:
        expected = [] if _blind_is_gregorian(value) else ["ATTR_BAD_DATE"]
        got = sorted(e.code for e in validate(_blind_with_value(doc, "DOC-0", "due", value)))
        if got != expected:
            mismatches.append((value, got, expected))
    assert not mismatches, mismatches[:10]


_BLIND_KINDS = [("enum", ("A", "B")), ("integer", ()), ("real", ()), ("boolean", ()),
                ("date", ())]


@pytest.mark.parametrize("empty", ["", None], ids=["open-close", "self-closing"])
@pytest.mark.parametrize("kind,options", _BLIND_KINDS, ids=[k for k, _ in _BLIND_KINDS])
def test_check_srs_22_empty_value_fits_kind_of_optional_attribute(tmp_path, kind, options,
                                                                  empty):
    """MELLON-CHECK-SRS-22: "Mellon shall count an empty value as fitting the kind of its
    attribute." Condition: an optional attribute of each non-text kind set to an empty value
    (an empty attr element) on the object: no validation error."""
    attrs = _BLIND_DEFAULT_ATTRS + (("x", kind, False, options),)
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement", "x": empty}},
                              attrs=attrs)
    assert _blind_codes(folder) == []


@pytest.mark.parametrize("empty", ["", None], ids=["open-close", "self-closing"])
@pytest.mark.parametrize("kind,options", _BLIND_KINDS, ids=[k for k, _ in _BLIND_KINDS])
def test_check_srs_22_empty_required_value_is_only_missing_required(tmp_path, kind, options,
                                                                    empty):
    """MELLON-CHECK-SRS-22: "Mellon shall count an empty value as fitting the kind of its
    attribute." Condition: the attribute is required and set to an empty value: reported
    once, as ATTR_MISSING_REQUIRED (MELLON-CHECK-SRS-14), and not as a value of the wrong
    kind."""
    attrs = _BLIND_DEFAULT_ATTRS + (("x", kind, True, options),)
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement", "x": empty}},
                              attrs=attrs)
    assert _blind_codes(folder) == ["ATTR_MISSING_REQUIRED"]


@pytest.mark.parametrize("value", [
    "", "   ", "abc", "42", "true", "2026-02-30", "<&>", "line one\nline two\n\nline four",
    "Ünïcödé — 日本語 ✓", "x" * 10_000,
], ids=["empty", "spaces", "abc", "42", "true", "bad-date", "markup", "line-breaks",
        "non-ascii", "10000-chars"])
def test_check_srs_24_any_text_value_fits_text_kind(tmp_path, value):
    """MELLON-CHECK-SRS-24: "Mellon shall count each value of a text attribute as fitting the
    attribute's kind." Condition: an optional text attribute set to empty, blank, number-,
    boolean- and date-like text, escaped markup, line breaks, non-ASCII and 10,000
    characters: no validation error."""
    attrs = _BLIND_DEFAULT_ATTRS + (("note", "text", False, ()),)
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "note": value}}, attrs=attrs)
    assert _blind_codes(folder) == []


def test_check_srs_24_generated_text_values_fit_text_kind(tmp_path):
    """MELLON-CHECK-SRS-24: "Mellon shall count each value of a text attribute as fitting the
    attribute's kind." Condition: 300 generated strings of random Unicode characters, 0 to 40
    long, given in memory: no validation error."""
    attrs = _BLIND_DEFAULT_ATTRS + (("note", "text", False, ()),)
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "note": "x"}}, attrs=attrs)
    doc = load_document(folder)
    rng = random.Random(24)

    def char():
        return chr(rng.choice([rng.randint(0x20, 0x7E), rng.randint(0xA0, 0xD7FF),
                               rng.randint(0xE000, 0x10FFFF)]))

    failures = []
    for _ in range(300):
        value = "".join(char() for _ in range(rng.randint(0, 40)))
        codes = [e.code for e in validate(_blind_with_value(doc, "DOC-0", "note", value))]
        if codes:
            failures.append((value, codes))
    assert not failures, failures[:10]


def test_check_srs_34_link_target_missing_judged_by_working_copy(tmp_path):
    """MELLON-CHECK-SRS-34: "If an outgoing link's target object is in the link's own
    document and doesn't exist in the working copy, Mellon shall report the validation error
    LINK_TARGET_MISSING." Conditions: (1) the target is committed and exists: no error;
    (2) the target is created and not committed: no error; (3) the uncommitted target's file
    is deleted: LINK_TARGET_MISSING."""
    root = tmp_path / "repo"
    root.mkdir()
    _blind_git(root, "init", "-q")
    objects = {"DOC-0": {"type": "Heading"}, "DOC-1": {"type": "Requirement"}}
    folder = _blind_write_doc(root, "DOC", objects,
                              links={"DOC-1": [("DOC", "DOC-0", "refines", None)]})
    _blind_commit(root, "Add DOC")
    assert _blind_codes(folder) == [], "(1) target committed and present"

    _blind_write_doc(root, "DOC", {**objects, "DOC-5": {"type": "Requirement"}},
                     links={"DOC-1": [("DOC", "DOC-0", "refines", None),
                                      ("DOC", "DOC-5", "refines", None)]})
    assert _blind_codes(folder) == [], "(2) target created in the uncommitted change"

    (folder / "objects" / "DOC-5.xml").unlink()
    _blind_write(folder / "DOC.xml", _blind_manifest("DOC", outline=["DOC-0", "DOC-1"]))
    assert _blind_codes(folder) == ["LINK_TARGET_MISSING"], "(3) target file deleted"


@pytest.mark.parametrize("value", _BLIND_INVALID_DATES, ids=ascii)
def test_check_srs_42_metadata_date_not_gregorian_yyyy_mm_dd_is_meta_bad_date(tmp_path,
                                                                              value):
    """MELLON-CHECK-SRS-42: "If a document's non-empty metadata date isn't ten ASCII
    characters YYYY-MM-DD naming a Gregorian date from 0001-01-01 to 9999-12-31, Mellon shall
    report the validation error META_BAD_DATE." Condition: each invalid form."""
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement"}}, date=value)
    assert _blind_codes(folder) == ["META_BAD_DATE"]


@pytest.mark.parametrize("value", _BLIND_VALID_DATES)
def test_check_srs_42_metadata_gregorian_date_in_range_is_accepted(tmp_path, value):
    """MELLON-CHECK-SRS-42: "... non-empty metadata date isn't ... YYYY-MM-DD naming a
    Gregorian date from 0001-01-01 to 9999-12-31 ... META_BAD_DATE." Condition: each valid
    form, so no error."""
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement"}}, date=value)
    assert _blind_codes(folder) == []


@pytest.mark.parametrize("value", _BLIND_SPACED_DATES, ids=ascii)
def test_check_srs_42_metadata_date_with_surrounding_space_is_meta_bad_date(tmp_path, value):
    """MELLON-CHECK-SRS-42: "If a document's non-empty metadata date isn't ten ASCII
    characters YYYY-MM-DD ... META_BAD_DATE." Condition: the document's metadata date (given
    in memory) is a date with a space, eleven characters."""
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement"}})
    doc = _blind_with_metadata_date(load_document(folder), value)
    assert sorted(e.code for e in validate(doc)) == ["META_BAD_DATE"]


def test_check_srs_42_generated_metadata_dates_bad_iff_not_gregorian_yyyy_mm_dd(tmp_path):
    """MELLON-CHECK-SRS-42: "If a document's non-empty metadata date isn't ten ASCII
    characters YYYY-MM-DD naming a Gregorian date from 0001-01-01 to 9999-12-31, Mellon shall
    report the validation error META_BAD_DATE." Condition: 600 generated date-shaped values,
    given in memory, against an independent Gregorian calendar."""
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement"}})
    doc = load_document(folder)
    samples = _blind_date_samples(42, 600)
    valid = sum(_blind_is_gregorian(v) for v in samples)
    assert 50 < valid < len(samples) - 50, f"generator: {valid} valid of {len(samples)}"
    mismatches = []
    for value in samples:
        expected = [] if _blind_is_gregorian(value) else ["META_BAD_DATE"]
        got = sorted(e.code for e in validate(_blind_with_metadata_date(doc, value)))
        if got != expected:
            mismatches.append((value, got, expected))
    assert not mismatches, mismatches[:10]


def test_check_srs_51_each_rule_checked_against_each_object(tmp_path):
    """MELLON-CHECK-SRS-51: "When Mellon validates a document that has a rules file, Mellon
    shall check each rule of the rules file against each object of the document."
    Condition: two rules, each broken by two objects: one error per rule and object."""
    rules = [
        ({"type": "Requirement"}, {"rationale": "required"}, ()),
        ({"type": "Heading"}, {"rationale": "forbidden"}, ()),
    ]
    folder = _blind_write_doc(tmp_path, "DOC", {
        "DOC-0": {"type": "Requirement"},
        "DOC-1": {"type": "Requirement"},
        "DOC-2": {"type": "Heading", "rationale": "why"},
        "DOC-3": {"type": "Heading", "rationale": "why"},
    }, rules=rules)
    assert _blind_codes(folder) == ["RULE_FORBIDDEN_PRESENT", "RULE_FORBIDDEN_PRESENT",
                                    "RULE_REQUIRED_MISSING", "RULE_REQUIRED_MISSING"]


_BLIND_RULE_REQ_APPROVED = [({"type": "Requirement", "status": "Approved"},
                             {"rationale": "required"}, ())]


def test_check_srs_52_rule_applies_only_when_each_name_matches(tmp_path):
    """MELLON-CHECK-SRS-52: "Mellon shall apply a rule to an object if and only if the object
    matches each name in the rule condition." Condition: condition type Requirement and
    status Approved; of three objects without a rationale only one matches both names."""
    folder = _blind_write_doc(tmp_path, "DOC", {
        "DOC-0": {"type": "Requirement", "status": "Approved"},
        "DOC-1": {"type": "Requirement", "status": "Draft"},
        "DOC-2": {"type": "Heading", "status": "Approved"},
    }, rules=_BLIND_RULE_REQ_APPROVED)
    assert _blind_codes(folder) == ["RULE_REQUIRED_MISSING"]


@pytest.mark.parametrize("obj_type,status,expected", [
    ("Requirement", "Approved", ["RULE_REQUIRED_MISSING"]),
    ("Requirement", "Draft", []),
    ("Heading", "Approved", []),
])
def test_check_srs_52_rule_applies_per_object_iff_all_names_match(tmp_path, obj_type, status,
                                                                  expected):
    """MELLON-CHECK-SRS-52: "Mellon shall apply a rule to an object if and only if the object
    matches each name in the rule condition." Condition: one object at a time, matching both
    names, only type, or only status."""
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": obj_type, "status": status}},
                              rules=_BLIND_RULE_REQ_APPROVED)
    assert _blind_codes(folder) == expected


_BLIND_RULE_EMPTY_WHEN = [({}, {"rationale": "required"}, ())]


def test_check_srs_53_rule_with_empty_condition_applies_to_each_object(tmp_path):
    """MELLON-CHECK-SRS-53: "Mellon shall apply a rule whose rule condition names nothing to
    each object of the document." Condition: an empty when element requiring rationale; a
    heading, an Information object and a requirement without rationale."""
    folder = _blind_write_doc(tmp_path, "DOC", {
        "DOC-0": {"type": "Heading"},
        "DOC-1": {"type": "Information"},
        "DOC-2": {"type": "Requirement"},
    }, rules=_BLIND_RULE_EMPTY_WHEN)
    assert _blind_codes(folder) == ["RULE_REQUIRED_MISSING"] * 3


def test_check_srs_53_rule_with_empty_condition_met_by_each_object(tmp_path):
    """MELLON-CHECK-SRS-53: "Mellon shall apply a rule whose rule condition names nothing to
    each object of the document." Condition: each object has the required rationale."""
    folder = _blind_write_doc(tmp_path, "DOC", {
        "DOC-0": {"type": "Heading", "rationale": "r"},
        "DOC-1": {"type": "Information", "rationale": "r"},
        "DOC-2": {"type": "Requirement", "rationale": "r"},
    }, rules=_BLIND_RULE_EMPTY_WHEN)
    assert _blind_codes(folder) == []


_BLIND_WEIGHT_ATTRS = _BLIND_DEFAULT_ATTRS + (("weight", "integer", False, ()),)


@pytest.mark.parametrize("when,obj_attrs,expected", [
    ({"status": "Approved"}, {"status": "Approved"}, 1),
    ({"status": "Approved"}, {"status": "approved"}, 0),
    ({"status": "Approved"}, {}, 0),
    ({"weight": "1"}, {"weight": "1"}, 1),
    ({"weight": "1"}, {"weight": "1.0"}, 0),
    ({"weight": "1"}, {"weight": "01"}, 0),
    ({"status": ""}, {"status": ""}, 1),
    ({"status": ""}, {}, 0),
], ids=["Approved=Approved", "approved", "status-unset", "weight-1", "weight-1.0",
        "weight-01", "empty=empty", "empty-vs-unset"])
def test_check_srs_54_condition_matches_exact_attribute_value(tmp_path, when, obj_attrs,
                                                              expected):
    """MELLON-CHECK-SRS-54: "Mellon shall match an object to a name in a rule condition, other
    than has-outgoing-link and requirement-type, if and only if the object's attribute of
    that name has exactly the given value." Condition: letter case, an unset attribute,
    number-like text (1.0, 01 against 1) and the empty value against set-empty and unset."""
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement", **obj_attrs}},
                              attrs=_BLIND_WEIGHT_ATTRS,
                              rules=[(when, {"rationale": "required"}, ())])
    codes = _blind_codes(folder)
    assert codes.count("RULE_REQUIRED_MISSING") == expected, codes


def test_check_srs_54_condition_doesnt_match_value_with_trailing_space(tmp_path):
    """MELLON-CHECK-SRS-54: "... if and only if the object's attribute of that name has
    exactly the given value." Condition: condition status Approved; the object's status
    (given in memory, since reading a file trims it) is 'Approved ' with a trailing space."""
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "status": "Approved"}},
                              rules=[({"status": "Approved"}, {"rationale": "required"}, ())])
    doc = load_document(folder)
    assert [e.code for e in validate(doc)] == ["RULE_REQUIRED_MISSING"], "control: matches"
    codes = [e.code for e in validate(_blind_with_value(doc, "DOC-0", "status", "Approved "))]
    assert codes.count("RULE_REQUIRED_MISSING") == 0, codes


def test_check_srs_54_generated_condition_matches_iff_values_equal(tmp_path):
    """MELLON-CHECK-SRS-54: "Mellon shall match an object to a name in a rule condition, other
    than has-outgoing-link and requirement-type, if and only if the object's attribute of
    that name has exactly the given value." Condition: 300 generated pairs of condition value
    and object value (equal, or with case, character or space edits), given in memory."""
    import dataclasses

    from mellon.schema import Rule

    attrs = _BLIND_DEFAULT_ATTRS + (("tag", "text", False, ()),)
    folder = _blind_write_doc(tmp_path, "DOC",
                              {"DOC-0": {"type": "Requirement", "tag": "x"}}, attrs=attrs)
    doc = load_document(folder)
    rng = random.Random(54)
    words = ["Approved", "1", "1.0", "a b", "\u00dcn\u00ef", "x", "Draft"]
    mismatches, matched = [], 0
    for _ in range(300):
        cond = rng.choice(words)
        value = _blind_edits(rng, rng.choice([cond, cond, rng.choice(words)]))
        rule = Rule(conditions={"tag": cond}, directives={"rationale": "required"})
        case = dataclasses.replace(_blind_with_value(doc, "DOC-0", "tag", value), rules=(rule,))
        expected = 1 if value == cond else 0
        matched += expected
        got = [e.code for e in validate(case)].count("RULE_REQUIRED_MISSING")
        if got != expected:
            mismatches.append((cond, value, got, expected))
    assert 30 < matched < 270, f"generator: {matched} equal pairs of 300"
    assert not mismatches, mismatches[:10]


@pytest.mark.parametrize("req_type,expected", [("Safety", 1), ("Functional", 0), (None, 0)],
                         ids=["Safety", "Functional", "unset"])
def test_check_srs_55_requirement_type_matches_attribute_requirementtype(tmp_path, req_type,
                                                                         expected):
    """MELLON-CHECK-SRS-55: "When a rule condition names requirement-type, Mellon shall match
    the condition's value against the object's attribute named RequirementType." Condition:
    requirement-type Safety against RequirementType Safety, Functional and unset."""
    attrs = _BLIND_DEFAULT_ATTRS + (("RequirementType", "enum", False,
                                     ("Functional", "Safety")),)
    obj = {"type": "Requirement"}
    if req_type is not None:
        obj["RequirementType"] = req_type
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": obj}, attrs=attrs,
                              rules=[({"requirement-type": "Safety"},
                                      {"rationale": "required"}, ())])
    assert _blind_codes(folder) == ["RULE_REQUIRED_MISSING"] * expected


@pytest.mark.parametrize("req_type,expected", [("", 1), (None, 0)],
                         ids=["set-empty", "unset"])
def test_requirement_type_empty_condition_matches_only_a_set_attribute(tmp_path, req_type,
                                                                       expected):
    # MELLON-CHECK-SRS-55 is an alias, so it matches as MELLON-CHECK-SRS-54 does:
    # requirement-type="" matches RequirementType set empty, not RequirementType unset.
    attrs = _BLIND_DEFAULT_ATTRS + (("RequirementType", "enum", False,
                                     ("Functional", "Safety")),)
    obj = {"type": "Requirement"}
    if req_type is not None:
        obj["RequirementType"] = req_type
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": obj}, attrs=attrs,
                              rules=[({"requirement-type": ""},
                                      {"rationale": "required"}, ())])
    assert _blind_codes(folder) == ["RULE_REQUIRED_MISSING"] * expected


def _blind_link_rule_doc(root, has_link_value, rationale_on):
    objects = {"DOC-0": {"type": "Requirement"}, "DOC-1": {"type": "Requirement"}}
    if rationale_on:
        objects[rationale_on]["rationale"] = "r"
    return _blind_write_doc(root, "DOC", objects,
                            links={"DOC-1": [("DOC", "DOC-0", "refines", None)]},
                            rules=[({"has-outgoing-link": has_link_value},
                                    {"rationale": "required"}, ())])


@pytest.mark.parametrize("rationale_on,expected", [(None, 1), ("DOC-1", 0), ("DOC-0", 1)],
                         ids=["none-has-rationale", "linked-has-rationale",
                              "unlinked-has-rationale"])
def test_check_srs_56_has_outgoing_link_true_matches_object_with_link(tmp_path, rationale_on,
                                                                      expected):
    """MELLON-CHECK-SRS-56: "When a rule condition gives has-outgoing-link the value true,
    Mellon shall match an object to that name if and only if the object has at least one
    outgoing link." Condition: DOC-1 has one outgoing link, DOC-0 none; the only rule
    requires rationale."""
    folder = _blind_link_rule_doc(tmp_path, "true", rationale_on)
    assert _blind_codes(folder) == ["RULE_REQUIRED_MISSING"] * expected


@pytest.mark.parametrize("rationale_on,expected", [(None, 1), ("DOC-0", 0), ("DOC-1", 1)],
                         ids=["none-has-rationale", "unlinked-has-rationale",
                              "linked-has-rationale"])
def test_check_srs_57_has_outgoing_link_false_matches_object_without_link(tmp_path,
                                                                          rationale_on,
                                                                          expected):
    """MELLON-CHECK-SRS-57: "When a rule condition gives has-outgoing-link the value false,
    Mellon shall match an object to that name if and only if the object has no outgoing
    link." Condition: DOC-1 has one outgoing link, DOC-0 none; the only rule requires
    rationale."""
    folder = _blind_link_rule_doc(tmp_path, "false", rationale_on)
    assert _blind_codes(folder) == ["RULE_REQUIRED_MISSING"] * expected


_BLIND_HEADING = {"type": "Heading"}


_BLIND_HEADING_R = {"type": "Heading", "rationale": "r"}


@pytest.mark.parametrize("first,second,obj,expected", [
    ("forbidden", None, _BLIND_HEADING_R, ["RULE_FORBIDDEN_PRESENT"]),
    ("forbidden", "optional", _BLIND_HEADING_R, []),
    ("forbidden", "optional", _BLIND_HEADING, []),
    ("required", None, _BLIND_HEADING, ["RULE_REQUIRED_MISSING"]),
    ("required", "optional", _BLIND_HEADING, []),
    ("required", "optional", _BLIND_HEADING_R, []),
], ids=["forbid-alone-with", "forbid-then-optional-with", "forbid-then-optional-without",
        "require-alone-without", "require-then-optional-without",
        "require-then-optional-with"])
def test_check_srs_62_deciding_optional_directive_reports_neither(tmp_path, first, second,
                                                                  obj, expected):
    """MELLON-CHECK-SRS-62: "When the rule directive that decides an attribute for an object
    makes the attribute optional, Mellon shall report neither RULE_REQUIRED_MISSING nor
    RULE_FORBIDDEN_PRESENT for that attribute." Condition: rule 1 (type Heading) forbids or
    requires rationale; rule 2 (type Heading) makes it optional; a heading with and without
    rationale. Rows with rule 1 alone show rule 1 fires on its own."""
    rules = [({"type": "Heading"}, {"rationale": first}, ())]
    if second:
        rules.append(({"type": "Heading"}, {"rationale": second}, ()))
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": obj}, rules=rules)
    assert _blind_codes(folder) == expected


_BLIND_R_REQUIRE = ({"type": "Requirement"}, {"rationale": "required"}, ())


_BLIND_R_FORBID = ({"type": "Requirement", "status": "Approved"}, {"rationale": "forbidden"},
                   ())


@pytest.mark.parametrize("order,obj,expected", [
    ("require-first", {"status": "Approved", "rationale": "r"}, ["RULE_FORBIDDEN_PRESENT"]),
    ("require-first", {"status": "Approved"}, []),
    ("require-first", {"status": "Draft"}, ["RULE_REQUIRED_MISSING"]),
    ("forbid-first", {"status": "Approved", "rationale": "r"}, []),
    ("forbid-first", {"status": "Approved"}, ["RULE_REQUIRED_MISSING"]),
    ("forbid-first", {"status": "Draft"}, ["RULE_REQUIRED_MISSING"]),
], ids=["a-approved-with", "b-approved-without", "c-draft-without", "swapped-a",
        "swapped-b", "swapped-c"])
def test_check_srs_63_last_rule_in_file_decides_attribute(tmp_path, order, obj, expected):
    """MELLON-CHECK-SRS-63: "When several rules apply to one object and give directives for
    the same attribute, Mellon shall use only the directive of the rule that comes last in
    the rules file." Condition: rule 'type Requirement requires rationale' and rule 'type
    Requirement and status Approved forbids rationale', in both orders."""
    rules = ([_BLIND_R_REQUIRE, _BLIND_R_FORBID] if order == "require-first"
             else [_BLIND_R_FORBID, _BLIND_R_REQUIRE])
    folder = _blind_write_doc(tmp_path, "DOC", {"DOC-0": {"type": "Requirement", **obj}},
                              rules=rules)
    assert _blind_rule_codes(_blind_codes(folder)) == expected


def test_check_srs_63_generated_rule_lists_last_applying_directive_decides(tmp_path):
    """MELLON-CHECK-SRS-63: "When several rules apply to one object and give directives for
    the same attribute, Mellon shall use only the directive of the rule that comes last in
    the rules file." Condition: 300 generated lists of 1 to 5 rules, each with a condition
    that does or doesn't apply to a Requirement/Approved object and a required, optional or
    forbidden directive for rationale; the object with and without rationale. The last
    applying rule decides (MELLON-CHECK-SRS-59, -61, -62)."""
    import dataclasses

    from mellon.schema import Rule

    conditions = [({}, True), ({"type": "Requirement"}, True),
                  ({"type": "Requirement", "status": "Approved"}, True),
                  ({"type": "Heading"}, False), ({"status": "Draft"}, False)]
    docs = {}
    for has_rationale in (False, True):
        obj = {"type": "Requirement", "status": "Approved"}
        if has_rationale:
            obj["rationale"] = "r"
        docs[has_rationale] = load_document(
            _blind_write_doc(tmp_path / str(has_rationale), "DOC", {"DOC-0": obj}))
    rng = random.Random(63)
    mismatches, decided_by_later = [], 0
    for _ in range(300):
        rules, decided, applying = [], None, 0
        for _ in range(rng.randint(1, 5)):
            cond, applies = rng.choice(conditions)
            directive = rng.choice(["required", "optional", "forbidden"])
            rules.append(Rule(conditions=dict(cond), directives={"rationale": directive}))
            if applies:
                decided, applying = directive, applying + 1
        decided_by_later += applying > 1
        has_rationale = rng.random() < 0.5
        expected = []
        if decided == "required" and not has_rationale:
            expected = ["RULE_REQUIRED_MISSING"]
        elif decided == "forbidden" and has_rationale:
            expected = ["RULE_FORBIDDEN_PRESENT"]
        case = dataclasses.replace(docs[has_rationale], rules=tuple(rules))
        got = _blind_rule_codes(e.code for e in validate(case))
        if got != expected:
            mismatches.append(([(r.conditions, r.directives) for r in rules], has_rationale,
                               got, expected))
    assert decided_by_later > 50, "generator: too few lists where several rules apply"
    assert not mismatches, mismatches[:5]


def _blind_parent_doc(root, doc_id):
    _blind_write_doc(root, doc_id, {
        f"{doc_id}-0": {"type": "Heading"},
        f"{doc_id}-1": {"type": "Requirement", "status": "Draft"},
    })


@pytest.mark.parametrize("links,expected", [
    ([("PARENT", "PARENT-1", "satisfies", None)], []),
    ([("PARENT", "PARENT-1", "refines", None)], []),
    ([("CHILD", "CHILD-0", "refines", None)], ["RULE_LINK_REQUIRED_MISSING"]),
    ([], ["RULE_LINK_REQUIRED_MISSING"]),
], ids=["satisfies-parent", "refines-parent", "own-document-only", "no-link"])
def test_check_srs_64_rule_link_required_to_document(tmp_path, links, expected):
    """MELLON-CHECK-SRS-64: "If a rule that applies to an object requires a link to a
    document, and the object has no outgoing link to that document, Mellon shall report the
    validation error RULE_LINK_REQUIRED_MISSING." Condition: rule type Requirement requires a
    link to PARENT; the requirement CHILD-1 links to PARENT (satisfies or refines), only to
    its own document, or nowhere; the heading CHILD-0 (no links) isn't matched."""
    _blind_parent_doc(tmp_path, "PARENT")
    folder = _blind_write_doc(tmp_path, "CHILD", {
        "CHILD-0": {"type": "Heading"},
        "CHILD-1": {"type": "Requirement", "status": "Draft"},
    }, parents=("PARENT",), links={"CHILD-1": links} if links else None,
        rules=[({"type": "Requirement"}, {}, ("PARENT",))])
    assert _blind_codes(folder) == expected


@pytest.mark.parametrize("status,targets,expected", [
    ("Approved", ["PARENT"], 1),
    ("Approved", ["GRAND"], 1),
    ("Approved", ["PARENT", "GRAND"], 0),
    ("Approved", [], 2),
    ("Draft", ["PARENT"], 0),
], ids=["approved-parent-only", "approved-grand-only", "approved-both", "approved-neither",
        "draft-parent-only"])
def test_check_srs_66_link_demands_of_several_rules_add_up(tmp_path, status, targets,
                                                           expected):
    """MELLON-CHECK-SRS-66: "When several rules apply to one object and require links, Mellon
    shall require an outgoing link to each document named by a link directive of a rule that
    applies to the object." Condition: rule 1 (type Requirement) requires a link to PARENT,
    rule 2 (status Approved) a link to GRAND."""
    _blind_parent_doc(tmp_path, "GRAND")
    _blind_parent_doc(tmp_path, "PARENT")
    links = [(doc, f"{doc}-1", "satisfies", None) for doc in targets]
    folder = _blind_write_doc(tmp_path, "CHILD", {
        "CHILD-0": {"type": "Requirement", "status": status},
    }, parents=("PARENT", "GRAND"), links={"CHILD-0": links} if links else None, rules=[
        ({"type": "Requirement"}, {}, ("PARENT",)),
        ({"status": "Approved"}, {}, ("GRAND",)),
    ])
    assert _blind_codes(folder) == ["RULE_LINK_REQUIRED_MISSING"] * expected
