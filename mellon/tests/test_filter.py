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
