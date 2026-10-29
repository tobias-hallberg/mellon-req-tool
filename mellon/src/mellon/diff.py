"""Structured diff between two git refs of the same document.

Library entry point: ``compare_document(repo_root, doc_id, ref_a, ref_b)``
returns a ``DocumentDiff`` describing object membership changes plus, for
modified objects, attribute / description / outgoing-link deltas.

This is the V-model "what changed between Issue 1 and Issue 2" question.
MVP scope: library only, no MCP, no
GUI; no outline / move detection (DOORS treats Move as a first-class
category — slot reserved on ``ObjectDelta`` for when a GUI surface lands).

Match key is the object id (analogous to DOORS' Absolute Number) so the
diff is stable across outline reordering. Reading from git trees is
non-mutating, unlike DOORS Classic's Module Comparison Wizard which creates
a side-effect link module.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

import pygit2
from lxml import etree

from .git_ops import (
    SHORT_HASH_LEN,
    GitError,
    discover_repo,
    list_tree_filenames,
    show_blob_at,
)
from .schema import Link


# --- result dataclasses ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributeChange:
    """An attribute whose value differs between A and B.

    ``value_a`` / ``value_b`` is None when the attribute is absent on that
    side (attribute added or removed on this object). Empty-string vs None
    is a real distinction: empty value present vs attribute not set.
    """

    name: str
    value_a: str | None
    value_b: str | None


@dataclass(frozen=True, slots=True)
class DescriptionChange:
    """An object's description text differs between A and B."""

    text_a: str
    text_b: str

    def unified_diff(self, n: int = 3) -> str:
        """``difflib.unified_diff`` joined into a single string. ``n`` is
        the number of context lines."""
        return "".join(
            difflib.unified_diff(
                self.text_a.splitlines(keepends=True),
                self.text_b.splitlines(keepends=True),
                fromfile="a",
                tofile="b",
                n=n,
            )
        )


@dataclass(frozen=True, slots=True)
class LinkChange:
    """A link present on exactly one side. Identity = (target_doc,
    target_id, type). ``ack`` is the value on the side where the link
    exists."""

    target_doc: str
    target_id: str
    type: str
    ack: str | None


@dataclass(frozen=True, slots=True)
class LinkAckChange:
    """Same link on both sides, but ack metadata differs (review activity).

    Tracked separately from added/removed so reviewer sign-off doesn't
    masquerade as the requirement being re-linked.
    """

    target_doc: str
    target_id: str
    type: str
    ack_a: str | None
    ack_b: str | None


@dataclass(frozen=True, slots=True)
class LinksDelta:
    added: tuple[LinkChange, ...] = ()
    removed: tuple[LinkChange, ...] = ()
    ack_changed: tuple[LinkAckChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.ack_changed)


@dataclass(frozen=True, slots=True)
class ObjectDelta:
    """Aggregate per-object change record. Future deltas (e.g.
    outline_position) slot in here as additional optional fields without
    breaking callers."""

    obj_id: str
    attrs: tuple[AttributeChange, ...] = ()
    description: DescriptionChange | None = None
    links: LinksDelta = field(default_factory=LinksDelta)

    @property
    def is_empty(self) -> bool:
        return (
            not self.attrs
            and self.description is None
            and self.links.is_empty
        )


@dataclass(frozen=True, slots=True)
class DocumentDiff:
    doc_id: str
    ref_a: str
    ref_b: str
    short_a: str
    short_b: str
    added_objects: tuple[str, ...]
    removed_objects: tuple[str, ...]
    modified_objects: tuple[ObjectDelta, ...]

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_objects
            or self.removed_objects
            or self.modified_objects
        )


# --- public entry point ---------------------------------------------------


def compare_document(
    repo_root: str | Path,
    doc_id: str,
    ref_a: str,
    ref_b: str,
) -> DocumentDiff:
    """Return a structured diff of ``doc_id`` between ``ref_a`` and ``ref_b``.

    Refs may be commit hashes (short or full), branch names, or any tag —
    notably ``<doc_id>-issue-<n>`` issue tags. Argument order is "from A to
    B": objects present at B but not A appear in ``added_objects``.

    Raises ``GitError`` if either ref can't be resolved, or if the document
    folder is absent at both refs.
    """
    repo, _ = discover_repo(repo_root)
    short_a = _short(repo, ref_a)
    short_b = _short(repo, ref_b)

    ids_a = _object_ids_at(repo, doc_id, ref_a)
    ids_b = _object_ids_at(repo, doc_id, ref_b)
    if not ids_a and not ids_b:
        if not _doc_present(repo, doc_id, ref_a) and not _doc_present(
            repo, doc_id, ref_b
        ):
            raise GitError(
                f"document {doc_id!r} not present at {ref_a} or {ref_b}"
            )

    added = tuple(sorted(ids_b - ids_a))
    removed = tuple(sorted(ids_a - ids_b))
    common = sorted(ids_a & ids_b)

    modified: list[ObjectDelta] = []
    for obj_id in common:
        attrs_a, desc_a = _read_object(repo, doc_id, obj_id, ref_a)
        attrs_b, desc_b = _read_object(repo, doc_id, obj_id, ref_b)
        links_a = _read_links(repo, doc_id, obj_id, ref_a)
        links_b = _read_links(repo, doc_id, obj_id, ref_b)

        attr_changes = _diff_attrs(attrs_a, attrs_b)
        desc_change = (
            DescriptionChange(text_a=desc_a, text_b=desc_b)
            if desc_a != desc_b
            else None
        )
        link_delta = _diff_links(links_a, links_b)

        delta = ObjectDelta(
            obj_id=obj_id,
            attrs=attr_changes,
            description=desc_change,
            links=link_delta,
        )
        if not delta.is_empty:
            modified.append(delta)

    return DocumentDiff(
        doc_id=doc_id,
        ref_a=ref_a,
        ref_b=ref_b,
        short_a=short_a,
        short_b=short_b,
        added_objects=added,
        removed_objects=removed,
        modified_objects=tuple(modified),
    )


