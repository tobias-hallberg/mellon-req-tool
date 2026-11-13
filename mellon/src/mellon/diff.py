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
``error`` at that version instead of being read as empty. Files whose bytes
differ but whose canonical forms are the same are no change: object, links
and manifest files are compared by what they parse to, figures by their
canonical XML (`_figure_key`).
"""

from __future__ import annotations

import bisect
import difflib
import os
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

import pygit2
from lxml import etree

from .git_ops import (
    SHORT_HASH_LEN,
    GitError,
    discover_repo,
    issue_tags,
    log_for_path,
)
from .loader import (
    LoaderError,
    _has_suffix,  # the loader's own rule, so a compare sees the files it loads
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
    whole_number,
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

    # Requirements: MELLON-VER-SRS-24
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
        # MELLON-VER-SRS-32: the description at To, or at From for a deleted
        # object (also used when the object can't be read at To).
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

    ``commit_a`` / ``commit_b`` is the full hash of the commit each version
    resolved to, None for the working copy; ``short_a`` / ``short_b`` its
    first 7 digits, or ``WORKING_COPY``.

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
    commit_a: str | None = None
    commit_b: str | None = None

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
        # MELLON-VER-SRS-33: counted over every change, whatever a view hides.
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

    # MELLON-VER-SRS-47: equal segments are the same tokens at both, so every
    # word that differs lands in a delete (From) or insert (To) segment.
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


# Requirements: MELLON-VER-SRS-62
def document_versions(
    repo_root: str | Path, doc_id: str, *, max_commits: int = 200
) -> list[Version]:
    """The versions to offer for `doc_id`: the working copy, the latest
    commit, every issue (highest first), every branch, and the newest
    `max_commits` commits on HEAD's history that changed the document.

    Raises ``GitError`` when `repo_root` isn't in a git repo.
    """
    repo, _ = discover_repo(repo_root)
    # MELLON-VER-SRS-8: the working copy is offered, with or without commits.
    out = [Version(WORKING_COPY, "Working copy (uncommitted edits)", "working-copy")]
    if repo.head_is_unborn:
        return out
    # MELLON-VER-SRS-7: the latest commit, each issue of the document, and
    # each local and remote-tracking branch.
    head = repo.head.peel(pygit2.Commit)
    out.append(Version(LATEST, f"Latest commit ({_short_oid(head.id)})", "latest"))
    for issue, tag, oid in issue_tags(repo, doc_id):
        out.append(Version(tag, f"Issue {issue} ({_short_oid(oid)})", "issue"))
    # MELLON-VER-SRS-7: each branch by its full ref name, which names the
    # branch even when a tag or another ref has its short name.
    for name in sorted(repo.branches.local):
        out.append(Version(f"refs/heads/{name}", f"Branch {name}", "branch"))
    for name in sorted(repo.branches.remote):
        if name.endswith("/HEAD"):
            continue
        out.append(Version(f"refs/remotes/{name}", f"Branch {name}", "branch"))
    # MELLON-VER-SRS-7, -62: the commits on HEAD's history whose document
    # folder differs from each parent's, and no others; only the newest
    # `max_commits` of them.
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
    # MELLON-VER-SRS-15: no commit yet: From unset, To the working copy.
    if repo.head_is_unborn:
        return None, WORKING_COPY
    # Highest issue first, only those whose commit is HEAD or an ancestor:
    # issues[0] is the current issue, issues[1] the issue before it.
    issues = issue_tags(repo, doc_id, reachable_only=True)
    # MELLON-VER-SRS-14: no issue: From unset, To the latest commit.
    if not issues:
        return None, LATEST
    # The document is unchanged since an issue when its folder's tree is the
    # same at the issue's commit and at HEAD.
    head_tree = _doc_tree_id(repo.head.peel(pygit2.Commit), doc_id)
    _issue, tag, oid = issues[0]
    if _doc_tree_id(repo[oid].peel(pygit2.Commit), doc_id) == head_tree:
        # MELLON-VER-SRS-13: unchanged since the current issue: From is the
        # issue before; MELLON-VER-SRS-14: unset when there is none.
        return (issues[1][1] if len(issues) > 1 else None), LATEST
    # MELLON-VER-SRS-12: changed since the current issue: From is that issue.
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
    # The full hash of the commit the version resolved to; None on disk.
    commit: str | None
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


# MELLON-VER-SRS-9: git reads an @ that stands alone as HEAD, as in @~2,
# @^{commit} or @@{1}. libgit2 knows only @ by itself and @{...}. A ref can't
# be named @, so nothing else is meant.
_LONE_AT = re.compile(r"@(?=[~^:]|@\{)")

# Where git looks for a name, in its order (git's ref_rev_parse_rules).
_REF_RULES = (
    "{}", "refs/{}", "refs/tags/{}", "refs/heads/{}", "refs/remotes/{}", "refs/remotes/{}/HEAD"
)
# The name a revision expression starts with ends at its first ~, ^, : or
# @{, none of which a ref name may hold.
_NAME_END = re.compile(r"[~^:]|@\{")
_HEX = re.compile(r"[0-9a-fA-F]{4,40}")


def _commit_hash(obj: pygit2.Object | pygit2.Reference) -> str | None:
    try:
        return str(obj.peel(pygit2.Commit).id)
    except (pygit2.GitError, KeyError, ValueError):
        return None


def _clashing_names(repo: pygit2.Repository, spec: str) -> list[str]:
    """What the name `spec` starts with names, when that is more than one
    commit, as git warns "ambiguous": each ref the name matches by git's
    rules, and the commit it is a hash or hash prefix of. Empty when the
    name names one commit, however many refs point there."""
    end = _NAME_END.search(spec)
    name = spec[: end.start()] if end else spec
    if not name:
        return []
    found: dict[str, str | None] = {}  # what names a commit -> its hash
    for rule in _REF_RULES:
        try:
            reference = repo.references.get(rule.format(name))
        except (ValueError, pygit2.GitError):
            continue  # not a valid ref name
        if reference is not None:
            commit = _commit_hash(reference)
            found[f"{reference.name} at {(commit or 'no commit')[:SHORT_HASH_LEN]}"] = commit
    if found and _HEX.fullmatch(name):
        try:
            obj = repo.get(name)
        except (ValueError, KeyError, pygit2.GitError):
            obj = None  # objects share the prefix: git takes the ref
        commit = _commit_hash(obj) if obj is not None else None
        if commit is not None:
            found[f"commit {commit[:SHORT_HASH_LEN]}"] = commit
    return list(found) if len(set(found.values())) > 1 else []


def _resolve_commit(repo: pygit2.Repository, ref: str) -> pygit2.Commit:
    """The commit git's revision syntax resolves `ref` to. Raises
    ``GitError`` naming `ref` when it resolves to nothing, to something
    other than a commit (a tree or a file), or to more than one commit."""
    spec = "HEAD" + ref[1:] if _LONE_AT.match(ref) else ref
    # MELLON-VER-SRS-10: a name that resolves to more than one commit is an
    # error naming the version as given, not one of the commits.
    clash = _clashing_names(repo, spec)
    if clash:
        raise GitError(
            f"cannot resolve {ref!r}: it names more than one commit ({', '.join(clash)})"
        )
    try:
        # MELLON-VER-SRS-9: git's revision syntax, peeled to the commit it names.
        return repo.revparse_single(spec).peel(pygit2.Commit)
    except (pygit2.GitError, KeyError, ValueError) as e:
        # MELLON-VER-SRS-10, -60: a version that resolves to no commit is an
        # error naming the version as given.
        raise GitError(f"cannot resolve {ref!r}: {e}") from e


class _GitSide(_Side):
    # Requirements: MELLON-VER-SRS-87
    def __init__(self, repo: pygit2.Repository, doc_id: str, ref: str) -> None:
        commit = _resolve_commit(repo, ref)
        self._repo = repo
        # MELLON-VER-SRS-67, -87: the commit this version resolved to.
        self.commit = str(commit.id)
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
                if _has_suffix(entry.name, suffix):
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
        self.commit = None
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
                if not _has_suffix(entry.name, suffix):
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
    # MELLON-VER-SRS-8: the working copy is the files on disk.
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

    # Requirements: MELLON-VER-SRS-27
    def obj(self, obj_id: str) -> tuple[RequirementObject | None, str | None]:
        hit = self._objects.get(obj_id)
        if hit is None:
            path = self.side.path(self.doc_id, "objects", obj_id)
            # MELLON-VER-SRS-28: a file that can't be read is this object's
            # error, kept with its state; the other objects are compared on.
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
    # MELLON-VER-SRS-29: a manifest that is missing or doesn't parse is
    # reported, and leaves no outline to find moves in.
    if side.manifest is None:
        return None, {}, f"{path} is missing"
    try:
        _meta, _parents, attributes, outline, _next = parse_manifest_bytes(
            side.manifest, path
        )
    except LoaderError as e:
        return None, {}, str(e)
    labels = {a.name: attribute_display_name(a.name, a) for a in attributes}
    return _Outline.build(outline), labels, None


def _moved(common: set[str], a: _Outline, b: _Outline) -> set[str]:
    """Objects at both versions whose parent changed, or whose order among
    the siblings they kept changed. Added and deleted siblings don't count:
    inserting an object before others doesn't move them."""
    moved: set[str] = set()
    # MELLON-VER-SRS-23: Moved when the parent changed (or the object is in
    # only one of the outlines), or when it isn't among the largest set of
    # same-parent siblings kept in order (`_in_place`).
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
        if kept_a == kept_b:
            continue
        moved |= kept - _in_place(kept_a, kept_b)
    return moved


