"""Structured diff between two versions of the same document.

Library entry point: ``compare_document(repo_root, doc_id, ref_a, ref_b)``.
A version is any git ref (a commit hash, a branch, a tag — notably the
``<doc_id>-issue-<n>`` issue tags) or ``WORKING_COPY``, the files on disk
with any uncommitted edits.

This is the V-model "what changed between Issue 1 and Issue 2" question,
behind the GUI's Compare dialog and the MCP ``compare_document`` tool. The
result says, per object, whether it was added, deleted, modified, moved in
the outline, or only had a suspect link reviewed (``Change``), and holds the
object's fields at both versions so callers can show them.

Objects are matched by id (DOORS' Absolute Number), so reordering the
outline doesn't make objects look added or deleted. Nothing is written:
history is read from git trees and the working copy from disk, unlike DOORS
Classic's Module Comparison Wizard, which creates a link module.

Files are compared by content first (git blob ids, or the same hash of the
bytes on disk), so only objects whose files differ are parsed. A file that
can't be parsed doesn't stop the comparison: the object is reported with an
``error`` at that version instead of being read as empty.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pygit2

from .git_ops import (
    SHORT_HASH_LEN,
    GitError,
    discover_repo,
    issue_tags,
    log_for_path,
)
from .loader import (
    LoaderError,
    parse_links_bytes,
    parse_manifest_bytes,
    parse_object_bytes,
)
from .schema import (
    Link,
    OutlineNode,
    RequirementObject,
    Table,
    attribute_display_name,
)

# The version that stands for the files on disk. Not a valid git ref name
# (it contains a space), so it can't shadow a branch or tag.
WORKING_COPY = "working copy"
# The version that stands for the latest commit.
LATEST = "HEAD"


class Change(StrEnum):
    """How an object changed between two versions. An object can have more
    than one: Modified and Moved, or Link reviewed and Moved."""

    ADDED = "Added"
    DELETED = "Deleted"
    MODIFIED = "Modified"
    MOVED = "Moved"
    LINK_REVIEWED = "Link reviewed"


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

    def words(self) -> tuple[tuple[str, str], ...]:
        """The change word by word; see `word_diff`."""
        return word_diff(self.text_a, self.text_b)


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
class TableChange:
    """An object's table differs; either side is None when it has none."""

    table_a: Table | None
    table_b: Table | None


@dataclass(frozen=True, slots=True)
class FigureChange:
    """An object's figure (``figures/<id>.svg``) differs; the SVG bytes at
    each side, None when it has none."""

    svg_a: bytes | None
    svg_b: bytes | None


@dataclass(frozen=True, slots=True)
class ObjectDelta:
    """The field changes of an object present at both versions."""

    obj_id: str
    attrs: tuple[AttributeChange, ...] = ()
    description: DescriptionChange | None = None
    links: LinksDelta = field(default_factory=LinksDelta)
    table: TableChange | None = None
    figure: FigureChange | None = None

    @property
    def is_empty(self) -> bool:
        return (
            not self.attrs
            and self.description is None
            and self.links.is_empty
            and self.table is None
            and self.figure is None
        )

    @property
    def only_link_reviews(self) -> bool:
        """True when the only change is to link acknowledgements."""
        return (
            bool(self.links.ack_changed)
            and not self.links.added
            and not self.links.removed
            and not self.attrs
            and self.description is None
            and self.table is None
            and self.figure is None
        )


@dataclass(frozen=True, slots=True)
class Position:
    """Where an object sits in the outline: under ``parent_id`` (None at the
    top level), after the sibling ``after_id`` (None when it comes first).
    ``in_outline`` is False for an object the outline doesn't list."""

    parent_id: str | None
    after_id: str | None
    in_outline: bool = True


@dataclass(frozen=True, slots=True)
class ObjectState:
    """An object's fields at one version.

    ``obj`` is None when the object's file couldn't be read there; ``error``
    then says why. ``links_error`` says why its links file couldn't be read
    (``links`` is then empty and not compared). ``position`` is None when the
    version's outline couldn't be read.
    """

    obj: RequirementObject | None
    links: tuple[Link, ...] = ()
    figure: bytes | None = None
    position: Position | None = None
    error: str | None = None
    links_error: str | None = None

    @property
    def problem(self) -> str | None:
        """Why something of this object couldn't be read, if anything."""
        if self.error and self.links_error:
            return f"{self.error}; {self.links_error}"
        return self.error or self.links_error


