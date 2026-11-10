"""Helpers for the Compare tests in test_diff.py, test_gui_compare.py and
test_mcp_git.py: the plain helpers more than one of those modules uses.

`_build` and `_tag` are test_gui_compare's standard repository; the rest came
with the tests written blind from MELLON-VER-SRS. The fixtures built on these
helpers (`rev_template`, `canon_template`, `canon_repo`) are in conftest.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pygit2
from lxml import etree

from mellon import editor, git_ops
from mellon.diff import WORKING_COPY
from mellon.git_ops import commit, create_issue_tag
from mellon.schema import Table, TableCell, TableColumn, TableRow


# --- test_gui_compare's standard repository ----------------------------------


def _tag(root: Path, issue: str) -> None:
    repo = pygit2.Repository(str(root))
    create_issue_tag(repo, "SPEC", issue, repo.head.peel(pygit2.Commit).id)


def _build(root: Path, *, issue: bool = True) -> Path:
    pygit2.init_repository(str(root))
    folder = editor.create_document(root, "SPEC", title="Spec")
    editor.create_object(folder, None, "Heading")
    editor.set_attribute(folder, "SPEC-0", "type", "Heading")
    editor.create_object(folder, "SPEC-0", "The VCU shall stop.")
    editor.create_object(folder, "SPEC-0", "Requirement 2.")
    editor.create_object(folder, "SPEC-0", "Requirement 3.")
    commit(root, "seed")
    if issue:
        _tag(root, "1")
    editor.set_description(folder, "SPEC-1", "The VCU must stop.")
    editor.set_attribute(folder, "SPEC-2", "rationale", "Because.")
    # SPEC-4 whether it's created before or after the delete: SPEC-3's id
    # isn't given out again.
    editor.create_object(folder, "SPEC-0", "Requirement 4.")
    editor.delete_object(folder, "SPEC-3")
    commit(root, "edits")
    return folder


# --- written blind from MELLON-VER-SRS (round 1) -----------------------------


DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'

SVG_1 = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    b'<rect width="20" height="10" fill="red"/></svg>\n'
)


# --------------------------------------------------------------------------
# Repository helpers
# --------------------------------------------------------------------------


class GitRepo:
    """A test repository whose commits get fixed, increasing times."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        pygit2.init_repository(str(root))
        self._time = 1_767_225_600  # 2026-01-01T00:00:00Z

    @property
    def repo(self) -> pygit2.Repository:
        return pygit2.Repository(str(self.root))

    def signature(self) -> pygit2.Signature:
        self._time += 60
        return pygit2.Signature("Blind Tester", "blind@example.invalid", self._time, 0)

    def commit(self, message: str) -> str:
        """Commit every change; return the new commit's full hash."""
        git_ops.commit(self.root, message, signature=self.signature())
        return str(self.repo.head.target)

    def commit_files(self, files: dict[str, bytes], message: str) -> str:
        """Commit a tree holding exactly `files`, built from blobs in memory and leaving
        the working copy alone (adding thousands of files from the working copy takes
        about a minute on Windows); return the full hash."""
        repo = self.repo
        index = pygit2.Index()
        for path, data in files.items():
            index.add(pygit2.IndexEntry(path, repo.create_blob(data), pygit2.GIT_FILEMODE_BLOB))
        tree = index.write_tree(repo)
        sig = self.signature()
        parents = [] if repo.head_is_unborn else [repo.head.target]
        return str(repo.create_commit("HEAD", sig, sig, message, tree, parents))

    def commit_snapshots(self, snapshots: list[tuple[str, dict[str, bytes]]]) -> list[str]:
        """Commit each (message, files) in turn on HEAD's branch, each commit's tree
        holding exactly its files. Uses one `git fast-import` run (one pack) when git is
        installed, since writing thousands of loose objects is slow on Windows."""
        git = shutil.which("git")
        if git is None:
            return [self.commit_files(files, message) for message, files in snapshots]
        branch = self.repo.references["HEAD"].target
        stream = bytearray()
        for mark, (message, files) in enumerate(snapshots, start=1):
            self._time += 60
            msg = message.encode("utf-8")
            stream += (f"commit {branch}\nmark :{mark}\n"
                       f"committer Blind Tester <blind@example.invalid> {self._time} +0000\n").encode()
            stream += b"data %d\n%s\n" % (len(msg), msg)
            if mark > 1:
                stream += b"from :%d\n" % (mark - 1)
            stream += b"deleteall\n"
            for path, data in sorted(files.items()):
                stream += b"M 100644 inline %s\ndata %d\n%s\n" % (path.encode("utf-8"), len(data), data)
            stream += b"\n"
        subprocess.run([git, "fast-import", "--quiet"], input=bytes(stream), cwd=self.root, check=True,
                       capture_output=True)
        commit = self.repo.revparse_single(branch).peel(pygit2.Commit)
        hashes = [str(commit.id)]
        for _ in snapshots[1:]:
            commit = commit.parents[0]
            hashes.append(str(commit.id))
        return hashes[::-1]

    def write(self, rel: str, data: bytes | str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink()

    def tag_issue(self, doc_id: str, issue: str, commit_hex: str) -> None:
        git_ops.create_issue_tag(self.repo, doc_id, issue, pygit2.Oid(hex=commit_hex))

    def side_commit(self, branch: str, parent_hex: str, rel: str, data: bytes, message: str) -> str:
        """A commit on `branch` (not checked out) with parent `parent_hex`, changing `rel`."""
        repo = self.repo
        parent = repo[pygit2.Oid(hex=parent_hex)]
        index = pygit2.Index()
        index.read_tree(parent.tree)
        blob = repo.create_blob(data)
        index.add(pygit2.IndexEntry(rel, blob, pygit2.GIT_FILEMODE_BLOB))
        tree = index.write_tree(repo)
        sig = self.signature()
        oid = repo.create_commit(f"refs/heads/{branch}", sig, sig, message, tree, [parent.id])
        return str(oid)

    def commit_of(self, ref: str) -> str:
        return str(self.repo.revparse_single(ref).peel(pygit2.Commit).id)


def links_xml(source: str, links: list[tuple[str, str, str, str | None]]) -> bytes:
    """A links file: each link is (target-doc, target-id, type, ack or None)."""
    lines = [f'<links source="{source}">']
    for doc, target, link_type, ack in links:
        ack_attr = f' ack="{ack}"' if ack is not None else ""
        lines.append(f'  <link target-doc="{doc}" target-id="{target}" type="{link_type}"{ack_attr}/>')
    lines.append("</links>")
    return DECL + ("\n".join(lines) + "\n").encode("utf-8")


def _table(limit: str) -> Table:
    return Table(
        columns=(TableColumn(), TableColumn()),
        rows=(
            TableRow(cells=(TableCell("Signal"), TableCell("Limit")), header=True),
            TableRow(cells=(TableCell("speed"), TableCell(limit))),
        ),
    )


def build_spec(tmp_path: Path) -> tuple[GitRepo, Path, str]:
    """SPEC at the commit "seed", made through the editor:

    SPEC-0  Heading      "Introduction"
      SPEC-1  Requirement  "The VCU shall stop."  status Draft, rationale "why",
                           link satisfies -> SPEC-2
      SPEC-2  Requirement  "Requirement 2."
      SPEC-3  Requirement  "Requirement 3."  with a 2x2 table (speed, 10)
    SPEC-4  Requirement  "Requirement 4."  with figure SVG_1
    """
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    editor.add_attr_option(f, "type", "Requirement")
    editor.add_attribute(f, "status", "enum", False, options=("Draft", "Approved"), label="Status")
    ids = [
        editor.create_object(f, None, "Introduction"),
        editor.create_object(f, "SPEC-0", "The VCU shall stop."),
        editor.create_object(f, "SPEC-0", "Requirement 2."),
        editor.create_object(f, "SPEC-0", "Requirement 3."),
        editor.create_object(f, None, "Requirement 4."),
    ]
    assert ids == ["SPEC-0", "SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"], f"setup: unexpected ids {ids}"
    editor.set_attribute(f, "SPEC-0", "type", "Heading")
    for obj_id in ids[1:]:
        editor.set_attribute(f, obj_id, "type", "Requirement")
    editor.set_attribute(f, "SPEC-1", "status", "Draft")
    editor.set_attribute(f, "SPEC-1", "rationale", "why")
    editor.add_link(f, "SPEC-1", "SPEC", "SPEC-2", "satisfies")
    editor.set_table(f, "SPEC-3", _table("10"))
    svg = tmp_path / "fig1.svg"
    svg.write_bytes(SVG_1)
    editor.set_figure(f, "SPEC-4", svg)
    return r, f, r.commit("seed")


# --------------------------------------------------------------------------
# MELLON-VER-SRS-26: document at neither version
# --------------------------------------------------------------------------


def build_other_only(tmp_path: Path) -> tuple[GitRepo, str, str]:
    """A repository holding only OTHER, with commits c1 and c2."""
    r = GitRepo(tmp_path / "repo")
    other = editor.create_document(r.root, "OTHER", title="Other")
    editor.create_object(other, None, "Other 1.")
    c1 = r.commit("c1")
    editor.set_description(other, "OTHER-0", "Other 2.")
    c2 = r.commit("c2")
    return r, c1, c2


# --------------------------------------------------------------------------
# MELLON-VER-SRS-56: comparing changes nothing
# --------------------------------------------------------------------------


def snapshot(root: Path) -> dict:
    """Every working-copy file (bytes and modified time, untracked ones included), the
    index bytes, HEAD and every reference."""
    snap: dict = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if rel.parts[0] == ".git":
            continue
        if path.is_file():
            snap[f"file {rel.as_posix()}"] = (path.read_bytes(), path.stat().st_mtime_ns)
        else:
            snap[f"dir {rel.as_posix()}"] = True
    git = root / ".git"
    snap["index"] = (git / "index").read_bytes() if (git / "index").exists() else None
    for name in ("HEAD", "ORIG_HEAD", "MERGE_HEAD", "FETCH_HEAD", "packed-refs"):
        snap[f"git {name}"] = (git / name).read_bytes() if (git / name).exists() else None
    for path in sorted((git / "refs").rglob("*")):
        if path.is_file():
            snap[f"ref file {path.relative_to(git).as_posix()}"] = path.read_bytes()
    repo = pygit2.Repository(str(root))
    for name in repo.references:
        snap[f"ref {name}"] = str(repo.references[name].target)
    return snap


def snapshot_difference(before: dict, after: dict) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [k for k in keys if before.get(k, "<absent>") != after.get(k, "<absent>")]


def build_snapshot_repo(tmp_path: Path) -> tuple[GitRepo, dict[str, str]]:
    """Issue 1 on c1 (seed); c2 edits; c3 breaks SPEC-3's object file and SPEC-1's links
    file. The working copy then has those two files good again, an unstaged change, a
    staged change that differs again in the working copy, SPEC-4's object file broken,
    and two untracked files."""
    r, f, c1 = build_spec(tmp_path)
    r.tag_issue("SPEC", "1", c1)
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    editor.move_object(f, "SPEC-2", to_top=True)
    editor.add_link(f, "SPEC-3", "SPEC", "SPEC-4", "refines")
    c2 = r.commit("edits")
    good_3 = r.read("SPEC/objects/SPEC-3.xml")
    good_links_1 = r.read("SPEC/links/SPEC-1.xml")
    r.write("SPEC/objects/SPEC-3.xml", DECL + b'<object id="SPEC-3">\n  <description>Br')
    r.write("SPEC/links/SPEC-1.xml", DECL + b'<links source="SPEC-1">\n  <link\n')
    c3 = r.commit("broken files")
    r.write("SPEC/objects/SPEC-3.xml", good_3)
    r.write("SPEC/links/SPEC-1.xml", good_links_1)
    editor.set_description(f, "SPEC-2", "Staged text.")
    repo = r.repo
    repo.index.add("SPEC/objects/SPEC-2.xml")
    repo.index.write()
    editor.set_description(f, "SPEC-2", "Unstaged text.")
    r.write("SPEC/objects/SPEC-4.xml", DECL + b'<object id="SPEC-4">\n  <descr')
    r.write("SPEC/objects/SPEC-99.xml", DECL + b'<object id="SPEC-99">\n  <description>New.</description>\n</object>\n')
    r.write("notes.txt", b"untracked notes\n")
    return r, {"c1": c1, "c2": c2, "c3": c3}


# --------------------------------------------------------------------------
# Repository helpers
# --------------------------------------------------------------------------


def _sig(n: int) -> pygit2.Signature:
    """A fixed signature, so commit hashes repeat from run to run."""
    return pygit2.Signature("Blind Tester", "blind@example.com", 1_700_000_000 + 60 * n, 0)


def _commit(root: Path, message: str, n: int | None = None) -> str:
    """Commit everything under `root`; return the new commit's full hash."""
    git_ops.commit(root, message, signature=_sig(n) if n is not None else None)
    return _oid(root, "HEAD")


def _oid(root: Path, rev: str) -> str:
    repo = pygit2.Repository(str(root))
    return str(repo.revparse_single(rev).peel(pygit2.Commit).id)


def _commit_of(root: Path, ref: str | None) -> str | None:
    """The commit `ref` names in `root`, or None if git resolves it to none."""
    if ref is None or ref == WORKING_COPY:
        return None
    try:
        return _oid(root, ref)
    except (KeyError, ValueError, pygit2.GitError):
        return None


def _side_commit(root: Path, from_rev: str, message: str) -> str:
    """A commit on a new branch `side`, off `from_rev`; not an ancestor of HEAD."""
    repo = pygit2.Repository(str(root))
    base = repo.revparse_single(from_rev).peel(pygit2.Commit)
    oid = repo.create_commit("refs/heads/side", _sig(50), _sig(50), message, base.tree_id, [base.id])
    return str(oid)


def _ambiguous_prefix(root: Path, kind: str) -> str:
    """Write objects (fixed contents) until two share a 4-hex prefix; return it."""
    repo = pygit2.Repository(str(root))
    head = repo.revparse_single("HEAD").peel(pygit2.Commit)
    seen: dict[str, str] = {}
    for i in range(20000):
        if kind == "commit":
            oid = repo.create_commit(None, _sig(i), _sig(i), f"filler {i}", head.tree_id, [head.id])
        else:
            oid = repo.create_blob(f"filler blob {i}\n".encode())
        prefix = str(oid)[:4]
        if prefix in seen:
            return prefix
        seen[prefix] = str(oid)
    raise AssertionError("setup: no 4-hex prefix collision found")


# ==========================================================================
# MELLON-VER-SRS-46: links added and removed
# ==========================================================================


def _relinked_repo(root: Path) -> tuple[str, str]:
    folder = _build(root)
    editor.add_link(folder, "SPEC-1", "SPEC", "SPEC-4", "refines")
    before = _commit(root, "link SPEC-1 to SPEC-4")
    assert editor.remove_link(folder, "SPEC-1", "SPEC", "SPEC-4"), "setup: link not removed"
    editor.add_link(folder, "SPEC-1", "SPEC", "SPEC-2", "refines")
    after = _commit(root, "relink SPEC-1 to SPEC-2")
    return before, after


# --- written blind from MELLON-VER-SRS (round 2) -----------------------------


DOC = "SPEC"


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


def _r2_sig(n: int) -> pygit2.Signature:
    """A fixed signature per commit number, so hashes repeat from run to run."""
    return pygit2.Signature("Blind Tester", "blind@example.invalid", 1_700_000_000 + 60 * n, 0)


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pygit2.init_repository(str(root), initial_head="main")


def _r2_commit(root: Path, message: str) -> str:
    """Commit every file under `root` on the current branch; return the full hash."""
    repo = pygit2.Repository(str(root))
    index = repo.index
    index.add_all()
    index.write()
    tree = index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    n = 0 if repo.head_is_unborn else sum(1 for _ in repo.walk(repo.head.target))
    oid = repo.create_commit("HEAD", _r2_sig(n), _r2_sig(n), message, tree, parents)
    return str(oid)


def _all_object_ids(repo: pygit2.Repository) -> set[str]:
    """Every object in the repository: commits, trees, blobs and tag objects."""
    seen: set[str] = set()

    def tree_walk(tree: pygit2.Tree) -> None:
        seen.add(str(tree.id))
        for entry in tree:
            if entry.type_str == "tree":
                tree_walk(repo[entry.id])
            else:
                seen.add(str(entry.id))

    for name in repo.references:
        ref = repo.references[name].resolve()
        obj = repo[ref.target]
        if obj.type_str == "tag":
            seen.add(str(obj.id))
            obj = obj.peel(pygit2.Commit)
        for commit in repo.walk(obj.id):
            seen.add(str(commit.id))
            tree_walk(commit.tree)
    return seen


def _build_rev_repo(root: Path) -> dict:
    """SPEC on `main`: commit Ck adds SPEC-k ("Add object k"), k = 0..5; HEAD = main = C5.

    Names: annotated tag `v1` and issue tag `SPEC-issue-1` on C1; lightweight
    tag `rel-2` and local branch `feature` on C2; remote-tracking branch
    `origin/review` on C3. Comparing From Ck with To C5 lists SPEC-(k+1)..SPEC-5
    as Added and nothing else, so the rows tell which commit From named.
    """
    _init(root)
    folder = editor.create_document(root, DOC, title="Revisions")
    ids, commits = [], []
    for k in range(6):
        ids.append(editor.create_object(folder, None, f"Object {k}."))
        commits.append(_r2_commit(root, f"Add object {k}"))
    repo = pygit2.Repository(str(root))
    oid = [pygit2.Oid(hex=h) for h in commits]
    git_ops.create_tag(repo, "v1", oid[1], "Version 1")
    git_ops.create_issue_tag(repo, DOC, "1", oid[1])
    repo.references.create("refs/tags/rel-2", oid[2])
    repo.references.create("refs/heads/feature", oid[2])
    repo.references.create("refs/remotes/origin/review", oid[3])
    everything = _all_object_ids(repo)
    prefix = None
    for k in (1, 2, 3, 4):
        p = commits[k][:4]
        if sum(1 for h in everything if h.startswith(p)) == 1:
            prefix = (p, k)
            break
    assert prefix, "test setup: no commit C1..C4 has a 4-digit prefix unique in the repository"
    v1_tag_object = str(repo.references["refs/tags/v1"].target)
    return {"ids": ids, "commits": commits, "prefix": prefix, "v1_tag_object": v1_tag_object}


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-22: differences gone in the canonical form are no change
# ---------------------------------------------------------------------------


FIGURE_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">\n'
    b'  <g id="layer">\n'
    b'    <rect x="0" y="0" width="40" height="20" fill="#ff0000"/>\n'
    b"  </g>\n"
    b"</svg>\n"
)