# --- internals ------------------------------------------------------------


def _short(repo: pygit2.Repository, ref: str) -> str:
    try:
        commit = repo.revparse_single(ref).peel(pygit2.Commit)
    except (pygit2.GitError, KeyError) as e:
        raise GitError(f"cannot resolve {ref!r}: {e}") from e
    return str(commit.id)[:SHORT_HASH_LEN]


def _doc_present(repo: pygit2.Repository, doc_id: str, ref: str) -> bool:
    try:
        commit = repo.revparse_single(ref).peel(pygit2.Commit)
    except (pygit2.GitError, KeyError) as e:
        raise GitError(f"cannot resolve {ref!r}: {e}") from e
    try:
        commit.tree[doc_id]
    except KeyError:
        return False
    return True


def _object_ids_at(
    repo: pygit2.Repository, doc_id: str, ref: str
) -> set[str]:
    names = list_tree_filenames(repo, f"{doc_id}/objects", ref)
    return {n[:-4] for n in names if n.endswith(".xml")}


def _read_object(
    repo: pygit2.Repository, doc_id: str, obj_id: str, ref: str
) -> tuple[dict[str, str], str]:
    rel = f"{doc_id}/objects/{obj_id}.xml"
    text = show_blob_at(repo, rel, ref)
    return _parse_object_xml(text)


def _read_links(
    repo: pygit2.Repository, doc_id: str, obj_id: str, ref: str
) -> tuple[Link, ...]:
    rel = f"{doc_id}/links/{obj_id}.xml"
    try:
        text = show_blob_at(repo, rel, ref)
    except GitError:
        return ()
    return _parse_links_xml(text)


def _parse_object_xml(text: str) -> tuple[dict[str, str], str]:
    """Lenient ``<object>`` blob parser. Returns ``(attrs, description)``.

    Skips malformed ``<attr>`` entries silently — diffing historical blobs
    benefits more from a partial result than from raising on a single bad
    record."""
    try:
        root = etree.fromstring(text.encode("utf-8"))
    except etree.XMLSyntaxError:
        return {}, ""
    if root.tag != "object":
        return {}, ""
    attrs: dict[str, str] = {}
    for el in root.findall("attr"):
        name = el.get("name")
        if not name:
            continue
        attrs[name] = (el.text or "").strip()
    desc_el = root.find("description")
    desc = (desc_el.text or "").strip() if desc_el is not None else ""
    return attrs, desc


def _parse_links_xml(text: str) -> tuple[Link, ...]:
    """Lenient ``<links>`` blob parser. Mirrors the historical-blob parser
    used in gui/controller.py — the loader's strict version raises on bad
    files, but a diff over historical blobs is better off skipping."""
    try:
        root = etree.fromstring(text.encode("utf-8"))
    except etree.XMLSyntaxError:
        return ()
    if root.tag != "links":
        return ()
    out: list[Link] = []
    for el in root.findall("link"):
        target_doc = el.get("target-doc") or ""
        target_id = el.get("target-id") or ""
        link_type = el.get("type") or ""
        ack = el.get("ack")
        if not target_doc or not target_id or not link_type:
            continue
        out.append(
            Link(
                target_doc=target_doc,
                target_id=target_id,
                type=link_type,
                ack=ack,
            )
        )
    return tuple(out)


def _diff_attrs(
    a: dict[str, str], b: dict[str, str]
) -> tuple[AttributeChange, ...]:
    out: list[AttributeChange] = []
    for name in sorted(set(a) | set(b)):
        val_a = a.get(name)
        val_b = b.get(name)
        if val_a == val_b:
            continue
        out.append(AttributeChange(name=name, value_a=val_a, value_b=val_b))
    return tuple(out)


def _diff_links(
    a: tuple[Link, ...], b: tuple[Link, ...]
) -> LinksDelta:
    """Diff outgoing-link sets keyed by (target_doc, target_id, type).

    Same-key links with different ack go into ``ack_changed``, not
    added/removed."""
    by_key_a: dict[tuple[str, str, str], Link] = {
        (l.target_doc, l.target_id, l.type): l for l in a
    }
    by_key_b: dict[tuple[str, str, str], Link] = {
        (l.target_doc, l.target_id, l.type): l for l in b
    }
    added: list[LinkChange] = []
    removed: list[LinkChange] = []
    ack_changed: list[LinkAckChange] = []
    for key in sorted(by_key_b.keys() - by_key_a.keys()):
        link = by_key_b[key]
        added.append(LinkChange(*key, ack=link.ack))
    for key in sorted(by_key_a.keys() - by_key_b.keys()):
        link = by_key_a[key]
        removed.append(LinkChange(*key, ack=link.ack))
    for key in sorted(by_key_a.keys() & by_key_b.keys()):
        la, lb = by_key_a[key], by_key_b[key]
        if la.ack != lb.ack:
            ack_changed.append(
                LinkAckChange(*key, ack_a=la.ack, ack_b=lb.ack)
            )
    return LinksDelta(
        added=tuple(added),
        removed=tuple(removed),
        ack_changed=tuple(ack_changed),
    )
