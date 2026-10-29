"""Filter engine — evaluates a `Filter` against a `Document`.

Pure module: no Qt, no IO. The engine returns a `set[str]` of matching
object ids; callers (the grid proxy, MCP serializer, future CLI) decide
how to apply that set.

Operator codes are deliberately short and stable across XML, MCP, and
GUI; user-facing labels live in `OPERATORS_LABEL`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Callable

from .schema import Document, Filter, FilterRule


class FilterError(Exception):
    """Raised when a filter is structurally invalid (unknown op, malformed value)."""


# Per-kind operator allow-list. Single source of truth; loader, dialog, and
# evaluator all read from this. "is-empty" / "is-not-empty" check whether the
# attribute value is missing or blank, regardless of kind.
OPS_BY_KIND: dict[str, tuple[str, ...]] = {
    "enum":    ("eq", "ne", "is-empty", "is-not-empty"),
    "text":    ("contains", "not-contains", "starts-with",
                "eq", "ne", "is-empty", "is-not-empty"),
    "integer": ("eq", "ne", "lt", "le", "gt", "ge", "between",
                "is-empty", "is-not-empty"),
    "real":    ("eq", "ne", "lt", "le", "gt", "ge", "between",
                "is-empty", "is-not-empty"),
    "boolean": ("is-true", "is-false", "is-empty"),
    "date":    ("eq", "ne", "before", "after", "between",
                "is-empty", "is-not-empty"),
}

# Operator code → human label, used by the dialog combobox and the pill
# summary. Kept terse on purpose.
OPERATORS_LABEL: dict[str, str] = {
    "eq":           "equals",
    "ne":           "does not equal",
    "contains":     "contains",
    "not-contains": "does not contain",
    "starts-with":  "starts with",
    "lt":           "<",
    "le":           "≤",
    "gt":           ">",
    "ge":           "≥",
    "between":      "between",
    "before":       "before",
    "after":        "after",
    "is-empty":     "is empty",
    "is-not-empty": "is not empty",
    "is-true":      "is true",
    "is-false":     "is false",
    "present":      "is present",
    "absent":       "is absent",
}

# Rule kinds the engine accepts. The dialog also uses these as selector codes.
KINDS: tuple[str, ...] = ("attr", "has-outgoing", "has-incoming")
LINK_OPS: tuple[str, ...] = ("present", "absent")
COMBINATORS: tuple[str, ...] = ("all", "any")


def valid_ops_for_kind(kind: str) -> tuple[str, ...]:
    """Return the operator codes valid for an attribute of the given kind.

    Unknown kinds return an empty tuple — the dialog uses that as a sentinel
    for "no attribute selected".
    """
    return OPS_BY_KIND.get(kind, ())


# Caller-supplied lookup for cross-document incoming links. Given a target
# object id, returns an iterable of source doc ids that link to it. None
# means "use same-document scan only".
IncomingProvider = Callable[[str], Iterable[str]]


def evaluate(
    doc: Document,
    filt: Filter,
    *,
    incoming_provider: IncomingProvider | None = None,
) -> set[str]:
    """Return the set of object ids in `doc` that match `filt`.

    An empty filter matches every object in the outline. The `keep_ancestors`
    post-pass unions in every matched object's outline ancestors so heading
    context survives the filter.
    """
    if filt.combinator not in COMBINATORS:
        raise FilterError(f"unknown combinator {filt.combinator!r}")

    all_ids = doc.iter_outline_ids()
    if filt.is_empty:
        return set(all_ids)

    rules = filt.rules
    matched: set[str] = set()

    for obj_id in all_ids:
        obj = doc.objects.get(obj_id)
        if obj is None:
            continue
        results = [_match(rule, obj_id, obj, doc, incoming_provider) for rule in rules]
        if filt.combinator == "all":
            ok = all(results)
        else:
            ok = any(results)
        if ok:
            matched.add(obj_id)

    if filt.keep_ancestors and matched:
        matched = _with_ancestors(doc, matched)
    return matched


def _with_ancestors(doc: Document, matched: set[str]) -> set[str]:
    """Union every matched id's outline ancestor chain back into the set."""
    out = set(matched)
    for obj_id in matched:
        path = doc.outline_path(obj_id)
        if path:
            out.update(path)
    return out


def _match(
    rule: FilterRule,
    obj_id: str,
    obj,
    doc: Document,
    incoming_provider: IncomingProvider | None,
) -> bool:
    if rule.kind == "attr":
        return _match_attr(rule, obj, doc)
    if rule.kind == "has-outgoing":
        return _match_has_outgoing(rule, obj_id, doc)
    if rule.kind == "has-incoming":
        return _match_has_incoming(rule, obj_id, doc, incoming_provider)
    raise FilterError(f"unknown rule kind {rule.kind!r}")