def _in_place(kept_a: list[str], kept_b: list[str]) -> set[str]:
    """The siblings that didn't move: the same ids in From order (`kept_a`)
    and To order (`kept_b`).

    MELLON-VER-SRS-23: the largest set of siblings whose order is the same at
    From and at To. Of equally large sets, the one whose siblings come
    earliest at From, so the sibling that comes later at From is Moved.

    Siblings in the same order at both versions are an increasing run of
    their From positions read in To order, so this is the longest
    increasing subsequence, taken smallest first: O(n log n).
    """
    at_a = {k: i for i, k in enumerate(kept_a)}
    pos = [at_a[k] for k in kept_b]  # From positions, in To order
    n = len(pos)
    # longest[i]: the longest increasing run that starts at i. Read
    # backwards, a run that starts at i is an increasing run of -pos that
    # ends there.
    longest = [0] * n
    tails: list[int] = []
    for i in range(n - 1, -1, -1):
        at = bisect.bisect_left(tails, -pos[i])
        if at == len(tails):
            tails.append(-pos[i])
        else:
            tails[at] = -pos[i]
        longest[i] = at + 1
    # Starts of runs of each length, left to right. Their From positions
    # fall: a later start with a higher position would make a longer run.
    starts: dict[int, list[int]] = {}
    for i in range(n):
        starts.setdefault(longest[i], []).append(i)
    # The k-th sibling of the smallest longest run starts a run of exactly
    # the remaining length, and is the one with the lowest From position
    # above the previous sibling's: the last start with a higher position.
    keep: set[str] = set()
    last = -1
    for length in range(len(tails), 0, -1):
        group = starts[length]
        neg = [-pos[i] for i in group]  # rising
        chosen = group[bisect.bisect_left(neg, -last) - 1]
        keep.add(kept_b[chosen])
        last = pos[chosen]
    return keep


