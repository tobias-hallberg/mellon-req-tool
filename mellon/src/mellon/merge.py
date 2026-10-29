"""Resolve a git merge that stopped with conflicts in Mellon documents.

Git merges document files as text, which misses what an object is. Two
branches that each created the next object both call it `<doc>-<n>`: the
object and links files conflict, the manifest gains a duplicate outline ref
without complaint, and links one branch made to its new object end up
pointing at the other branch's object.

`plan(root)` reads the three commits of the merge in progress — the merge
base, ours (HEAD) and theirs (MERGE_HEAD) — and works out, per document:

- **Renumbered:** an id both branches created for different objects isn't a
  conflict. The object committed later yields: its branch is rewritten as if
  it had used the next id free on both branches from the start — its object,
  links and figure files, its outline ref, and every link that branch made to
  it, in any document.
- **Merged by itself:** a file or field changed on one side only takes that
  side. Objects merge field by field (each attribute, the text, the table),
  links files as sets of links, a manifest's outline by object position and
  its properties field by field.
- **Merged by hand:** a field both branches changed differently — an
  object's text or an attribute, a table, a figure, a metadata field, an
  attribute definition, a view, a filter, the rules — and an object deleted
  on one side but changed on the other. Each defaults to ours (and to keeping
  the object); `resolve` takes the merged text or the side to use. An
  object's hand merge carries the requirements it traces to, for context.

`resolve(root, edits=..., delete=...)` writes the result into the work tree
and marks the paths resolved in the index; the merge commit is left to the
git client. Everything is computed from the three commits, not from git's
conflict markers, so it can simply be run again. Paths outside document
folders are reported, never touched.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pygit2
from lxml import etree

from .editor import EditorError, _sanitize_text
from .git_ops import GitError, _replace_file, discover_repo

Side = Literal["ours", "theirs"]
# text: merged text to type in; value / whole: "ours" or "theirs"
FieldKind = Literal["text", "value", "whole"]

# repo-relative posix path -> file content
Files = dict[str, bytes]

_XML_HEADER = b'<?xml version="1.0" encoding="UTF-8"?>\n'
_EXCERPT_CHARS = 120


class MergeError(Exception):
    """No mergeable state: no merge in progress, unusable merged text, ..."""


@dataclass(frozen=True)
class Renumbered:
    """A new object both branches created under one id: the later one moves."""

    doc_id: str
    obj_id: str
    new_id: str  # what the later object becomes
    side: Side  # the side whose object committed later and is renumbered
    text: str  # its description, shortened


@dataclass(frozen=True)
class FieldConflict:
    """A field both branches changed differently."""

    field: str  # key for `resolve`'s edits: "description", "attr:status", ...
    label: str  # "Text", "status", "Table", "Figure", "Title", ...
    ours: str  # shown values ("" for a table or figure, "(not set)" if absent)
    theirs: str
    kind: FieldKind


@dataclass(frozen=True)
class Linked:
    """A requirement an object traces to: one of its outgoing links."""

    target_doc: str
    target_id: str
    link_type: str
    text: str


@dataclass(frozen=True)
class HandMerge:
    """One file that needs a hand: its conflicting fields, or a deletion on
    one side against a change on the other (`deleted_on`)."""

    path: str
    doc_id: str
    title: str  # "VCU3-SRS-1", "document properties", "view triage", ...
    conflicts: tuple[FieldConflict, ...]
    from_theirs: tuple[str, ...]  # field labels merged in from theirs alone
    deleted_on: Side | None
    links: tuple[Linked, ...]  # for an object: what it traces to


@dataclass(frozen=True)
class MergePlan:
    into: str  # the branch being merged into (HEAD)
    merging: str  # the branch being merged in
    renumbered: tuple[Renumbered, ...]
    by_hand: tuple[HandMerge, ...]
    outside: tuple[str, ...]  # conflicted paths outside document folders


# --- reading the merge ------------------------------------------------------


@dataclass
class _State:
    repo: pygit2.Repository
    base: Files
    ours: Files
    theirs: Files
    ours_commit: pygit2.Commit
    theirs_commit: pygit2.Commit
    base_oid: pygit2.Oid | None
    conflicted: tuple[str, ...]


def _open(root: Path) -> _State:
    try:
        repo, git_root = discover_repo(root)
    except GitError as e:
        raise MergeError(str(e)) from e
    if Path(root).resolve() != git_root:
        raise MergeError(f"{root} is not the root of its git work tree")
    if repo.state() != pygit2.GIT_REPOSITORY_STATE_MERGE:
        raise MergeError("no merge is in progress")
    merge_head = Path(repo.path) / "MERGE_HEAD"
    heads = merge_head.read_text(encoding="utf-8").split()
    if len(heads) != 1:
        raise MergeError("a merge of more than two branches can't be resolved here")
    ours = repo.head.peel(pygit2.Commit)
    theirs = repo[heads[0]].peel(pygit2.Commit)
    base_oid = repo.merge_base(ours.id, theirs.id)
    base_tree = repo[base_oid].peel(pygit2.Commit).tree if base_oid else None
    return _State(
        repo=repo,
        base=_doc_files(repo, base_tree) if base_tree is not None else {},
        ours=_doc_files(repo, ours.tree),
        theirs=_doc_files(repo, theirs.tree),
        ours_commit=ours,
        theirs_commit=theirs,
        base_oid=base_oid,
        conflicted=_conflicted_paths(repo),
    )


def _doc_files(repo: pygit2.Repository, tree: pygit2.Tree) -> Files:
    """Every file of every document folder (`X/` holding `X/X.xml`) in `tree`."""
    files: Files = {}
    for entry in tree:
        if entry.type_str != "tree":
            continue
        folder = repo[entry.id]
        if f"{entry.name}.xml" not in folder:
            continue
        for rel, data in _walk(repo, folder, entry.name):
            files[rel] = data
    return files


def _walk(
    repo: pygit2.Repository, tree: pygit2.Tree, prefix: str
) -> Iterator[tuple[str, bytes]]:
    for entry in tree:
        path = f"{prefix}/{entry.name}"
        if entry.type_str == "tree":
            yield from _walk(repo, repo[entry.id], path)
        elif entry.type_str == "blob":
            yield path, repo[entry.id].data


def _doc_ids(*sides: Files) -> list[str]:
    ids = set()
    for files in sides:
        for path in files:
            doc, _, rest = path.partition("/")
            if rest == f"{doc}.xml":
                ids.add(doc)
    return sorted(ids)


def _object_ids(files: Files, doc: str) -> dict[str, str]:
    """obj_id -> path of `doc`'s object files."""
    prefix = f"{doc}/objects/"
    return {
        path[len(prefix):-4]: path
        for path in files
        if path.startswith(prefix)
        and path.endswith(".xml")
        and "/" not in path[len(prefix):]
    }


