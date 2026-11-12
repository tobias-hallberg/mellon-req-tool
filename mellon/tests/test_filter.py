"""Tests for the filter engine (mellon.filter)."""

from __future__ import annotations

import pytest

from mellon import (
    Attribute,
    Document,
    Filter,
    FilterError,
    FilterRule,
    Link,
    Metadata,
    OutlineNode,
    RequirementObject,
    evaluate_filter,
    valid_ops_for_kind,
)


# A test document with attributes of every supported kind, two heading
# objects nesting one requirement each, and one cross-doc outgoing link from
# REQ-1.
def _doc() -> Document:
    attrs = (
        Attribute(name="type", kind="enum", required=True,
                  options=("Heading", "Requirement")),
        Attribute(name="priority", kind="enum", required=False,
                  options=("Low", "Medium", "High")),
        Attribute(name="weight", kind="integer", required=False),
        Attribute(name="ratio", kind="real", required=False),
        Attribute(name="approved", kind="boolean", required=False),
        Attribute(name="due", kind="date", required=False),
        Attribute(name="rationale", kind="text", required=False),
    )
    objects = {
        "H-1": RequirementObject(
            id="H-1",
            attrs={"type": "Heading"},
            description="Top heading",
        ),
        "REQ-1": RequirementObject(
            id="REQ-1",
            attrs={
                "type": "Requirement",
                "priority": "High",
                "weight": "10",
                "ratio": "1.5",
                "approved": "true",
                "due": "2026-05-01",
                "rationale": "because we must",
            },
            description="The system shall A.",
        ),
        "H-2": RequirementObject(
            id="H-2",
            attrs={"type": "Heading"},
            description="Second heading",
        ),
        "REQ-2": RequirementObject(
            id="REQ-2",
            attrs={
                "type": "Requirement",
                "priority": "Low",
                "weight": "3",
                "ratio": "0.5",
                "approved": "false",
                "due": "2026-06-01",
                "rationale": "",
            },
            description="The system shall B.",
        ),
    }
    outline = (
        OutlineNode(ref_id="H-1", children=(OutlineNode(ref_id="REQ-1"),)),
        OutlineNode(ref_id="H-2", children=(OutlineNode(ref_id="REQ-2"),)),
    )
    links = {
        "REQ-1": (
            Link(target_doc="PARENT", target_id="PARENT-1",
                 type="satisfies", ack=None),
        ),
    }
    return Document(
        id="DOC",
        metadata=Metadata(),
        parents=("PARENT",),
        attributes=attrs,
        outline=outline,
        objects=objects,
        links=links,
    )


def _attr_rule(attr: str, op: str, value: str = "") -> FilterRule:
    return FilterRule(kind="attr", attr=attr, op=op, value=value)


# --- empty filter ---------------------------------------------------------


def test_empty_filter_matches_every_outline_id() -> None:
    doc = _doc()
    matches = evaluate_filter(doc, Filter(slug="empty", name="Empty"))
    assert matches == set(doc.iter_outline_ids())


# --- enum + text + AND/OR -------------------------------------------------


def test_enum_eq_matches() -> None:
    doc = _doc()
    filt = Filter(
        slug="t", name="t",
        rules=(_attr_rule("priority", "eq", "High"),),
        keep_ancestors=False,
    )
    assert evaluate_filter(doc, filt) == {"REQ-1"}


def test_enum_eq_with_keep_ancestors_includes_heading() -> None:
    doc = _doc()
    filt = Filter(
        slug="t", name="t",
        rules=(_attr_rule("priority", "eq", "High"),),
        keep_ancestors=True,
    )
    assert evaluate_filter(doc, filt) == {"H-1", "REQ-1"}


def test_text_contains_is_case_insensitive() -> None:
    doc = _doc()
    filt = Filter(
        slug="t", name="t",
        rules=(_attr_rule("rationale", "contains", "MUST"),),
        keep_ancestors=False,
    )
    assert evaluate_filter(doc, filt) == {"REQ-1"}


def test_text_starts_with() -> None:
    doc = _doc()
    filt = Filter(
        slug="t", name="t",
        rules=(_attr_rule("rationale", "starts-with", "because"),),
        keep_ancestors=False,
    )
    assert evaluate_filter(doc, filt) == {"REQ-1"}