def _match_attr(rule: FilterRule, obj, doc: Document) -> bool:
    if not rule.attr:
        raise FilterError("attr rule missing attribute name")
    raw = (obj.attrs.get(rule.attr) or "").strip()

    op = rule.op
    if op == "is-empty":
        return raw == ""
    if op == "is-not-empty":
        return raw != ""

    # Look up the attribute kind so coercion respects the schema. `type` is
    # a system enum-ish attribute that doesn't appear in `doc.attributes`;
    # treat unknown attributes as text.
    spec = doc.attribute(rule.attr)
    kind = spec.kind if spec is not None else "text"

    if op not in valid_ops_for_kind(kind):
        raise FilterError(f"operator {op!r} not valid for {kind} attribute")

    if kind == "boolean":
        truthy = raw.lower() in ("true", "yes", "1")
        falsy = raw.lower() in ("false", "no", "0")
        if op == "is-true":
            return truthy
        if op == "is-false":
            return falsy
        raise FilterError(f"operator {op!r} not valid for boolean")

    if kind in ("integer", "real"):
        if raw == "":
            return False
        try:
            value = int(raw) if kind == "integer" else float(raw)
        except ValueError:
            return False
        return _compare_numeric(value, op, rule.value, kind)

    if kind == "date":
        if raw == "":
            return False
        try:
            value = date.fromisoformat(raw)
        except ValueError:
            return False
        return _compare_date(value, op, rule.value)

    # enum + text + unknown all fall through to string ops.
    target = rule.value
    if op == "eq":
        return raw == target
    if op == "ne":
        return raw != target
    if op == "contains":
        return target.lower() in raw.lower()
    if op == "not-contains":
        return target.lower() not in raw.lower()
    if op == "starts-with":
        return raw.lower().startswith(target.lower())
    raise FilterError(f"operator {op!r} not implemented for {kind}")


def _compare_numeric(value: float, op: str, target: str, kind: str) -> bool:
    cast = int if kind == "integer" else float
    if op == "between":
        lo, hi = _split_range(target, cast)
        return lo <= value <= hi
    try:
        rhs = cast(target)
    except ValueError:
        return False
    if op == "eq":
        return value == rhs
    if op == "ne":
        return value != rhs
    if op == "lt":
        return value < rhs
    if op == "le":
        return value <= rhs
    if op == "gt":
        return value > rhs
    if op == "ge":
        return value >= rhs
    raise FilterError(f"operator {op!r} not implemented for numeric")


def _compare_date(value: date, op: str, target: str) -> bool:
    if op == "between":
        lo_s, hi_s = target.split("|", 1) if "|" in target else (target, target)
        try:
            lo = date.fromisoformat(lo_s)
            hi = date.fromisoformat(hi_s)
        except ValueError as e:
            raise FilterError(f"invalid date range {target!r}: {e}") from e
        return lo <= value <= hi
    try:
        rhs = date.fromisoformat(target)
    except ValueError as e:
        raise FilterError(f"invalid date {target!r}: {e}") from e
    if op == "eq":
        return value == rhs
    if op == "ne":
        return value != rhs
    if op == "before":
        return value < rhs
    if op == "after":
        return value > rhs
    raise FilterError(f"operator {op!r} not implemented for date")


def _split_range(target: str, cast):
    if "|" not in target:
        raise FilterError(f"between requires '<lo>|<hi>', got {target!r}")
    lo_s, hi_s = target.split("|", 1)
    try:
        return cast(lo_s), cast(hi_s)
    except ValueError as e:
        raise FilterError(f"invalid range {target!r}: {e}") from e


def _match_has_outgoing(rule: FilterRule, obj_id: str, doc: Document) -> bool:
    if rule.op not in LINK_OPS:
        raise FilterError(f"has-outgoing op must be present|absent, got {rule.op!r}")
    out_links = doc.links.get(obj_id, ())
    if rule.attr:
        has_match = any(link.target_doc == rule.attr for link in out_links)
    else:
        has_match = bool(out_links)
    return has_match if rule.op == "present" else not has_match


def _match_has_incoming(
    rule: FilterRule,
    obj_id: str,
    doc: Document,
    incoming_provider: IncomingProvider | None,
) -> bool:
    if rule.op not in LINK_OPS:
        raise FilterError(f"has-incoming op must be present|absent, got {rule.op!r}")

    # Same-document scan covers the no-target-doc case and the
    # target_doc == doc.id case for free.
    same_doc_sources = {src for src, _ in doc.incoming_links(obj_id)}

    if rule.attr:
        if rule.attr == doc.id:
            has_match = bool(same_doc_sources)
        elif incoming_provider is not None:
            has_match = rule.attr in set(incoming_provider(obj_id))
        else:
            has_match = False
    else:
        cross_doc_sources: set[str] = set()
        if incoming_provider is not None:
            cross_doc_sources = {s for s in incoming_provider(obj_id) if s != doc.id}
        has_match = bool(same_doc_sources) or bool(cross_doc_sources)

    return has_match if rule.op == "present" else not has_match