def _build_canon_repo(root: Path) -> dict:
    """SPEC at commit "seed", written by the editor: SPEC-0 (Heading) over SPEC-1
    (two-line description, rationale set but empty, a link to SPEC-0 with an
    acknowledged commit), SPEC-2 (text with < and &, a figure) and SPEC-3 (empty
    description). Files with empty elements written both as <x/> and <x></x>."""
    _init(root)
    folder = editor.create_document(root, DOC, title="Canonical forms")
    top = editor.create_object(folder, None, "Top heading.")
    editor.set_attribute(folder, top, "type", "Heading")
    one = editor.create_object(folder, top, "Line one.\nLine two.")
    editor.set_attribute(folder, one, "type", "Information")
    editor.set_attribute(folder, one, "rationale", "")
    two = editor.create_object(folder, top, "Speed < 10 & stop.")
    editor.set_attribute(folder, two, "type", "Information")
    three = editor.create_object(folder, top, "")
    editor.set_attribute(folder, three, "type", "Note")
    editor.add_link(folder, one, DOC, top, "refines")
    src = root.parent / "figure-src.svg"
    src.write_bytes(FIGURE_SVG)
    editor.set_figure(folder, two, src)
    c0 = _r2_commit(root, "objects")
    links = folder / "links" / f"{one}.xml"
    data = links.read_bytes()
    marker = b'type="refines"/>'
    assert marker in data, f"test setup: unexpected links file {data!r}"
    links.write_bytes(data.replace(marker, f'type="refines" ack="{c0}"/>'.encode()))
    c1 = _r2_commit(root, "seed")
    return {"ids": [top, one, two, three], "c0": c0, "c1": c1}