def test_is_empty_matches_blank_text() -> None:
    doc = _doc()
    filt = Filter(
        slug="t", name="t",
        rules=(_attr_rule("rationale", "is-empty"),),
        keep_ancestors=False,
    )
    # Headings have no rationale at all → also empty.
    assert evaluate_filter(doc, filt) == {"H-1", "H-2", "REQ-2"}


def test_combinator_all_and_any() -> None:
    doc = _doc()
    rules = (
        _attr_rule("priority", "eq", "High"),
        _attr_rule("approved", "is-true"),
    )
    all_filt = Filter(slug="a", name="a", combinator="all",
                      rules=rules, keep_ancestors=False)
    any_filt = Filter(slug="o", name="o", combinator="any",
                      rules=rules, keep_ancestors=False)
    assert evaluate_filter(doc, all_filt) == {"REQ-1"}
    assert evaluate_filter(doc, any_filt) == {"REQ-1"}

    # Different shape: priority=Low OR approved=true → REQ-1 (approved) and
    # REQ-2 (priority=Low).
    rules2 = (
        _attr_rule("priority", "eq", "Low"),
        _attr_rule("approved", "is-true"),
    )
    assert evaluate_filter(
        doc,
        Filter(slug="o2", name="o2", combinator="any",
               rules=rules2, keep_ancestors=False),
    ) == {"REQ-1", "REQ-2"}


# --- numeric & date -------------------------------------------------------


def test_integer_comparisons() -> None:
    doc = _doc()
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("weight", "ge", "5"),),
               keep_ancestors=False),
    )
    assert matches == {"REQ-1"}


def test_integer_between_inclusive() -> None:
    doc = _doc()
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("weight", "between", "3|10"),),
               keep_ancestors=False),
    )
    assert matches == {"REQ-1", "REQ-2"}


def test_real_lt() -> None:
    doc = _doc()
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("ratio", "lt", "1.0"),),
               keep_ancestors=False),
    )
    assert matches == {"REQ-2"}


def test_boolean_is_true_false() -> None:
    doc = _doc()
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("approved", "is-true"),),
               keep_ancestors=False),
    ) == {"REQ-1"}
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("approved", "is-false"),),
               keep_ancestors=False),
    ) == {"REQ-2"}


def test_date_before_after_between() -> None:
    doc = _doc()
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("due", "before", "2026-05-15"),),
               keep_ancestors=False),
    ) == {"REQ-1"}
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("due", "after", "2026-05-15"),),
               keep_ancestors=False),
    ) == {"REQ-2"}
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("due", "between", "2026-04-01|2026-05-31"),),
               keep_ancestors=False),
    ) == {"REQ-1"}


# --- numeric/date coercion failures degrade gracefully -------------------


def test_numeric_unparseable_attribute_does_not_match() -> None:
    doc = _doc()
    # H-1 has no weight attribute → empty string → numeric op never matches.
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(_attr_rule("weight", "lt", "1000"),),
               keep_ancestors=False),
    )
    assert matches == {"REQ-1", "REQ-2"}


# --- link rules -----------------------------------------------------------


def test_has_outgoing_present_any_doc() -> None:
    doc = _doc()
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(FilterRule(kind="has-outgoing", op="present"),),
               keep_ancestors=False),
    )
    assert matches == {"REQ-1"}


def test_has_outgoing_present_target_doc_match_and_miss() -> None:
    doc = _doc()
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(FilterRule(kind="has-outgoing", attr="PARENT",
                                 op="present"),),
               keep_ancestors=False),
    ) == {"REQ-1"}
    assert evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(FilterRule(kind="has-outgoing", attr="OTHER",
                                 op="present"),),
               keep_ancestors=False),
    ) == set()


def test_has_outgoing_absent() -> None:
    doc = _doc()
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(FilterRule(kind="has-outgoing", op="absent"),),
               keep_ancestors=False),
    )
    assert matches == {"H-1", "H-2", "REQ-2"}