def _branch_names(st: _State) -> tuple[str, str]:
    """(into, merging): HEAD's branch and the branch named in MERGE_MSG,
    falling back to short hashes."""
    repo = st.repo
    into = (
        str(st.ours_commit.id)[:7] if repo.head_is_detached else repo.head.shorthand
    )
    merging = str(st.theirs_commit.id)[:7]
    msg = Path(repo.path) / "MERGE_MSG"
    if msg.is_file():
        first = msg.read_text(encoding="utf-8", errors="replace").splitlines()
        hit = re.match(
            r"Merge (?:remote-tracking )?branch '([^']+)'", first[0] if first else ""
        )
        if hit:
            merging = hit.group(1)
    return into, merging


# --- XML helpers ----------------------------------------------------------------


def _parse(data: bytes) -> etree._Element:
    return etree.fromstring(data)


def _dump(root: etree._Element) -> bytes:
    """Serialise like `editor._write_tree`: canonical indent, LF, header."""
    root = copy.deepcopy(root)
    etree.indent(root, space="  ")
    body = etree.tostring(root, encoding="UTF-8")
    return _XML_HEADER + body + b"\n"


def _el_text(el: etree._Element) -> str:
    """An element as canonical text, to compare and carry it whole."""
    el = copy.deepcopy(el)
    el.tail = None
    etree.indent(el, space="  ")
    return etree.tostring(el, encoding="unicode")


def _description(obj_data: bytes | None, limit: int | None = None) -> str:
    if obj_data is None:
        return ""
    try:
        desc = _parse(obj_data).findtext("description") or ""
    except etree.XMLSyntaxError:
        return ""
    line = " ".join(desc.split())
    if limit is not None and len(line) > limit:
        return line[: limit - 3] + "..."
    return line


# --- renumbering -----------------------------------------------------------------