@dataclass(frozen=True, slots=True)
class ObjectChange:
    """One changed object: how it changed and its state at each version.

    ``before`` is None for an added object and ``after`` for a deleted one.
    ``delta`` holds the field changes of an object at both versions (None
    when added, deleted, or when one side couldn't be read).
    """

    obj_id: str
    kinds: tuple[Change, ...]
    before: ObjectState | None
    after: ObjectState | None
    delta: ObjectDelta | None = None

    @property
    def text(self) -> str:
        """The first line of the object's text: at B, or at A when deleted."""
        for state in (self.after, self.before):
            if state is not None and state.obj is not None:
                lines = state.obj.description.strip().splitlines()
                return lines[0].strip() if lines else ""
        return ""

    @property
    def errors(self) -> tuple[str | None, str | None]:
        """Why the object (or its links) couldn't be read at A and at B
        (None when it could)."""
        return (
            self.before.problem if self.before is not None else None,
            self.after.problem if self.after is not None else None,
        )


@dataclass(frozen=True, slots=True)
class DocumentDiff:
    """The difference between two versions of one document.

    ``changes`` lists every changed object in display order: the outline at
    B, with each deleted object placed after the object that preceded it at
    A. ``added_objects``, ``removed_objects`` and ``modified_objects`` are
    the older id-sorted views: ``modified_objects`` holds every object at
    both versions with field changes, link reviews included.

    ``labels`` maps attribute names to the labels declared at B (or A).
    ``present_a`` / ``present_b`` say whether the document exists at each
    version. ``outline_error_a`` / ``outline_error_b`` say why a version's
    manifest couldn't be read; moves are then not detected, and without the
    outline at B, rows fall back to id order.
    """

    doc_id: str
    ref_a: str
    ref_b: str
    short_a: str
    short_b: str
    added_objects: tuple[str, ...]
    removed_objects: tuple[str, ...]
    modified_objects: tuple[ObjectDelta, ...]
    changes: tuple[ObjectChange, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    present_a: bool = True
    present_b: bool = True
    outline_error_a: str | None = None
    outline_error_b: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.changes
            or self.added_objects
            or self.removed_objects
            or self.modified_objects
        )

    def counts(self) -> dict[Change, int]:
        """How many objects have each kind of change (an object that was
        modified and moved counts in both)."""
        out = {kind: 0 for kind in Change}
        for change in self.changes:
            for kind in change.kinds:
                out[kind] += 1
        return out

    def label(self, attr_name: str) -> str:
        return self.labels.get(attr_name) or attribute_display_name(attr_name, None)


# --- word diff ------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]")


def word_diff(a: str, b: str) -> tuple[tuple[str, str], ...]:
    """Segments turning ``a`` into ``b``, word by word.

    Each segment is ``(op, text)`` with op ``"equal"``, ``"delete"`` (text
    only in ``a``) or ``"insert"`` (text only in ``b``). Joining the equal
    and delete segments gives ``a``; the equal and insert ones give ``b``.
    """
    ta = _TOKEN_RE.findall(a)
    tb = _TOKEN_RE.findall(b)
    out: list[tuple[str, str]] = []

    def add(op: str, text: str) -> None:
        if not text:
            return
        if out and out[-1][0] == op:
            out[-1] = (op, out[-1][1] + text)
        else:
            out.append((op, text))

    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            add("equal", "".join(ta[i1:i2]))
        else:
            add("delete", "".join(ta[i1:i2]))
            add("insert", "".join(tb[j1:j2]))
    return tuple(out)


# --- versions -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Version:
    """A version a document can be compared at, for pickers.

    ``ref`` is what to pass to `compare_document`; ``kind`` is one of
    ``"working-copy"``, ``"latest"``, ``"issue"``, ``"branch"``, ``"commit"``.
    """

    ref: str
    label: str
    kind: str