def test_has_incoming_uses_provider() -> None:
    doc = _doc()
    # No same-doc links target REQ-2; declare a cross-doc incoming via the
    # provider.
    incoming = {"REQ-2": ["OTHER-DOC"]}
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(FilterRule(kind="has-incoming", op="present"),),
               keep_ancestors=False),
        incoming_provider=lambda obj_id: incoming.get(obj_id, ()),
    )
    assert matches == {"REQ-2"}


def test_has_incoming_absent_with_provider() -> None:
    doc = _doc()
    incoming = {"REQ-2": ["OTHER-DOC"]}
    matches = evaluate_filter(
        doc,
        Filter(slug="t", name="t",
               rules=(FilterRule(kind="has-incoming", op="absent"),),
               keep_ancestors=False),
        incoming_provider=lambda obj_id: incoming.get(obj_id, ()),
    )
    assert matches == {"H-1", "H-2", "REQ-1"}


# --- error paths ----------------------------------------------------------


def test_unknown_combinator_raises() -> None:
    doc = _doc()
    with pytest.raises(FilterError, match="combinator"):
        evaluate_filter(doc, Filter(slug="t", name="t", combinator="maybe"))


def test_unknown_kind_raises() -> None:
    doc = _doc()
    bad = FilterRule(kind="weird", op="present")
    with pytest.raises(FilterError, match="rule kind"):
        evaluate_filter(doc, Filter(slug="t", name="t", rules=(bad,)))


def test_invalid_op_for_kind_raises() -> None:
    doc = _doc()
    # `priority` is enum; `lt` is only valid for numeric.
    bad = _attr_rule("priority", "lt", "Low")
    with pytest.raises(FilterError, match="not valid"):
        evaluate_filter(doc, Filter(slug="t", name="t", rules=(bad,)))


# --- valid_ops_for_kind sanity --------------------------------------------


def test_valid_ops_returned_per_kind() -> None:
    assert "eq" in valid_ops_for_kind("enum")
    assert "between" in valid_ops_for_kind("integer")
    assert "between" in valid_ops_for_kind("real")
    assert "before" in valid_ops_for_kind("date")
    assert "is-true" in valid_ops_for_kind("boolean")
    assert "starts-with" in valid_ops_for_kind("text")
    assert valid_ops_for_kind("nope") == ()


# MELLON-EDIT-SRS blind tests (176 and up)
from pathlib import Path  # noqa: E402

from tests.edit_b_support import _ALL_ATTR_OPS, _KIND_OPS, write_doc  # noqa: E402


# A value fitting each operator on the kind it belongs to.
_OP_VALUE = {
    "enum": {"eq": "Draft", "ne": "Draft"},
    "text": {"eq": "x", "ne": "x", "contains": "x", "not-contains": "x",
             "starts-with": "x"},
    "integer": {"eq": "3", "ne": "3", "lt": "3", "le": "3", "gt": "3",
                "ge": "3", "between": "1|5"},
    "real": {"eq": "3.5", "ne": "3.5", "lt": "3.5", "le": "3.5", "gt": "3.5",
             "ge": "3.5", "between": "1.5|5.5"},
    "boolean": {},
    "date": {"eq": "2026-01-01", "ne": "2026-01-01", "before": "2026-01-01",
             "after": "2026-01-01", "between": "2026-01-01|2026-12-31"},
}


_GENERIC_VALUE = {"eq": "3", "ne": "3", "contains": "x", "not-contains": "x",
                  "starts-with": "x", "lt": "3", "le": "3", "gt": "3", "ge": "3",
                  "between": "1|5", "before": "2026-01-01",
                  "after": "2026-01-01"}


_KIND_ATTR = {"enum": "status", "text": "note", "integer": "count",
              "real": "weight", "boolean": "flag", "date": "due"}