def _find_renumbered(st: _State) -> list[Renumbered]:
    out: list[Renumbered] = []
    for doc in _doc_ids(st.ours, st.theirs):
        base_ids = _object_ids(st.base, doc)
        ours_ids = _object_ids(st.ours, doc)
        theirs_ids = _object_ids(st.theirs, doc)
        both = sorted(
            (set(ours_ids) & set(theirs_ids)) - set(base_ids),
            key=_id_sort_key,
        )
        both = [i for i in both if st.ours[ours_ids[i]] != st.theirs[theirs_ids[i]]]
        if not both:
            continue
        next_n = _max_number(doc, base_ids, ours_ids, theirs_ids) + 1
        for obj_id in both:
            path = ours_ids[obj_id]
            t_ours = _added_time(st, st.ours_commit, path)
            t_theirs = _added_time(st, st.theirs_commit, path)
            side: Side = "ours" if t_ours > t_theirs else "theirs"
            data = st.ours[path] if side == "ours" else st.theirs[path]
            out.append(Renumbered(
                doc_id=doc,
                obj_id=obj_id,
                new_id=f"{doc}-{next_n}",
                side=side,
                text=_description(data, _EXCERPT_CHARS),
            ))
            next_n += 1
    return out


def _id_sort_key(obj_id: str) -> tuple[int, str]:
    tail = obj_id.rsplit("-", 1)[-1]
    return (int(tail), obj_id) if tail.isdigit() else (-1, obj_id)


def _max_number(doc: str, *id_maps: Mapping[str, str]) -> int:
    prefix = f"{doc}-"
    numbers = [
        int(i[len(prefix):])
        for ids in id_maps
        for i in ids
        if i.startswith(prefix) and i[len(prefix):].isdigit()
    ]
    return max(numbers, default=-1)


def _added_time(st: _State, tip: pygit2.Commit, path: str) -> int:
    """Commit time of the first commit on `tip`'s side that has `path`."""
    walker = st.repo.walk(
        tip.id, pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
    )
    if st.base_oid is not None:
        walker.hide(st.base_oid)
    for commit in walker:
        try:
            commit.tree[path]
        except KeyError:
            continue
        return commit.commit_time
    return tip.commit_time


def _renumber(files: Files, renames: Mapping[tuple[str, str], str]) -> Files:
    """`files` as if the renamed objects had their new ids from the start.

    Every link to a renamed id is retargeted: the id is new on this side,
    so only this side's objects can have meant this side's object.
    """
    if not renames:
        return files
    out = dict(files)
    for (doc, old), new in renames.items():
        obj = f"{doc}/objects/{old}.xml"
        if obj in out:
            root = _parse(out.pop(obj))
            root.set("id", new)
            out[f"{doc}/objects/{new}.xml"] = _dump(root)
        links = f"{doc}/links/{old}.xml"
        if links in out:
            root = _parse(out.pop(links))
            root.set("source", new)
            out[f"{doc}/links/{new}.xml"] = _dump(root)
        fig = f"{doc}/figures/{old}.svg"
        if fig in out:
            out[f"{doc}/figures/{new}.svg"] = out.pop(fig)
        manifest = f"{doc}/{doc}.xml"
        if manifest in out:
            root = _parse(out[manifest])
            for ref in root.iter("ref"):
                if ref.get("id") == old:
                    ref.set("id", new)
            out[manifest] = _dump(root)
    by_target = {(doc, old): new for (doc, old), new in renames.items()}
    for path, data in list(out.items()):
        if "/links/" not in path or not path.endswith(".xml"):
            continue
        root = _parse(data)
        changed = False
        for link in root.iter("link"):
            new = by_target.get((link.get("target-doc"), link.get("target-id")))
            if new is not None:
                link.set("target-id", new)
                changed = True
        if changed:
            out[path] = _dump(root)
    return out


# --- field-level three-way merges -------------------------------------------------

_NOT_SET = "(not set)"


@dataclass
class _Hand:
    """A HandMerge being assembled."""

    conflicts: list[FieldConflict] = field(default_factory=list)
    from_theirs: list[str] = field(default_factory=list)
    deleted_on: Side | None = None