def _id_sort_key(obj_id: str) -> tuple[int, int, str]:
    # MELLON-VER-SRS-19, -20: any object ID sorts, so none stops the
    # comparison. A number is ASCII digits only, as in an object number; an
    # ID without one comes after those with one, by its text.
    number = whole_number(obj_id.rsplit("-", 1)[-1])
    return (0, number, obj_id) if number is not None else (1, 0, obj_id)


def _display_order(
    changed: set[str],
    deleted: set[str],
    outline_a: _Outline | None,
    outline_b: _Outline | None,
) -> list[str]:
    """Changed ids in the outline order at B, each deleted object placed
    after the object that preceded it at A. Objects neither outline places
    come last, by id."""
    # MELLON-VER-SRS-35: rows follow the outline at To, each object before
    # its sub-objects (pre-order).
    order: list[str] = list(outline_b.preorder) if outline_b is not None else []
    placed = set(order)
    if outline_a is not None:
        # MELLON-VER-SRS-36: insert deleted objects (in A order) after their
        # nearest preceding object at A that is already placed, or first.
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


# Requirements: MELLON-VER-SRS-20, MELLON-VER-SRS-22
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
    # MELLON-VER-SRS-56: only reads: git objects by id and files on disk;
    # nothing in the working copy, the index or a reference is written.
    repo, root = discover_repo(repo_root)
    side_a = _open_side(repo, root, doc_id, ref_a)
    side_b = _open_side(repo, root, doc_id, ref_b)
    # MELLON-VER-SRS-26: absent at both versions is an error naming the
    # document and both versions, not an empty result.
    if not side_a.present and not side_b.present:
        raise GitError(f"document {doc_id!r} not present at {ref_a} or {ref_b}")

    read_a, read_b = _Reader(side_a, doc_id), _Reader(side_b, doc_id)
    outline_a, labels_a, outline_error_a = _read_manifest(side_a, doc_id)
    outline_b, labels_b, outline_error_b = _read_manifest(side_b, doc_id)
    # MELLON-VER-SRS-45: each attribute's label at To, or at From when only
    # From defines it.
    labels = {**labels_a, **labels_b}

    def pos(outline: _Outline | None, obj_id: str) -> Position | None:
        return outline.position(obj_id) if outline is not None else None

    # MELLON-VER-SRS-19, -20: an object exists at a version when its object
    # file does: Added at To only, Deleted at From only.
    ids_a, ids_b = set(side_a.objects), set(side_b.objects)
    added, deleted, common = ids_b - ids_a, ids_a - ids_b, ids_a & ids_b
    # MELLON-VER-SRS-29: without both outlines no object is Moved.
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
        # MELLON-VER-SRS-57: files with the same content key (blob id) are
        # skipped unparsed; only objects whose files differ are read.
        same_object = side_a.objects[obj_id] == side_b.objects[obj_id]
        same_links = side_a.links.get(obj_id) == side_b.links.get(obj_id)
        same_files = (
            same_object
            and same_links
            and side_a.figures.get(obj_id) == side_b.figures.get(obj_id)
        )
        if same_files and obj_id not in moved:
            continue
        before = read_a.state(obj_id, pos(outline_a, obj_id))
        after = read_b.state(obj_id, pos(outline_b, obj_id))
        # MELLON-VER-SRS-27: a file with the same content at both versions is
        # no change, so it isn't reported unreadable, even when it can't be
        # read; that leaves an ack-only change Link reviewed (-24).
        cleared: dict[str, None] = {}
        if same_object:
            cleared["error"] = None
        if same_links:
            cleared["links_error"] = None
        if cleared:
            before, after = replace(before, **cleared), replace(after, **cleared)
        kinds: list[Change] = []
        delta = None
        if not same_files:
            # MELLON-VER-SRS-27: the files differ, so an object that can't be
            # read on one side is reported (Modified, with the reason at that
            # version) rather than silently passed over.
            if before.problem or after.problem:
                kinds.append(Change.MODIFIED)
            # MELLON-VER-SRS-21, -22: Modified only when a parsed field
            # differs, so a difference gone in the canonical form is none.
            delta = _delta(obj_id, before, after)
            if delta.is_empty:
                delta = None
            else:
                modified.append(delta)
                if Change.MODIFIED not in kinds:
                    # MELLON-VER-SRS-24: only acknowledged commits changed:
                    # Link reviewed, not Modified.
                    kinds.append(
                        Change.LINK_REVIEWED if delta.only_link_reviews else Change.MODIFIED
                    )
        # MELLON-VER-SRS-23: Moved together with the object's other changes.
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
        commit_a=side_a.commit,
        commit_b=side_b.commit,
    )