def _kinds_doc(root: Path) -> Path:
    attrs = [
        {"name": "type", "kind": "enum", "required": True,
         "options": ["Heading", "Requirement"]},
        {"name": "status", "kind": "enum", "required": False,
         "options": ["Draft", "Approved"]},
        {"name": "note", "kind": "text", "required": False},
        {"name": "count", "kind": "integer", "required": False},
        {"name": "weight", "kind": "real", "required": False},
        {"name": "flag", "kind": "boolean", "required": False},
        {"name": "due", "kind": "date", "required": False},
    ]
    return write_doc(root, "K", attrs=attrs, objects={
        "K-0": ({"type": "Requirement", "status": "Draft", "note": "xyz",
                 "count": "3", "weight": "3.5", "flag": "true",
                 "due": "2026-01-01"}, "one"),
        "K-1": ({"type": "Requirement"}, "two"),
    }, next_number=2)


def _apply(doc, kind: str, op: str, value: str | None = None):
    from mellon.filter import evaluate
    from mellon.schema import Filter, FilterRule

    if value is None:
        value = _OP_VALUE[kind].get(op, _GENERIC_VALUE.get(op, ""))
    rule = FilterRule(kind="attr", attr=_KIND_ATTR[kind], op=op, value=value)
    filt = Filter(slug="f", name="F", combinator="all", keep_ancestors=False,
                  rules=(rule,))
    return evaluate(doc, filt)


def _check_operator(tmp_path, kind: str, op: str) -> None:
    from mellon.filter import FilterError
    from mellon.loader import load_document

    doc = load_document(_kinds_doc(tmp_path))
    if op in _KIND_OPS[kind]:
        result = _apply(doc, kind, op)
        assert isinstance(result, set), f"{op} on {kind} was not applied"
    else:
        with pytest.raises(FilterError):
            _apply(doc, kind, op)


@pytest.mark.parametrize("op", _ALL_ATTR_OPS)
def test_edit_srs_292_enumeration_accepts_only_eq_ne_is_empty_is_not_empty(
        tmp_path, op):
    """MELLON-EDIT-SRS-292: applying a filter, Mellon accepts on an
    enumeration only eq, ne, is-empty and is-not-empty; each other operator is
    refused."""
    _check_operator(tmp_path, "enum", op)


@pytest.mark.parametrize("op", _ALL_ATTR_OPS)
def test_edit_srs_293_text_accepts_only_its_seven_operators(tmp_path, op):
    """MELLON-EDIT-SRS-293: on a text attribute only contains, not-contains,
    starts-with, eq, ne, is-empty and is-not-empty are accepted."""
    _check_operator(tmp_path, "text", op)


@pytest.mark.parametrize("kind", ["integer", "real"])
@pytest.mark.parametrize("op", _ALL_ATTR_OPS)
def test_edit_srs_294_number_accepts_only_its_nine_operators(tmp_path, kind, op):
    """MELLON-EDIT-SRS-294: on an integer or real attribute only eq, ne, lt,
    le, gt, ge, between, is-empty and is-not-empty are accepted."""
    _check_operator(tmp_path, kind, op)


@pytest.mark.parametrize("op", _ALL_ATTR_OPS)
def test_edit_srs_295_boolean_accepts_only_is_true_is_false_is_empty(tmp_path, op):
    """MELLON-EDIT-SRS-295: on a boolean attribute only is-true, is-false and
    is-empty are accepted."""
    _check_operator(tmp_path, "boolean", op)


@pytest.mark.parametrize("op", _ALL_ATTR_OPS)
def test_edit_srs_296_date_accepts_only_its_seven_operators(tmp_path, op):
    """MELLON-EDIT-SRS-296: on a date attribute only eq, ne, before, after,
    between, is-empty and is-not-empty are accepted."""
    _check_operator(tmp_path, "date", op)


def _undefined_attr_rule(op: str, value: str = ""):
    from mellon.schema import Filter, FilterRule

    return Filter(slug="f", name="F", combinator="all", keep_ancestors=False,
                  rules=(FilterRule(kind="attr", attr="nosuch", op=op,
                                    value=value),))


@pytest.mark.parametrize("op,value,matches_all", [
    ("is-empty", "", True), ("ne", "x", True), ("contains", "x", False)],
    ids=["is-empty", "ne", "contains"])