def document_versions(
    repo_root: str | Path, doc_id: str, *, max_commits: int = 200
) -> list[Version]:
    """The versions to offer for `doc_id`: the working copy, the latest
    commit, every issue (highest first), every branch, and the newest
    `max_commits` commits on HEAD's history that changed the document.

    Raises ``GitError`` when `repo_root` isn't in a git repo.
    """
    repo, _ = discover_repo(repo_root)
    out = [Version(WORKING_COPY, "Working copy (uncommitted edits)", "working-copy")]
    if repo.head_is_unborn:
        return out
    head = repo.head.peel(pygit2.Commit)
    out.append(Version(LATEST, f"Latest commit ({_short_oid(head.id)})", "latest"))
    for issue, tag, oid in issue_tags(repo, doc_id):
        out.append(Version(tag, f"Issue {issue} ({_short_oid(oid)})", "issue"))
    for name in sorted(repo.branches.local):
        out.append(Version(name, f"Branch {name}", "branch"))
    for name in sorted(repo.branches.remote):
        if name.endswith("/HEAD"):
            continue
        out.append(Version(name, f"Branch {name}", "branch"))
    for entry in log_for_path(repo, doc_id, max_entries=max_commits):
        message = entry["message"]
        if len(message) > 60:
            message = message[:59] + "…"
        out.append(
            Version(entry["commit_oid"], f"{entry['short_hash']}  {message}", "commit")
        )
    return out


def default_versions(repo_root: str | Path, doc_id: str) -> tuple[str | None, str]:
    """The versions the Compare dialog opens with: ``(from_ref, to_ref)``.

    To is the latest commit. From is the latest issue reachable from HEAD;
    when the document hasn't changed since that issue, it's the issue before
    it instead, so the two issues are compared. From is None when there is
    no such issue: the user must choose. In a repo with no commit yet, To is
    the working copy.

    Raises ``GitError`` when `repo_root` isn't in a git repo.
    """
    repo, _ = discover_repo(repo_root)
    if repo.head_is_unborn:
        return None, WORKING_COPY
    issues = issue_tags(repo, doc_id, reachable_only=True)
    if not issues:
        return None, LATEST
    head_tree = _doc_tree_id(repo.head.peel(pygit2.Commit), doc_id)
    _issue, tag, oid = issues[0]
    if _doc_tree_id(repo[oid].peel(pygit2.Commit), doc_id) == head_tree:
        return (issues[1][1] if len(issues) > 1 else None), LATEST
    return tag, LATEST


def _doc_tree_id(commit: pygit2.Commit, doc_id: str) -> pygit2.Oid | None:
    try:
        return commit.tree[doc_id].id
    except KeyError:
        return None


def _short_oid(oid: pygit2.Oid) -> str:
    return str(oid)[:SHORT_HASH_LEN]


# --- reading one version --------------------------------------------------


class _Side:
    """The document's files at one version, compared by content key and
    read only when needed. Files are indexed by stem, matching the suffix
    without regard to case as the loader does on Windows; `names` keeps each
    file's real name."""

    present: bool
    short: str
    # stem -> content key (a blob id), for objects/*.xml, links/*.xml, figures/*.svg
    objects: dict[str, object]
    links: dict[str, object]
    figures: dict[str, object]
    # (sub, stem) -> the file's name in that folder
    names: dict[tuple[str, str], str]
    manifest: bytes | None

    def read(self, sub: str, stem: str) -> bytes:
        raise NotImplementedError

    def path(self, doc_id: str, sub: str, stem: str) -> str:
        return f"{doc_id}/{sub}/{self.names[(sub, stem)]}"


_SUBFOLDERS = (("objects", ".xml"), ("links", ".xml"), ("figures", ".svg"))


class _GitSide(_Side):
    def __init__(self, repo: pygit2.Repository, doc_id: str, ref: str) -> None:
        try:
            commit = repo.revparse_single(ref).peel(pygit2.Commit)
        except (pygit2.GitError, KeyError, ValueError) as e:
            raise GitError(f"cannot resolve {ref!r}: {e}") from e
        self._repo = repo
        self.short = _short_oid(commit.id)
        self.objects, self.links, self.figures = {}, {}, {}
        self.names = {}
        self.manifest = None
        self._ids: dict[tuple[str, str], pygit2.Oid] = {}
        try:
            tree = repo[commit.tree[doc_id].id]
        except KeyError:
            tree = None
        self.present = isinstance(tree, pygit2.Tree)
        if not self.present:
            return
        try:
            manifest = tree[f"{doc_id}.xml"]
        except KeyError:
            manifest = None
        if manifest is not None and manifest.filemode != pygit2.GIT_FILEMODE_TREE:
            self.manifest = repo[manifest.id].data
        targets = {"objects": self.objects, "links": self.links, "figures": self.figures}
        for sub, suffix in _SUBFOLDERS:
            try:
                subtree = repo[tree[sub].id]
            except KeyError:
                continue
            if not isinstance(subtree, pygit2.Tree):
                continue
            for entry in subtree:
                if entry.filemode == pygit2.GIT_FILEMODE_TREE:
                    continue
                if entry.name.lower().endswith(suffix):
                    stem = entry.name[: -len(suffix)]
                    targets[sub][stem] = entry.id
                    self.names[(sub, stem)] = entry.name
                    self._ids[(sub, stem)] = entry.id

    def read(self, sub: str, stem: str) -> bytes:
        return self._repo[self._ids[(sub, stem)]].data