def _three_way(
    b: Mapping[str, str | None],
    o: Mapping[str, str | None],
    t: Mapping[str, str | None],
    *,
    kind_of,
    label_of,
    shown,
    edits: Mapping[str, str],
    hand: _Hand,
) -> dict[str, str | None]:
    """Merge field maps (absent = None). A field changed on both sides is a
    conflict: its edit decides (text, or "ours" / "theirs"), else ours."""
    keys = list(o) + [k for k in t if k not in o] + [
        k for k in b if k not in o and k not in t
    ]
    merged: dict[str, str | None] = {}
    for key in keys:
        bv, ov, tv = b.get(key), o.get(key), t.get(key)
        if ov == tv:
            merged[key] = ov
        elif bv == ov:
            merged[key] = tv
            hand.from_theirs.append(label_of(key))
        elif bv == tv:
            merged[key] = ov
        else:
            kind = kind_of(key)
            hand.conflicts.append(
                FieldConflict(key, label_of(key), shown(ov), shown(tv), kind)
            )
            choice = edits.get(key)
            if kind == "text":
                merged[key] = ov if choice is None else _clean(choice, label_of(key))
            else:
                merged[key] = tv if choice == "theirs" else ov
    return merged


def _clean(text: str, what: str) -> str:
    try:
        return _sanitize_text(text, what)
    except EditorError as e:
        raise MergeError(str(e)) from e


def _shown_value(v: str | None) -> str:
    return _NOT_SET if v is None else v


def _object_fields(data: bytes | None) -> dict[str, str | None]:
    """An object as fields: `attr:<name>`, `description`, `table`."""
    if data is None:
        return {}
    root = _parse(data)
    fields: dict[str, str | None] = {
        f"attr:{a.get('name')}": a.text or "" for a in root.findall("attr")
    }
    fields["description"] = root.findtext("description") or ""
    table = root.find("table")
    if table is not None:
        fields["table"] = _el_text(table)
    return fields


def _object_label(key: str) -> str:
    if key.startswith("attr:"):
        return key[5:]
    return {"description": "Text", "table": "Table"}.get(key, key)


def _merge_object(
    b: bytes | None, o: bytes, t: bytes, edits: Mapping[str, str], hand: _Hand
) -> bytes:
    """Merge an object field by field: each attribute, the text, the table."""
    merged = _three_way(
        _object_fields(b), _object_fields(o), _object_fields(t),
        kind_of=lambda k: (
            "text" if k == "description" else "whole" if k == "table" else "value"
        ),
        label_of=_object_label,
        shown=lambda v: (
            "" if v is not None and v.startswith("<table") else _shown_value(v)
        ),
        edits=edits,
        hand=hand,
    )
    template = _parse(o)
    root = etree.Element("object", id=template.get("id"))
    for key, value in merged.items():
        if key.startswith("attr:") and value is not None:
            etree.SubElement(root, "attr", name=key[5:]).text = value
    etree.SubElement(root, "description").text = merged.get("description") or ""
    if merged.get("table"):
        root.append(etree.fromstring(merged["table"]))
    for child in template:  # anything else an object file carries
        if child.tag not in ("attr", "description", "table"):
            root.append(copy.deepcopy(child))
    return _dump(root)


def _merge_links(b: bytes | None, o: bytes | None, t: bytes | None) -> bytes | None:
    """Links merge as a set keyed by target. A link changed on both sides
    (another type or ack) keeps ours; one deleted and changed on the other
    side is kept."""
    _, bl = _links_of(b)
    o_src, ol = _links_of(o)
    t_src, tl = _links_of(t)
    keys = list(ol) + [k for k in tl if k not in ol]
    result = {}
    for key in keys:
        bv, ov, tv = bl.get(key), ol.get(key), tl.get(key)
        if ov == tv:
            v = ov
        elif bv == ov:
            v = tv
        elif bv == tv:
            v = ov
        else:
            v = ov if ov is not None else tv
        if v is not None:
            result[key] = v
    if not result:
        return None
    root = etree.Element("links", source=o_src or t_src or "")
    for attrs in result.values():
        etree.SubElement(root, "link", attrs)
    return _dump(root)


def _links_of(data: bytes | None) -> tuple[str | None, dict[tuple, dict]]:
    if data is None:
        return None, {}
    root = _parse(data)
    links = {
        (el.get("target-doc"), el.get("target-id")): dict(el.attrib)
        for el in root.findall("link")
    }
    return root.get("source"), links