def _canonical(data: bytes) -> bytes:
    """The definition's canonical form, worked out independently: Canonical XML
    (lxml's C14N) with whitespace-only text between elements removed."""
    root = etree.fromstring(data, etree.XMLParser(strip_cdata=True, resolve_entities=False))
    for el in root.iter():
        if len(el) and el.text is not None and not el.text.strip(" \t\r\n"):
            el.text = None
        if el.tail is not None and not el.tail.strip(" \t\r\n"):
            el.tail = None
    return etree.tostring(root, method="c14n")


_TAG = re.compile(r"<([A-Za-z_][\w.:-]*)((?:\s+[\w.:-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))+)(\s*/?)>")
_ATTR = re.compile(r"([\w.:-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")


def _reverse_attributes(s: str) -> str:
    def one(m):
        attrs = _ATTR.findall(m.group(2))[::-1]
        return "<" + m.group(1) + "".join(f" {n}={v}" for n, v in attrs) + m.group(3) + ">"

    return _TAG.sub(one, s)


def _single_quotes(s: str) -> str:
    return re.sub(r'="([^"<>\']*)"', r"='\1'", s)


def _space_inside_tags(s: str) -> str:
    def one(m):
        attrs = _ATTR.findall(m.group(2))
        return "<" + m.group(1) + "".join(f"\n      {n} = {v}" for n, v in attrs) + " " + m.group(3).strip() + ">"

    s = _TAG.sub(one, s)
    s = re.sub(r"<([A-Za-z_][\w.:-]*)>", r"<\1 >", s)
    return re.sub(r"</([\w.:-]+)>", r"</\1  >", s)