class _DiskSide(_Side):
    """The working copy. Every file is read (there is no index of what
    changed), and keyed by its git blob hash so it compares with history."""

    def __init__(self, folder: Path, doc_id: str) -> None:
        self.short = WORKING_COPY
        self.objects, self.links, self.figures = {}, {}, {}
        self.names = {}
        self.manifest = None
        self._data: dict[tuple[str, str], bytes | OSError] = {}
        # As in git: the document is there when its folder is, manifest or not.
        self.present = folder.is_dir()
        if not self.present:
            return
        try:
            self.manifest = (folder / f"{doc_id}.xml").read_bytes()
        except OSError:
            self.manifest = None
        targets = {"objects": self.objects, "links": self.links, "figures": self.figures}
        for sub, suffix in _SUBFOLDERS:
            try:
                entries = list(os.scandir(folder / sub))
            except OSError:
                continue
            for entry in entries:
                if not os.path.normcase(entry.name).endswith(suffix):
                    continue
                stem = entry.name[: -len(suffix)]
                try:
                    if not entry.is_file():
                        continue
                    with open(entry.path, "rb") as f:
                        data: bytes | OSError = f.read()
                    key: object = pygit2.hash(data)
                except OSError as e:
                    data, key = e, ("unreadable", sub, entry.name)
                self._data[(sub, stem)] = data
                self.names[(sub, stem)] = entry.name
                targets[sub][stem] = key

    def read(self, sub: str, stem: str) -> bytes:
        data = self._data[(sub, stem)]
        if isinstance(data, OSError):
            raise data
        return data


def _open_side(
    repo: pygit2.Repository, root: Path, doc_id: str, ref: str
) -> _Side:
    if ref == WORKING_COPY:
        return _DiskSide(root / doc_id, doc_id)
    return _GitSide(repo, doc_id, ref)


class _Reader:
    """Parses one side's files on demand, remembering each result."""

    def __init__(self, side: _Side, doc_id: str) -> None:
        self.side = side
        self.doc_id = doc_id
        self._objects: dict[str, tuple[RequirementObject | None, str | None]] = {}
        self._links: dict[str, tuple[tuple[Link, ...], str | None]] = {}

    def obj(self, obj_id: str) -> tuple[RequirementObject | None, str | None]:
        hit = self._objects.get(obj_id)
        if hit is None:
            path = self.side.path(self.doc_id, "objects", obj_id)
            try:
                hit = (parse_object_bytes(self.side.read("objects", obj_id), path), None)
            except LoaderError as e:
                hit = (None, str(e))
            except OSError as e:
                hit = (None, f"{path}: cannot read file: {e}")
            self._objects[obj_id] = hit
        return hit

    def links(self, obj_id: str) -> tuple[tuple[Link, ...], str | None]:
        hit = self._links.get(obj_id)
        if hit is None:
            if obj_id not in self.side.links:
                hit = ((), None)
            else:
                path = self.side.path(self.doc_id, "links", obj_id)
                try:
                    _source, links = parse_links_bytes(self.side.read("links", obj_id), path)
                    hit = (links, None)
                except LoaderError as e:
                    hit = ((), str(e))
                except OSError as e:
                    hit = ((), f"{path}: cannot read file: {e}")
            self._links[obj_id] = hit
        return hit

    def figure(self, obj_id: str) -> bytes | None:
        if obj_id not in self.side.figures:
            return None
        try:
            return self.side.read("figures", obj_id)
        except OSError:
            return None

    def state(self, obj_id: str, position: Position | None) -> ObjectState:
        obj, error = self.obj(obj_id)
        links, links_error = self.links(obj_id)
        return ObjectState(
            obj=obj,
            links=links,
            figure=self.figure(obj_id),
            position=position,
            error=error,
            links_error=links_error,
        )


# --- outline --------------------------------------------------------------