def _merge_manifest(
    b: bytes | None,
    o: bytes,
    t: bytes,
    edits: Mapping[str, str],
    hand: _Hand,
    git_merged: bytes | None,
) -> bytes:
    """The outline merges by object position. The properties are git's
    clean merge when it made one, else merged field by field: metadata
    fields, the parents as a set, attribute definitions by name."""
    if git_merged is not None:
        root = _parse(git_merged)
    else:
        root = _merge_properties(b, o, t, edits, hand)
    merged = _merge_outline(_outline_of(b), _outline_of(o), _outline_of(t))
    new_outline = etree.Element("outline")
    _build_outline(new_outline, merged)
    outline = root.find("outline")
    if outline is None:
        root.append(new_outline)
    else:
        root.replace(outline, new_outline)
    return _dump(root)


def _merge_properties(
    b: bytes | None, o: bytes, t: bytes, edits: Mapping[str, str], hand: _Hand
) -> etree._Element:
    broot = _parse(b) if b is not None else None
    oroot, troot = _parse(o), _parse(t)
    root = copy.deepcopy(oroot)

    def meta(r) -> dict[str, str | None]:
        m = r.find("metadata") if r is not None else None
        return {f"meta:{c.tag}": c.text or "" for c in m} if m is not None else {}

    merged_meta = _three_way(
        meta(broot), meta(oroot), meta(troot),
        kind_of=lambda _k: "text",
        label_of=lambda k: k[5:].capitalize(),
        shown=_shown_value,
        edits=edits,
        hand=hand,
    )
    metadata = _child(root, "metadata", after=None)
    for c in list(metadata):
        metadata.remove(c)
    for key, value in merged_meta.items():
        if value is not None:
            etree.SubElement(metadata, key[5:]).text = value

    def parents(r) -> list[str]:
        p = r.find("parents") if r is not None else None
        return [e.get("doc") for e in p.findall("parent")] if p is not None else []

    bp, op, tp = parents(broot), parents(oroot), parents(troot)
    merged_parents = [d for d in op if not (d in bp and d not in tp)] + [
        d for d in tp if d not in op and d not in bp
    ]
    if merged_parents or root.find("parents") is not None:
        parents_el = _child(root, "parents", after="metadata")
        for c in list(parents_el):
            parents_el.remove(c)
        for d in merged_parents:
            etree.SubElement(parents_el, "parent", doc=d)

    def attributes(r) -> dict[str, str | None]:
        a = r.find("attributes") if r is not None else None
        if a is None:
            return {}
        return {
            f"attribute:{e.get('name')}": _el_text(e)
            for e in a.findall("attribute")
        }

    merged_attrs = _three_way(
        attributes(broot), attributes(oroot), attributes(troot),
        kind_of=lambda _k: "whole",
        label_of=lambda k: f"attribute {k[10:]}",
        shown=lambda _v: "",
        edits=edits,
        hand=hand,
    )
    attrs_el = _child(root, "attributes", after="parents")
    for c in list(attrs_el):
        attrs_el.remove(c)
    for value in merged_attrs.values():
        if value is not None:
            attrs_el.append(etree.fromstring(value))
    return root


def _child(root: etree._Element, tag: str, *, after: str | None) -> etree._Element:
    """`root`'s `<tag>`, created after `<after>` (or first) when missing."""
    el = root.find(tag)
    if el is not None:
        return el
    el = etree.Element(tag)
    anchor = root.find(after) if after else None
    if anchor is None and after == "parents":
        anchor = root.find("metadata")
    if anchor is not None:
        anchor.addnext(el)
    else:
        root.insert(0, el)
    return el


# An outline as nested [id, [children...]] lists.
_Node = list


def _outline_of(data: bytes | None) -> list[_Node]:
    if data is None:
        return []
    outline = _parse(data).find("outline")
    if outline is None:
        return []

    def nodes(el) -> list[_Node]:
        return [[ref.get("id"), nodes(ref)] for ref in el.findall("ref")]

    return nodes(outline)


def _build_outline(parent_el: etree._Element, nodes: list[_Node]) -> None:
    for obj_id, children in nodes:
        ref = etree.SubElement(parent_el, "ref", id=obj_id)
        _build_outline(ref, children)


def _positions(nodes: list[_Node]) -> dict[str, tuple[str | None, str | None]]:
    """obj_id -> (parent id, previous sibling id)."""
    out: dict[str, tuple[str | None, str | None]] = {}

    def walk(children: list[_Node], parent: str | None) -> None:
        prev = None
        for obj_id, sub in children:
            out[obj_id] = (parent, prev)
            walk(sub, obj_id)
            prev = obj_id

    walk(nodes, None)
    return out


