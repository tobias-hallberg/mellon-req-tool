"""Consistency checks for a loaded Document.

Covers everything from PLAN.md § Verification that doesn't require git:
    - Manifest outline <-> objects consistency
    - Attribute schema consistency (names + enum values + required)
    - Link source/target existence (intra-doc only; cross-doc needs git_ops)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date as _date

from .schema import (
    TABLE_ALIGNMENTS,
    Document,
    RequirementObject,
    Rule,
    table_geometry_errors,
)


@dataclass(frozen=True, slots=True)
class ValidationError:
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


def validate(doc: Document) -> list[ValidationError]:
    """Return a list of problems found in `doc`. Empty = valid."""
    errors: list[ValidationError] = []
    errors.extend(_check_metadata(doc))
    errors.extend(_check_outline(doc))
    errors.extend(_check_attrs(doc))
    errors.extend(_check_tables(doc))
    errors.extend(_check_links(doc))
    errors.extend(_check_link_policy(doc))
    errors.extend(_check_rules(doc))
    return errors


def _check_metadata(doc: Document) -> list[ValidationError]:
    errors: list[ValidationError] = []
    date_value = doc.metadata.date
    if date_value:
        try:
            _date.fromisoformat(date_value)
        except ValueError:
            errors.append(
                ValidationError(
                    "META_BAD_DATE",
                    f"manifest metadata: date={date_value!r} is not "
                    f"an ISO 8601 date (YYYY-MM-DD)",
                )
            )
    return errors


def _check_outline(doc: Document) -> list[ValidationError]:
    errors: list[ValidationError] = []
    outline_ids = doc.iter_outline_ids()
    counts = Counter(outline_ids)

    for ref_id, n in counts.items():
        if n > 1:
            errors.append(
                ValidationError(
                    "OUTLINE_DUP",
                    f"{ref_id!r} appears {n} times in <outline>; must appear exactly once",
                )
            )
        if ref_id not in doc.objects:
            errors.append(
                ValidationError(
                    "OUTLINE_MISSING_OBJECT",
                    f"<outline> references {ref_id!r} but no objects/{ref_id}.xml exists",
                )
            )

    outline_set = set(outline_ids)
    for obj_id in doc.objects:
        if obj_id not in outline_set:
            errors.append(
                ValidationError(
                    "OBJECT_NOT_IN_OUTLINE",
                    f"object {obj_id!r} exists on disk but is not in <outline>",
                )
            )
    return errors


def _check_attrs(doc: Document) -> list[ValidationError]:
    errors: list[ValidationError] = []
    schema = {a.name: a for a in doc.attributes}

    for obj in doc.objects.values():
        for name, value in obj.attrs.items():
            attr = schema.get(name)
            if attr is None:
                errors.append(
                    ValidationError(
                        "ATTR_UNKNOWN",
                        f"object {obj.id!r}: attribute {name!r} is not declared in manifest",
                    )
                )
                continue
            err = _check_attr_value(obj.id, attr, value)
            if err is not None:
                errors.append(err)

        for attr in doc.attributes:
            # An empty value (e.g. an enum cleared in the GUI) counts as missing.
            if attr.required and not obj.attrs.get(attr.name, "").strip():
                errors.append(
                    ValidationError(
                        "ATTR_MISSING_REQUIRED",
                        f"object {obj.id!r}: required attribute {attr.name!r} is missing",
                    )
                )
    return errors


def _check_attr_value(obj_id, attr, value: str) -> "ValidationError | None":
    """Type-check a non-empty attribute value against its declared kind.

    Empty values are not type-checked here; `ATTR_MISSING_REQUIRED` covers
    the required-but-empty case so the user gets one diagnostic instead of two.
    """
    if not value:
        return None
    if attr.kind == "enum" and value not in attr.options:
        return ValidationError(
            "ATTR_BAD_ENUM",
            f"object {obj_id!r}: attribute {attr.name!r}={value!r} is not one of "
            f"{list(attr.options)}",
        )
    if attr.kind == "integer":
        try:
            int(value, 10)
        except ValueError:
            return ValidationError(
                "ATTR_BAD_INTEGER",
                f"object {obj_id!r}: attribute {attr.name!r}={value!r} is not "
                f"a base-10 integer",
            )
    elif attr.kind == "real":
        try:
            float(value)
        except ValueError:
            return ValidationError(
                "ATTR_BAD_REAL",
                f"object {obj_id!r}: attribute {attr.name!r}={value!r} is not "
                f"a real number",
            )
    elif attr.kind == "boolean":
        if value not in ("true", "false"):
            return ValidationError(
                "ATTR_BAD_BOOLEAN",
                f"object {obj_id!r}: attribute {attr.name!r}={value!r} is not "
                f"'true' or 'false'",
            )
    elif attr.kind == "date":
        try:
            _date.fromisoformat(value)
        except ValueError:
            return ValidationError(
                "ATTR_BAD_DATE",
                f"object {obj_id!r}: attribute {attr.name!r}={value!r} is not "
                f"an ISO 8601 date (YYYY-MM-DD)",
            )
    return None


def _check_tables(doc: Document) -> list[ValidationError]:
    """Flag malformed tables: bad alignment, bad spans, or inconsistent tiling.

    A table that loads off disk is already well-formed (the loader rejects
    inconsistent geometry). This catches tables built in memory and documents
    the invariant in one place.
    """
    errors: list[ValidationError] = []
    for obj in doc.objects.values():
        table = obj.table
        if table is None:
            continue
        for col in table.columns:
            if col.align not in TABLE_ALIGNMENTS:
                errors.append(
                    ValidationError(
                        "TABLE_BAD_ALIGN",
                        f"object {obj.id!r}: table column alignment "
                        f"{col.align!r} is not one of {sorted(TABLE_ALIGNMENTS)}",
                    )
                )
        for r, row in enumerate(table.rows):
            for cell in row.cells:
                if cell.colspan < 1 or cell.rowspan < 1:
                    errors.append(
                        ValidationError(
                            "TABLE_BAD_SPAN",
                            f"object {obj.id!r}: table row {r} has a cell with "
                            f"colspan={cell.colspan}, rowspan={cell.rowspan} "
                            f"(both must be >= 1)",
                        )
                    )
        if not table.columns and table.rows:
            errors.append(
                ValidationError(
                    "TABLE_NO_COLUMNS",
                    f"object {obj.id!r}: table has rows but no <col> declarations",
                )
            )
        else:
            for problem in table_geometry_errors(table):
                errors.append(
                    ValidationError("TABLE_GEOMETRY", f"object {obj.id!r}: {problem}")
                )
    return errors


def _check_links(doc: Document) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for source, link_list in doc.links.items():
        if source not in doc.objects:
            errors.append(
                ValidationError(
                    "LINK_SOURCE_MISSING",
                    f"links/{source}.xml: source {source!r} has no corresponding object",
                )
            )
        for link in link_list:
            if link.target_doc == doc.id and link.target_id not in doc.objects:
                errors.append(
                    ValidationError(
                        "LINK_TARGET_MISSING",
                        f"links/{source}.xml: link target {link.target_doc}/"
                        f"{link.target_id} not found in this document",
                    )
                )
    return errors


def _check_link_policy(doc: Document) -> list[ValidationError]:
    errors: list[ValidationError] = []
    allowed = {doc.id, *doc.parents}
    for source, link_list in doc.links.items():
        for link in link_list:
            if link.target_doc not in allowed:
                errors.append(
                    ValidationError(
                        "LINK_NOT_ALLOWED",
                        f"links/{source}.xml: link to {link.target_doc}/"
                        f"{link.target_id} is not allowed; {doc.id!r} may only "
                        f"link to itself or to declared parents "
                        f"{list(doc.parents)}",
                    )
                )
    return errors


def _rule_matches(rule: Rule, obj: RequirementObject, has_link: bool) -> bool:
    for key, expected in rule.conditions.items():
        if key == "type":
            if obj.attrs.get("type", "") != expected:
                return False
        elif key == "requirement-type":
            if obj.attrs.get("RequirementType", "") != expected:
                return False
        elif key == "has-outgoing-link":
            actual = "true" if has_link else "false"
            if actual != expected.lower():
                return False
        else:
            # Fallback: any unrecognised predicate matches `obj.attrs[key]`.
            if obj.attrs.get(key, "") != expected:
                return False
    return True


def effective_directives(
    rules: tuple[Rule, ...], obj: RequirementObject, has_link: bool
) -> dict[str, str]:
    """Merge all matching rules for `obj` in document order; later wins per attr."""
    out: dict[str, str] = {}
    for rule in rules:
        if _rule_matches(rule, obj, has_link):
            out.update(rule.directives)
    return out


def _check_rules(doc: Document) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not doc.rules:
        return errors
    for obj in doc.objects.values():
        outgoing = doc.links.get(obj.id, ())
        has_link = bool(outgoing)
        directives = effective_directives(doc.rules, obj, has_link)
        for attr_name, status in directives.items():
            value = obj.attrs.get(attr_name, "").strip()
            if status == "required" and not value:
                errors.append(
                    ValidationError(
                        "RULE_REQUIRED_MISSING",
                        f"object {obj.id!r}: rules require attribute "
                        f"{attr_name!r} but it is missing or empty",
                    )
                )
            elif status == "forbidden" and value:
                errors.append(
                    ValidationError(
                        "RULE_FORBIDDEN_PRESENT",
                        f"object {obj.id!r}: rules forbid attribute "
                        f"{attr_name!r} but it has value {value!r}",
                    )
                )

        link_targets = {link.target_doc for link in outgoing}
        for ld in _effective_link_directives(doc.rules, obj, has_link):
            if ld.status == "required" and ld.target_doc not in link_targets:
                errors.append(
                    ValidationError(
                        "RULE_LINK_REQUIRED_MISSING",
                        f"object {obj.id!r}: rules require an outgoing link to "
                        f"document {ld.target_doc!r} but none is present",
                    )
                )
    return errors


def _effective_link_directives(rules, obj, has_link):
    """Accumulate link directives from every matching rule."""
    out = []
    for rule in rules:
        if _rule_matches(rule, obj, has_link):
            out.extend(rule.link_directives)
    return out