def test_edit_srs_297_undefined_attribute_is_treated_as_text(
        tmp_path, op, value, matches_all):
    """MELLON-EDIT-SRS-297: a filter condition on an attribute the document
    doesn't define treats it as text: is-empty and ne x match each object
    (none has a value), contains x matches none."""
    from mellon.filter import evaluate
    from mellon.loader import load_document

    doc = load_document(_kinds_doc(tmp_path))
    got = evaluate(doc, _undefined_attr_rule(op, value))
    assert got == ({"K-0", "K-1"} if matches_all else set())


def test_edit_srs_297_undefined_attribute_refuses_operator_unfit_for_text(tmp_path):
    """MELLON-EDIT-SRS-297 with MELLON-EDIT-SRS-293: as a text attribute, an
    undefined attribute refuses lt."""
    from mellon.filter import FilterError, evaluate
    from mellon.loader import load_document

    doc = load_document(_kinds_doc(tmp_path))
    with pytest.raises(FilterError):
        evaluate(doc, _undefined_attr_rule("lt", "3"))


def _bounds_doc(root: Path) -> Path:
    attrs = [
        {"name": "type", "kind": "enum", "required": True,
         "options": ["Requirement"]},
        {"name": "weight", "kind": "real", "required": False},
        {"name": "due", "kind": "date", "required": False},
    ]
    return write_doc(root, "BD", attrs=attrs, objects={
        "BD-0": ({"type": "Requirement", "weight": "0.49", "due": "2026-04-30"}, "0"),
        "BD-1": ({"type": "Requirement", "weight": "0.5", "due": "2026-05-01"}, "1"),
        "BD-2": ({"type": "Requirement", "weight": "1.5", "due": "2026-06-01"}, "2"),
        "BD-3": ({"type": "Requirement", "weight": "1.51", "due": "2026-06-02"}, "3"),
        "BD-4": ({"type": "Requirement", "weight": "1.0", "due": "2026-05-15"}, "4"),
    }, next_number=5)


@pytest.mark.parametrize("attr,bounds", [
    ("weight", "0.5|1.5"), ("due", "2026-05-01|2026-06-01")], ids=["real", "date"])
def test_edit_srs_300_between_includes_both_bounds(tmp_path, attr, bounds):
    """MELLON-EDIT-SRS-300: between matches values from the lower to the upper
    bound, both included; values just outside (0.49 and 1.51; 2026-04-30 and
    2026-06-02) don't match."""
    from mellon.filter import evaluate
    from mellon.loader import load_document
    from mellon.schema import Filter, FilterRule

    doc = load_document(_bounds_doc(tmp_path))
    filt = Filter(slug="f", name="F", keep_ancestors=False, rules=(
        FilterRule(kind="attr", attr=attr, op="between", value=bounds),))
    assert evaluate(doc, filt) == {"BD-1", "BD-2", "BD-4"}


def _combinator_doc(root: Path) -> Path:
    attrs = [
        {"name": "type", "kind": "enum", "required": True,
         "options": ["Requirement"]},
        {"name": "priority", "kind": "enum", "required": False,
         "options": ["High", "Low"]},
        {"name": "approved", "kind": "boolean", "required": False},
    ]
    return write_doc(root, "CB", attrs=attrs, objects={
        "CB-0": ({"type": "Requirement", "priority": "High", "approved": "true"}, "A"),
        "CB-1": ({"type": "Requirement", "priority": "Low", "approved": "false"}, "B"),
    }, next_number=2)


@pytest.mark.parametrize("combinator,expected", [
    ("all", set()), ("any", {"CB-0", "CB-1"})], ids=["all", "any"])
def test_edit_srs_284_all_needs_each_condition_any_needs_one(
        tmp_path, combinator, expected):
    """MELLON-EDIT-SRS-284/-285: A (High, approved) meets only 'approved
    is-true'; B (Low, not approved) only 'priority eq Low'. Under all no
    object matches; under any both do."""
    from mellon.filter import evaluate
    from mellon.loader import load_document
    from mellon.schema import Filter, FilterRule

    doc = load_document(_combinator_doc(tmp_path))
    filt = Filter(slug="f", name="F", combinator=combinator,
                  keep_ancestors=False, rules=(
                      FilterRule(kind="attr", attr="priority", op="eq",
                                 value="Low"),
                      FilterRule(kind="attr", attr="approved", op="is-true")))
    assert evaluate(doc, filt) == expected