def _preorder(nodes: list[_Node]) -> Iterator[str]:
    for obj_id, sub in nodes:
        yield obj_id
        yield from _preorder(sub)


def _merge_outline(
    base: list[_Node], ours: list[_Node], theirs: list[_Node]
) -> list[_Node]:
    """Ours, plus theirs' additions, deletions and moves at their place.

    An object moved on both sides stays where ours put it.
    """
    result = copy.deepcopy(ours)
    bpos, opos, tpos = _positions(base), _positions(ours), _positions(theirs)

    def find(nodes: list[_Node], obj_id: str) -> tuple[list[_Node], int] | None:
        for i, (nid, sub) in enumerate(nodes):
            if nid == obj_id:
                return nodes, i
            hit = find(sub, obj_id)
            if hit is not None:
                return hit
        return None

    def detach(obj_id: str, keep_children: bool) -> _Node:
        siblings, i = find(result, obj_id)
        node = siblings.pop(i)
        if not keep_children:
            # Children nobody deleted take the removed object's place.
            siblings[i:i] = node[1]
            node[1] = []
        return node

    def place(node: _Node) -> None:
        """Insert `node` where theirs has it."""
        parent, _ = tpos[node[0]]
        siblings = result
        while parent is not None:
            hit = find(result, parent)
            if hit is not None:
                psibs, pi = hit
                siblings = psibs[pi][1]
                break
            parent = tpos.get(parent, (None, None))[0]
        # After the nearest earlier sibling (in theirs) that is in place.
        before = [oid for oid, (p, _) in tpos.items() if p == tpos[node[0]][0]]
        earlier = before[: before.index(node[0])]
        here = [nid for nid, _ in siblings]
        index = 0
        for oid in reversed(earlier):
            if oid in here:
                index = here.index(oid) + 1
                break
        siblings.insert(index, node)

    # Deleted on theirs (and still in ours): gone.
    for obj_id in list(_preorder(result)):
        if obj_id in bpos and obj_id not in tpos and find(result, obj_id):
            detach(obj_id, keep_children=False)
    for obj_id in _preorder(theirs):
        present = find(result, obj_id) is not None
        if not present:
            if obj_id not in bpos:  # added on theirs
                place([obj_id, []])
            continue  # else deleted on ours
        moved_theirs = obj_id in bpos and tpos[obj_id] != bpos[obj_id]
        moved_ours = opos.get(obj_id) != bpos.get(obj_id)
        if moved_theirs and not moved_ours:
            place(detach(obj_id, keep_children=True))
    return result


# --- plan and resolve ---------------------------------------------------------------


def _kind(path: str) -> str:
    doc, _, rest = path.partition("/")
    if rest == f"{doc}.xml":
        return "manifest"
    head = rest.split("/", 1)[0]
    return {"links": "links", "objects": "object", "figures": "figure",
            "views": "view", "filters": "filter"}.get(head, "file")


def _title(path: str) -> str:
    kind = _kind(path)
    name = path.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    if kind == "object":
        return stem
    if kind == "manifest":
        return "document properties"
    if kind in ("view", "filter"):
        return f"{kind} {stem}"
    if name.endswith(".rules.xml"):
        return "rules"
    return name


def _in_docs(path: str, docs: set[str]) -> bool:
    return path.partition("/")[0] in docs


@dataclass
class _Outcome:
    renumbered: list[Renumbered]
    results: dict[str, bytes | None]
    hands: dict[str, _Hand]
    links: dict[str, tuple[Linked, ...]]
    outside: list[str]