@dataclass
class _Outline:
    """An outline flattened for lookups: pre-order ids, each id's parent and
    the children of each parent (None for the top level)."""

    preorder: list[str]
    parent: dict[str, str | None]
    children: dict[str | None, list[str]]

    @classmethod
    def build(cls, nodes: tuple[OutlineNode, ...]) -> _Outline:
        out = cls([], {}, {None: []})

        def walk(level: tuple[OutlineNode, ...], parent: str | None) -> None:
            for node in level:
                if node.ref_id in out.parent:
                    continue  # a duplicate ref; the validator reports it
                out.preorder.append(node.ref_id)
                out.parent[node.ref_id] = parent
                out.children.setdefault(parent, []).append(node.ref_id)
                walk(node.children, node.ref_id)

        walk(nodes, None)
        return out

    def position(self, obj_id: str) -> Position:
        if obj_id not in self.parent:
            return Position(None, None, in_outline=False)
        parent = self.parent[obj_id]
        siblings = self.children[parent]
        i = siblings.index(obj_id)
        return Position(parent, siblings[i - 1] if i else None)


def _read_manifest(
    side: _Side, doc_id: str
) -> tuple[_Outline | None, dict[str, str], str | None]:
    """The side's outline and attribute labels, and why the outline can't
    be read (None when it can, or when the document isn't there)."""
    if not side.present:
        return None, {}, None
    path = f"{doc_id}/{doc_id}.xml"
    if side.manifest is None:
        return None, {}, f"{path} is missing"
    try:
        _meta, _parents, attributes, outline = parse_manifest_bytes(side.manifest, path)
    except LoaderError as e:
        return None, {}, str(e)
    labels = {a.name: attribute_display_name(a.name, a) for a in attributes}
    return _Outline.build(outline), labels, None


def _moved(common: set[str], a: _Outline, b: _Outline) -> set[str]:
    """Objects at both versions whose parent changed, or whose order among
    the siblings they kept changed. Added and deleted siblings don't count:
    inserting an object before others doesn't move them."""
    moved: set[str] = set()
    for obj_id in common:
        if (obj_id in a.parent) != (obj_id in b.parent):
            moved.add(obj_id)
        elif obj_id in a.parent and a.parent[obj_id] != b.parent[obj_id]:
            moved.add(obj_id)
    for parent, kids_b in b.children.items():
        kept_b = [k for k in kids_b if k in common and k not in moved]
        if len(kept_b) < 2:
            continue
        kept = set(kept_b)
        kept_a = [k for k in a.children.get(parent, []) if k in kept]
        matcher = difflib.SequenceMatcher(None, kept_a, kept_b, autojunk=False)
        in_place = {
            kept_b[j]
            for block in matcher.get_matching_blocks()
            for j in range(block.b, block.b + block.size)
        }
        moved |= kept - in_place
    return moved


def _id_sort_key(obj_id: str) -> tuple[int, str]:
    tail = obj_id.rsplit("-", 1)[-1]
    return (int(tail), obj_id) if tail.isdigit() else (-1, obj_id)


def _display_order(
    changed: set[str],
    deleted: set[str],
    outline_a: _Outline | None,
    outline_b: _Outline | None,
) -> list[str]:
    """Changed ids in the outline order at B, each deleted object placed
    after the object that preceded it at A. Objects neither outline places
    come last, by id."""
    order: list[str] = list(outline_b.preorder) if outline_b is not None else []
    placed = set(order)
    if outline_a is not None:
        # Insert deleted objects (in A order) after their nearest preceding
        # object at A that is already placed.
        after: dict[str | None, list[str]] = {}
        anchor: str | None = None
        for obj_id in outline_a.preorder:
            if obj_id in deleted and obj_id not in placed:
                after.setdefault(anchor, []).append(obj_id)
            elif obj_id in placed:
                anchor = obj_id
        merged = list(after.get(None, []))
        for obj_id in order:
            merged.append(obj_id)
            merged.extend(after.get(obj_id, []))
        order = merged
        placed = set(order)
    rest = sorted((c for c in changed if c not in placed), key=_id_sort_key)
    return [c for c in order if c in changed] + rest


# --- public entry point ---------------------------------------------------