# --- internals ------------------------------------------------------------


def _figure_key(svg: bytes | None) -> object:
    """What two figures are compared by.

    MELLON-VER-SRS-22: a figure is an SVG file, so XML, and differences gone
    in its canonical form are no change: the figure as Canonical XML writes
    it, with whitespace-only text between elements removed. (For a whole
    document, Canonical XML 1.0, which lxml writes, and 1.1 write the same.
    Comments are left out, as in the 1.1 method without comments, and as
    parsing leaves them out of the other files.) A figure that can't be
    parsed is compared by its bytes, as before.
    """
    if svg is None:
        return None
    # Never fetch the DTD a draw.io file names; internal entities are part
    # of the content, so they are expanded.
    parser = etree.XMLParser(resolve_entities="internal", no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(svg, parser)
        for node in root.iter():
            if isinstance(node.tag, str) and len(node) and _blank(node.text):
                node.text = None
            if _blank(node.tail):
                node.tail = None
        canonical = etree.tostring(root.getroottree(), method="c14n", with_comments=False)
        return ("canonical", canonical)
    except (etree.LxmlError, ValueError):
        return ("bytes", svg)


def _blank(text: str | None) -> bool:
    """Whitespace-only text, by XML's whitespace characters."""
    return text is not None and not text.strip(" \t\r\n")


def _delta(obj_id: str, before: ObjectState, after: ObjectState) -> ObjectDelta:
    """Field changes between two states; fields that couldn't be read on
    either side are left out."""
    attrs: tuple[AttributeChange, ...] = ()
    description = None
    table = None
    # MELLON-VER-SRS-21: each field: attribute values, description, table,
    # figure and outgoing links.
    if before.obj is not None and after.obj is not None:
        attrs = _diff_attrs(before.obj.attrs, after.obj.attrs)
        if before.obj.description != after.obj.description:
            description = DescriptionChange(before.obj.description, after.obj.description)
        if before.obj.table != after.obj.table:
            table = TableChange(before.obj.table, after.obj.table)
    figure = (
        FigureChange(before.figure, after.figure)
        if before.figure != after.figure
        and _figure_key(before.figure) != _figure_key(after.figure)
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
    # MELLON-VER-SRS-46: each link added and each removed, by target and
    # link type.
    for key in sorted(by_key_b.keys() - by_key_a.keys()):
        link = by_key_b[key]
        added.append(LinkChange(*key, ack=link.ack))
    for key in sorted(by_key_a.keys() - by_key_b.keys()):
        link = by_key_a[key]
        removed.append(LinkChange(*key, ack=link.ack))
    # MELLON-VER-SRS-24, -53: the same link with another acknowledged commit,
    # kept apart from added and removed links, with the commit at both.
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