def _compute(
    st: _State,
    edits: Mapping[str, Mapping[str, str]],
    delete: Iterable[str],
) -> _Outcome:
    docs = set(_doc_ids(st.ours, st.theirs))
    delete = set(delete)
    renumbered = _find_renumbered(st)
    o = _renumber(st.ours, {
        (r.doc_id, r.obj_id): r.new_id for r in renumbered if r.side == "ours"
    })
    t = _renumber(st.theirs, {
        (r.doc_id, r.obj_id): r.new_id for r in renumbered if r.side == "theirs"
    })

    # Every document file both sides changed, whatever git made of it — so a
    # second run redoes the first one's work instead of skipping it.
    b = st.base
    affected = {
        p for p in set(o) | set(t) | set(b)
        if b.get(p) != o.get(p) and b.get(p) != t.get(p) and o.get(p) != t.get(p)
    }
    affected |= {p for p in st.conflicted if _in_docs(p, docs)}
    for r in renumbered:
        d = r.doc_id
        for i in (r.obj_id, r.new_id):
            affected |= {f"{d}/objects/{i}.xml", f"{d}/links/{i}.xml",
                         f"{d}/figures/{i}.svg"}
        affected.add(f"{d}/{d}.xml")
    for side, renamed in ((st.ours, o), (st.theirs, t)):
        affected |= {p for p in renamed if renamed.get(p) != side.get(p)}
        affected |= {p for p in side if p not in renamed}

    results: dict[str, bytes | None] = {}
    hands: dict[str, _Hand] = {}
    for path in sorted(affected):
        b, ov, tv = st.base.get(path), o.get(path), t.get(path)
        if ov == tv:
            results[path] = ov
            continue
        if b == ov:
            results[path] = tv
            continue
        if b == tv:
            results[path] = ov
            continue
        kind = _kind(path)
        if kind == "links":
            results[path] = _merge_links(b, ov, tv)
            continue
        # The figure belongs to its object's hand merge.
        owner = path
        if kind == "figure":
            doc = path.partition("/")[0]
            owner = f"{doc}/objects/{path.rsplit('/', 1)[-1][:-4]}.xml"
        hand = hands.setdefault(owner, _Hand())
        mine = edits.get(owner, {})
        if ov is None or tv is None:
            if kind == "figure":
                hand.conflicts.append(FieldConflict(
                    "figure", "Figure", "" if ov else "(none)", "" if tv else "(none)",
                    "whole",
                ))
                results[path] = tv if mine.get("figure") == "theirs" else ov
            else:  # deleted on one side, changed on the other: kept by default
                hand.deleted_on = "ours" if ov is None else "theirs"
                results[path] = None if path in delete else (ov or tv)
        elif kind == "object":
            results[path] = _merge_object(b, ov, tv, mine, hand)
        elif kind == "manifest":
            results[path] = _merge_manifest(
                b, ov, tv, mine, hand, _git_merged(st, path)
            )
        elif kind == "figure":
            hand.conflicts.append(FieldConflict("figure", "Figure", "", "", "whole"))
            results[path] = tv if mine.get("figure") == "theirs" else ov
        else:
            hand.conflicts.append(FieldConflict("file", "Keep", "", "", "whole"))
            results[path] = tv if mine.get("file") == "theirs" else ov
    # A renumbered object's files that exist nowhere (no figure, say).
    for path in [p for p, data in results.items() if data is None]:
        if not any(path in files for files in (st.ours, st.theirs, o, t)):
            del results[path]
    hands = {p: h for p, h in hands.items() if h.conflicts or h.deleted_on}
    links = {
        p: _traces(st, results, p) for p in hands if _kind(p) == "object"
    }
    outside = [p for p in st.conflicted if not _in_docs(p, docs)]
    return _Outcome(renumbered, results, hands, links, outside)


def _merged_file(st: _State, results: Mapping[str, bytes | None], path: str):
    """`path` as the merge leaves it: Mellon's result, else git's clean
    merge, else ours."""
    if path in results:
        return results[path]
    staged = _git_merged(st, path)
    return staged if staged is not None else st.ours.get(path)


def _traces(
    st: _State, results: Mapping[str, bytes | None], obj_path: str
) -> tuple[Linked, ...]:
    """What the merged object links to, with the targets' merged text."""
    doc = obj_path.partition("/")[0]
    obj_id = obj_path.rsplit("/", 1)[-1][:-4]
    links_data = _merged_file(st, results, f"{doc}/links/{obj_id}.xml")
    _, links = _links_of(links_data)
    out = []
    for (tdoc, tid), attrs in links.items():
        target = _merged_file(st, results, f"{tdoc}/objects/{tid}.xml")
        out.append(Linked(tdoc, tid, attrs.get("type", ""), _description(target)))
    return tuple(out)


def _git_merged(st: _State, path: str) -> bytes | None:
    """Git's clean merge of `path` (the staged file), None if it conflicted
    or isn't staged."""
    if path in st.conflicted:
        return None
    try:
        entry = st.repo.index[path]
    except KeyError:
        return None
    return st.repo[entry.id].data