def compare_document(
    repo_root: str | Path,
    doc_id: str,
    ref_a: str,
    ref_b: str,
) -> DocumentDiff:
    """Return a structured diff of ``doc_id`` between ``ref_a`` and ``ref_b``.

    Refs may be commit hashes (short or full), branch names, any tag —
    notably ``<doc_id>-issue-<n>`` issue tags — or ``WORKING_COPY``.
    Argument order is "from A to B": objects present at B but not A are
    added.

    Raises ``GitError`` if either ref can't be resolved, or if the document
    folder is absent at both refs.
    """
    repo, root = discover_repo(repo_root)
    side_a = _open_side(repo, root, doc_id, ref_a)
    side_b = _open_side(repo, root, doc_id, ref_b)
    if not side_a.present and not side_b.present:
        raise GitError(f"document {doc_id!r} not present at {ref_a} or {ref_b}")

    read_a, read_b = _Reader(side_a, doc_id), _Reader(side_b, doc_id)
    outline_a, labels_a, outline_error_a = _read_manifest(side_a, doc_id)
    outline_b, labels_b, outline_error_b = _read_manifest(side_b, doc_id)
    labels = {**labels_a, **labels_b}

    def pos(outline: _Outline | None, obj_id: str) -> Position | None:
        return outline.position(obj_id) if outline is not None else None

    ids_a, ids_b = set(side_a.objects), set(side_b.objects)
    added, deleted, common = ids_b - ids_a, ids_a - ids_b, ids_a & ids_b
    moved = (
        _moved(common, outline_a, outline_b)
        if outline_a is not None and outline_b is not None
        else set()
    )

    changes: dict[str, ObjectChange] = {}
    modified: list[ObjectDelta] = []
    for obj_id in added:
        changes[obj_id] = ObjectChange(
            obj_id, (Change.ADDED,), None, read_b.state(obj_id, pos(outline_b, obj_id))
        )
    for obj_id in deleted:
        changes[obj_id] = ObjectChange(
            obj_id, (Change.DELETED,), read_a.state(obj_id, pos(outline_a, obj_id)), None
        )
    for obj_id in common:
        same_files = (
            side_a.objects[obj_id] == side_b.objects[obj_id]
            and side_a.links.get(obj_id) == side_b.links.get(obj_id)
            and side_a.figures.get(obj_id) == side_b.figures.get(obj_id)
        )
        if same_files and obj_id not in moved:
            continue
        before = read_a.state(obj_id, pos(outline_a, obj_id))
        after = read_b.state(obj_id, pos(outline_b, obj_id))
        kinds: list[Change] = []
        delta = None
        if not same_files:
            # The files differ, so an object that can't be read on one side
            # is reported as modified rather than silently passed over.
            if before.problem or after.problem:
                kinds.append(Change.MODIFIED)
            delta = _delta(obj_id, before, after)
            if delta.is_empty:
                delta = None
            else:
                modified.append(delta)
                if Change.MODIFIED not in kinds:
                    kinds.append(
                        Change.LINK_REVIEWED if delta.only_link_reviews else Change.MODIFIED
                    )
        if obj_id in moved:
            kinds.append(Change.MOVED)
        if kinds:
            changes[obj_id] = ObjectChange(obj_id, tuple(kinds), before, after, delta)

    order = _display_order(set(changes), deleted, outline_a, outline_b)
    return DocumentDiff(
        doc_id=doc_id,
        ref_a=ref_a,
        ref_b=ref_b,
        short_a=side_a.short,
        short_b=side_b.short,
        added_objects=tuple(sorted(added)),
        removed_objects=tuple(sorted(deleted)),
        modified_objects=tuple(sorted(modified, key=lambda d: d.obj_id)),
        changes=tuple(changes[c] for c in order),
        labels=labels,
        present_a=side_a.present,
        present_b=side_b.present,
        outline_error_a=outline_error_a,
        outline_error_b=outline_error_b,
    )


# --- internals ------------------------------------------------------------


def _delta(obj_id: str, before: ObjectState, after: ObjectState) -> ObjectDelta:
    """Field changes between two states; fields that couldn't be read on
    either side are left out."""
    attrs: tuple[AttributeChange, ...] = ()
    description = None
    table = None
    if before.obj is not None and after.obj is not None:
        attrs = _diff_attrs(before.obj.attrs, after.obj.attrs)
        if before.obj.description != after.obj.description:
            description = DescriptionChange(before.obj.description, after.obj.description)
        if before.obj.table != after.obj.table:
            table = TableChange(before.obj.table, after.obj.table)
    figure = (
        FigureChange(before.figure, after.figure)
        if before.figure != after.figure
        else None
    )
    # A links file that can't be read isn't "no links": comparing it would
    # show every link as added or removed.
    links = (
        LinksDelta()
        if before.links_error or after.links_error
        else _diff_links(before.links, after.links)
    )
    return ObjectDelta(
        obj_id=obj_id,
        attrs=attrs,
        description=description,
        links=links,
        table=table,
        figure=figure,
    )


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