def _expand_empty(s: str) -> str:
    return re.sub(r"<([A-Za-z_][\w.:-]*)([^<>]*?)\s*/>", r"<\1\2></\1>", s)


def _tabs(s: str) -> str:
    return re.sub(r"(?m)^( +)(?=<)", lambda m: "\t" * (len(m.group(1)) // 2), s)


def _blank_lines(s: str) -> str:
    return re.sub(r">\n([ \t]*)<", r">\n\n\1<", s)


def _no_declaration(s: str) -> str:
    return re.sub(r"^<\?xml[^>]*\?>\s*", "", s)


def _crlf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "\r\n")


def _char_refs(s: str) -> str:
    s = s.replace('"SPEC', '"&#83;PEC')
    s = s.replace(">Line one.", ">&#x4C;ine one.")
    return s.replace(">Top heading.", ">&#84;op heading.")


def _cdata(s: str) -> str:
    def one(m):
        text = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return f"<description><![CDATA[{text}]]></description>"

    return re.sub(r"<description>([^<]+)</description>", one, s)


def _everything(s: str) -> str:
    for step in (_cdata, _char_refs, _expand_empty, _reverse_attributes, _single_quotes,
                 _space_inside_tags, _tabs, _no_declaration, _blank_lines, _crlf):
        s = step(s)
    return s


def _rewrite(root: Path, pattern: str, rewrite: Callable[[str], str]) -> list[str]:
    """Rewrite each file matching `pattern` under SPEC/. Each rewrite must leave the
    file's canonical form as it was (checked here, so a mistake in this test can't
    pass for a finding). Returns the files changed; at least one must be."""
    changed = []
    for path in sorted((root / DOC).glob(pattern)):
        old = path.read_bytes()
        new = rewrite(old.decode("utf-8")).encode("utf-8")
        if new == old:
            continue
        assert _canonical(new) == _canonical(old), f"test error: rewrite changed the canonical form of {path}"
        path.write_bytes(new)
        changed.append(path.relative_to(root).as_posix())
    assert changed, f"test error: the rewrite changed no file matching {pattern}"
    return changed


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-56: comparing changes nothing but by saving pending edits
# ---------------------------------------------------------------------------


def _build_dirty_repo(root: Path) -> dict:
    """SPEC-0..SPEC-2 at "seed" (C1); "revise" (C2 = HEAD) changes SPEC-1. A branch
    `side` and an annotated tag `v1` on C1. Then a working copy that differs from
    the index, which differs from HEAD: SPEC-0 changed and not staged; SPEC-1
    changed, staged, and changed again; two untracked files. SPEC-2 is clean, for
    the pending edit."""
    _init(root)
    folder = editor.create_document(root, DOC, title="Dirty")
    ids = [editor.create_object(folder, None, f"Object {k}.") for k in range(3)]
    c1 = _r2_commit(root, "seed")
    editor.set_description(folder, ids[1], "Object 1, revised.")
    c2 = _r2_commit(root, "revise")
    repo = pygit2.Repository(str(root))
    repo.references.create("refs/heads/side", pygit2.Oid(hex=c1))
    git_ops.create_tag(repo, "v1", pygit2.Oid(hex=c1), "Version 1")
    editor.set_description(folder, ids[0], "Object 0, not committed.")
    editor.set_description(folder, ids[1], "Object 1, staged.")
    index = repo.index
    index.read()
    index.add(f"{DOC}/objects/{ids[1]}.xml")
    index.write()
    editor.set_description(folder, ids[1], "Object 1, staged, then edited again.")
    (root / "notes.txt").write_text("untracked at the root\n", encoding="utf-8")
    (folder / "scratch.txt").write_text("untracked in the document folder\n", encoding="utf-8")
    return {"ids": ids, "c1": c1, "c2": c2}


def _snapshot(root: Path) -> dict[str, dict]:
    """The three things -56 protects. The working copy: every file outside .git,
    tracked or not, by bytes and modified time. The git index: its entries (path,
    blob, mode); a rewrite of git's stat cache alone isn't a change to the index.
    The git references: HEAD, packed-refs, every file under refs/ and each other
    *_HEAD file, by bytes."""
    files = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel.split("/")[0] == ".git" or not p.is_file():
            continue
        files[rel] = (p.read_bytes(), p.stat().st_mtime_ns)
    repo = pygit2.Repository(str(root))
    index = {e.path: (str(e.id), e.mode) for e in repo.index}
    git = root / ".git"
    refs = {}
    for p in [git / "packed-refs", *git.glob("*HEAD"), *(git / "refs").rglob("*")]:
        if p.is_file():
            refs[p.relative_to(git).as_posix()] = p.read_bytes()
    return {"working copy": files, "git index": index, "git references": refs}


def _changed(before: dict, after: dict, part: str) -> set[str]:
    b, a = before[part], after[part]
    return {k for k in set(b) | set(a) if b.get(k) != a.get(k)}


PARTS = ["working copy", "git index", "git references"]


# ---------------------------------------------------------------------------
# MELLON-VER-SRS-67: each result reports the commit each version resolved to,
# "by at least the first 7 digits of its hash" (settled with the person on
# 2026-11-07). The tests accept any run of 7 to 40 hex digits that begins the
# right commit's hash. They don't tie a hash to its side, but each side's
# commit differs, so the other side's hash can't stand in for it.
#
# MELLON-VER-SRS-87: the MCP server's compare tool returns the full hash.
# ---------------------------------------------------------------------------


_HEX_RUN = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])")


def _shows_hash_of(text: str, full: str) -> bool:
    """True when `text` holds at least the first 7 digits of the hash `full`."""
    return any(full.startswith(run) for run in _HEX_RUN.findall(text.lower()))