def pending(root: str | Path) -> tuple[str, ...] | None:
    """The conflicted paths of the merge in progress at `root`; None when
    no merge is in progress (or `root` isn't in a git repository)."""
    try:
        repo, _ = discover_repo(root)
    except GitError:
        return None
    if repo.state() != pygit2.GIT_REPOSITORY_STATE_MERGE:
        return None
    return _conflicted_paths(repo)


def _conflicted_paths(repo: pygit2.Repository) -> tuple[str, ...]:
    conflicts = repo.index.conflicts
    if conflicts is None:
        return ()
    return tuple(sorted(
        next(e.path for e in entry if e is not None) for entry in conflicts
    ))


def plan(root: str | Path) -> MergePlan:
    """What resolving the merge in progress at `root` would do.

    Raises `MergeError` when no merge is in progress.
    """
    st = _open(Path(root))
    out = _compute(st, {}, ())
    into, merging = _branch_names(st)
    by_hand = tuple(
        HandMerge(
            path=p,
            doc_id=p.partition("/")[0],
            title=_title(p),
            conflicts=tuple(h.conflicts),
            from_theirs=tuple(h.from_theirs),
            deleted_on=h.deleted_on,
            links=out.links.get(p, ()),
        )
        for p, h in out.hands.items()
    )
    return MergePlan(
        into=into,
        merging=merging,
        renumbered=tuple(out.renumbered),
        by_hand=by_hand,
        outside=tuple(out.outside),
    )


def resolve(
    root: str | Path,
    *,
    edits: Mapping[str, Mapping[str, str]] | None = None,
    delete: Iterable[str] = (),
) -> tuple[str, ...]:
    """Write the merged documents into the work tree and mark them resolved.

    `edits` maps a `HandMerge.path` to `{field: value}` for its conflicts:
    the merged text for a "text" field, "ours" or "theirs" otherwise. A
    conflict without an edit keeps ours. `delete` lists the paths of
    deleted-versus-changed objects to delete; the others are kept. Returns
    the paths written or deleted.
    """
    root = Path(root).resolve()
    st = _open(root)
    results = _compute(st, edits or {}, delete).results
    for doc in {p.partition("/")[0] for p in results}:
        _reconcile_outline(root, doc, results)

    index = st.repo.index
    done: list[str] = []
    for path, data in sorted(results.items()):
        target = root / path
        if data is None:
            if target.exists():
                target.unlink()
            index.remove_all([path])
        else:
            if not target.exists() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace_file(target, data)
            index.add(path)
        if index.conflicts is not None and path in index.conflicts:
            del index.conflicts[path]
        done.append(path)
    index.write()
    return tuple(done)


def _reconcile_outline(
    root: Path, doc: str, results: dict[str, bytes | None]
) -> None:
    """Make the outline list exactly the objects the merge keeps.

    Keeping an object the other side deleted brings back a ref the outline
    merge dropped; deleting one drops its ref.
    """
    manifest = f"{doc}/{doc}.xml"
    data = results.get(manifest)
    if data is None and manifest in results:
        return  # the document itself goes
    if data is None:
        path = root / manifest
        if not path.is_file():
            return
        data = path.read_bytes()
    try:
        doc_root = _parse(data)
    except etree.XMLSyntaxError:
        return
    outline = doc_root.find("outline")
    if outline is None:
        return
    objects = {
        p.stem for p in (root / doc / "objects").glob("*.xml")
    } if (root / doc / "objects").is_dir() else set()
    for path, content in results.items():
        if path.startswith(f"{doc}/objects/") and path.endswith(".xml"):
            stem = path.rsplit("/", 1)[-1][:-4]
            (objects.add if content is not None else objects.discard)(stem)
    changed = False
    for ref in list(outline.iter("ref")):
        if ref.get("id") not in objects:
            parent = ref.getparent()
            i = parent.index(ref)
            for child in reversed(list(ref)):
                parent.insert(i, child)
            parent.remove(ref)
            changed = True
    listed = {ref.get("id") for ref in outline.iter("ref")}
    for obj_id in sorted(objects - listed, key=_id_sort_key):
        etree.SubElement(outline, "ref", id=obj_id)
        changed = True
    if changed:
        results[manifest] = _dump(doc_root)
