"""Blind tests for Compare: the comparison engine and the MCP compare tool.

Written from MELLON-VER-SRS, the definitions and the public signatures alone,
without reading the code under test or the existing tests. Every expected value
is worked out by hand from a requirement or a definition; the docstring of each
test names the requirement it checks.

Each test builds its own git repository under ``tmp_path`` with fixed commit
times, so a run repeats exactly.
"""

from __future__ import annotations

import asyncio
import html.parser
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pygit2
import pytest
from lxml import etree

from mellon import diff, editor, git_ops
from mellon.diff import Change
from mellon.schema import Table, TableCell, TableColumn, TableRow

DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'

SVG_1 = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    b'<rect width="20" height="10" fill="red"/></svg>\n'
)
SVG_2 = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
    b'<circle cx="10" cy="5" r="4" fill="blue"/></svg>\n'
)

BROKEN_OBJECT_1 = DECL + b'<object id="SPEC-1">\n  <description>Broken text\n'


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


def compare(r: GitRepo, ref_a: str, ref_b: str, doc_id: str = "SPEC") -> diff.DocumentDiff:
    return diff.compare_document(r.root, doc_id, ref_a, ref_b)


def change_for(d: diff.DocumentDiff, obj_id: str) -> diff.ObjectChange | None:
    found = [c for c in d.changes if c.obj_id == obj_id]
    assert len(found) <= 1, f"{obj_id} is listed {len(found)} times: {found}"
    return found[0] if found else None


def kinds_by_object(d: diff.DocumentDiff) -> dict[str, tuple[Change, ...]]:
    return {c.obj_id: tuple(c.kinds) for c in d.changes}


def moved_ids(d: diff.DocumentDiff) -> set[str]:
    return {c.obj_id for c in d.changes if Change.MOVED in c.kinds}


def not_well_formed(data: bytes) -> bool:
    try:
        etree.fromstring(data)
    except etree.XMLSyntaxError:
        return True
    return False


# --------------------------------------------------------------------------
# MELLON-VER-SRS-12, -13, -14: the versions the Compare dialog opens with
# --------------------------------------------------------------------------


def build_issue_repo(tmp_path: Path, n_commits: int) -> tuple[GitRepo, Path, list[str]]:
    """Commits c1..cn, each changing SPEC-0's description. OTHER is created in c1."""
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    obj = editor.create_object(f, None, "Version 1.")
    other = editor.create_document(r.root, "OTHER", title="Other")
    editor.create_object(other, None, "Other 1.")
    commits = [r.commit("c1")]
    for i in range(2, n_commits + 1):
        editor.set_description(f, obj, f"Version {i}.")
        commits.append(r.commit(f"c{i}"))
    return r, f, commits


def defaults(r: GitRepo) -> tuple[str | None, str | None, str | None]:
    """default_versions for SPEC as (from_ref, commit From names, whether To is the latest commit)."""
    from_ref, to_ref = diff.default_versions(r.root, "SPEC")
    from_commit = r.commit_of(from_ref) if from_ref is not None else None
    to_is_latest = to_ref != diff.WORKING_COPY and r.commit_of(to_ref) == str(r.repo.head.target)
    return from_ref, from_commit, to_is_latest


def test_ver12_from_is_current_issue_in_issue_order(tmp_path):
    """MELLON-VER-SRS-12: changed since its current issue, From = current issue, To = latest.

    Issues 1 on c1, 2.9 on c2, 2.10 on c3; c4 changes SPEC. In issue order 2.9
    comes before 2.10, so the current issue is 2.10 (on c3).
    """
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "1", c[0])
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    from_ref, from_commit, to_is_latest = defaults(r)
    assert (from_commit, to_is_latest) == (c[2], True), (
        f"expected From = SPEC-issue-2.10 ({c[2][:7]}) and To = latest commit, "
        f"got From {from_ref!r} ({(from_commit or '')[:7]}), To latest: {to_is_latest}"
    )


def test_ver12_issue_off_the_latest_commits_history_is_ignored(tmp_path):
    """MELLON-VER-SRS-12 (definition 'current issue'): an issue tag on a commit that isn't
    an ancestor of the latest commit isn't the current issue, even when it is higher."""
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "1", c[0])
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    side = r.side_commit("side", c[2], "SPEC/objects/SPEC-0.xml",
                         DECL + b'<object id="SPEC-0">\n  <description>Side.</description>\n</object>\n',
                         "side change")
    r.tag_issue("SPEC", "3", side)
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[2], (
        f"From should stay SPEC-issue-2.10 ({c[2][:7]}); SPEC-issue-3 is on a side branch. Got {from_ref!r}"
    )


def test_ver12_another_documents_issue_is_ignored(tmp_path):
    """MELLON-VER-SRS-12: OTHER-issue-5 on the latest commit isn't an issue of SPEC."""
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "1", c[0])
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    r.tag_issue("OTHER", "5", c[3])
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[2], (
        f"From should stay SPEC-issue-2.10 ({c[2][:7]}), not OTHER's issue; got {from_ref!r}"
    )


def test_ver12_shorter_issue_number_comes_first(tmp_path):
    """MELLON-VER-SRS-12 (definition 'issue order'): 2 comes before 2.0, so with issues
    2 on c1 and 2.0 on c2 and SPEC changed since, From = SPEC-issue-2.0."""
    r, _, c = build_issue_repo(tmp_path, 4)
    r.tag_issue("SPEC", "2", c[0])
    r.tag_issue("SPEC", "2.0", c[1])
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[1], f"From should be SPEC-issue-2.0 ({c[1][:7]}), got {from_ref!r}"


def test_ver13_unchanged_since_current_issue_from_is_issue_before(tmp_path):
    """MELLON-VER-SRS-13: issues 2.9 on c2 and 2.10 on the latest commit, SPEC unchanged
    since 2.10: From = the issue before 2.10, which is 2.9; To = latest commit."""
    r, _, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    from_ref, from_commit, to_is_latest = defaults(r)
    assert (from_commit, to_is_latest) == (c[1], True), (
        f"expected From = SPEC-issue-2.9 ({c[1][:7]}) and To = latest commit, "
        f"got From {from_ref!r}, To latest: {to_is_latest}"
    )


def test_ver13_issue_before_ignores_issue_off_the_history(tmp_path):
    """MELLON-VER-SRS-13 (definition 'issue before'): SPEC-issue-2.9.1 on a side-branch
    commit comes between 2.9 and 2.10 in issue order but isn't an ancestor of the
    latest commit, so From stays 2.9."""
    r, _, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    side = r.side_commit("side", c[1], "SPEC/objects/SPEC-0.xml",
                         DECL + b'<object id="SPEC-0">\n  <description>Side.</description>\n</object>\n',
                         "side change")
    r.tag_issue("SPEC", "2.9.1", side)
    r.tag_issue("SPEC", "2.10", c[2])
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[1], f"From should stay SPEC-issue-2.9 ({c[1][:7]}), got {from_ref!r}"


def test_ver13_change_then_byte_for_byte_restore_is_unchanged(tmp_path):
    """MELLON-VER-SRS-13 (definition 'changed since an issue'): after 2.10, a commit
    changing SPEC-0 and a commit restoring its file byte for byte leave SPEC unchanged
    since 2.10, so From = 2.9 and To = latest commit."""
    r, f, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    original = r.read("SPEC/objects/SPEC-0.xml")
    editor.set_description(f, "SPEC-0", "Changed in between.")
    r.commit("c4 change")
    r.write("SPEC/objects/SPEC-0.xml", original)
    r.commit("c5 restore")
    from_ref, from_commit, to_is_latest = defaults(r)
    assert (from_commit, to_is_latest) == (c[1], True), (
        f"expected From = SPEC-issue-2.9 ({c[1][:7]}), To = latest; got From {from_ref!r}, To latest: {to_is_latest}"
    )


def test_ver13_commit_to_another_document_leaves_spec_unchanged(tmp_path):
    """MELLON-VER-SRS-13 (rationale: a commit to another document doesn't change this
    one): after 2.10 only OTHER changes, so From = 2.9."""
    r, _, c = build_issue_repo(tmp_path, 3)
    r.tag_issue("SPEC", "2.9", c[1])
    r.tag_issue("SPEC", "2.10", c[2])
    editor.set_description(r.root / "OTHER", "OTHER-0", "Other changed.")
    r.commit("c4 other only")
    from_ref, from_commit, _ = defaults(r)
    assert from_commit == c[1], f"From should be SPEC-issue-2.9 ({c[1][:7]}), got {from_ref!r}"


def test_ver14_only_issue_on_unchanged_document_leaves_from_unset(tmp_path):
    """MELLON-VER-SRS-14 (boundary of -13): the only issue is the one the document still
    is, so there is no issue before; From is unset and To is the latest commit."""
    r, _, c = build_issue_repo(tmp_path, 2)
    r.tag_issue("SPEC", "1", c[1])
    from_ref, _, to_is_latest = defaults(r)
    assert (from_ref, to_is_latest) == (None, True), (
        f"expected From unset and To = latest commit, got From {from_ref!r}, To latest: {to_is_latest}"
    )


# --------------------------------------------------------------------------
# MELLON-VER-SRS-19: Added follows the object file, not the outline
# --------------------------------------------------------------------------


def test_ver19_object_file_without_outline_entry_is_added(tmp_path):
    """MELLON-VER-SRS-19 (definition 'doesn't exist at a version'): an object file
    SPEC-9.xml committed with no outline entry exists at To, so SPEC-9 is Added."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-9.xml",
            DECL + b'<object id="SPEC-9">\n  <attr name="type">Requirement</attr>\n'
                   b'  <description>Orphan.</description>\n</object>\n')
    c2 = r.commit("orphan object file")
    change = change_for(compare(r, c1, c2), "SPEC-9")
    assert change is not None and tuple(change.kinds) == (Change.ADDED,), (
        f"SPEC-9 should be exactly Added, got {change and change.kinds}"
    )


def test_ver19_outline_entry_without_object_file_is_not_added(tmp_path):
    """MELLON-VER-SRS-19 (definition 'doesn't exist at a version'): an outline entry for
    SPEC-10 with no object file means SPEC-10 doesn't exist at To, so it isn't Added."""
    r, _, c1 = build_spec(tmp_path)
    manifest = r.read("SPEC/SPEC.xml")
    assert b"  </outline>" in manifest, "setup: manifest outline not found"
    r.write("SPEC/SPEC.xml", manifest.replace(b"  </outline>", b'    <ref id="SPEC-10"/>\n  </outline>'))
    c2 = r.commit("dangling outline entry")
    change = change_for(compare(r, c1, c2), "SPEC-10")
    assert change is None or Change.ADDED not in change.kinds, (
        f"SPEC-10 has no object file at To and mustn't be Added, got {change.kinds}"
    )


# --------------------------------------------------------------------------
# MELLON-VER-SRS-21: each field alone makes an object Modified
# --------------------------------------------------------------------------


def _modified_only(r: GitRepo, c1: str, c2: str, obj_id: str) -> diff.ObjectChange:
    change = change_for(compare(r, c1, c2), obj_id)
    assert change is not None, f"{obj_id} isn't listed as changed"
    assert tuple(change.kinds) == (Change.MODIFIED,), f"{obj_id} should be exactly Modified, got {change.kinds}"
    assert change.delta is not None, f"{obj_id} is Modified but carries no field changes"
    return change


def test_ver21_attribute_value_changed(tmp_path):
    """MELLON-VER-SRS-21: status Draft -> Approved makes SPEC-1 Modified, reporting the attribute."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_attribute(f, "SPEC-1", "status", "Approved")
    c2 = r.commit("approve")
    change = _modified_only(r, c1, c2, "SPEC-1")
    attrs = {(a.name, a.value_a, a.value_b) for a in change.delta.attrs}
    assert ("status", "Draft", "Approved") in attrs, f"status Draft -> Approved not reported: {attrs}"


def test_ver21_attribute_unset_at_to(tmp_path):
    """MELLON-VER-SRS-21: the rationale attribute removed from SPEC-1's file (not set at
    To) makes SPEC-1 Modified, with rationale 'why' -> not set."""
    r, _, c1 = build_spec(tmp_path)
    data = r.read("SPEC/objects/SPEC-1.xml")
    line = b'  <attr name="rationale">why</attr>\n'
    assert line in data, "setup: rationale line not found"
    r.write("SPEC/objects/SPEC-1.xml", data.replace(line, b""))
    c2 = r.commit("unset rationale")
    change = _modified_only(r, c1, c2, "SPEC-1")
    attrs = {(a.name, a.value_a, a.value_b) for a in change.delta.attrs}
    assert ("rationale", "why", None) in attrs, f"rationale 'why' -> not set not reported: {attrs}"


def test_ver21_link_added(tmp_path):
    """MELLON-VER-SRS-21: a new outgoing link SPEC-1 -refines-> SPEC-4 makes SPEC-1 Modified."""
    r, f, c1 = build_spec(tmp_path)
    editor.add_link(f, "SPEC-1", "SPEC", "SPEC-4", "refines")
    c2 = r.commit("add link")
    change = _modified_only(r, c1, c2, "SPEC-1")
    added = {(lk.target_doc, lk.target_id, lk.type) for lk in change.delta.links.added}
    assert added == {("SPEC", "SPEC-4", "refines")}, f"added links reported: {added}"


def test_ver21_link_removed(tmp_path):
    """MELLON-VER-SRS-21: removing the link SPEC-1 -satisfies-> SPEC-2 makes SPEC-1 Modified."""
    r, f, c1 = build_spec(tmp_path)
    assert editor.remove_link(f, "SPEC-1", "SPEC", "SPEC-2"), "setup: link not removed"
    c2 = r.commit("remove link")
    change = _modified_only(r, c1, c2, "SPEC-1")
    removed = {(lk.target_doc, lk.target_id, lk.type) for lk in change.delta.links.removed}
    assert removed == {("SPEC", "SPEC-2", "satisfies")}, f"removed links reported: {removed}"


def test_ver21_link_type_changed_same_target(tmp_path):
    """MELLON-VER-SRS-21 (definition 'field': the outgoing links, each with its target and
    link type): satisfies -> refines to the same target, same ack, is Modified, not
    Link reviewed, and the old and the new link type are both reported."""
    r, _, c1 = build_spec(tmp_path)
    data = r.read("SPEC/links/SPEC-1.xml")
    assert data.count(b'type="satisfies"') == 1, "setup: link type not found"
    r.write("SPEC/links/SPEC-1.xml", data.replace(b'type="satisfies"', b'type="refines"'))
    c2 = r.commit("change link type")
    change = _modified_only(r, c1, c2, "SPEC-1")
    links = change.delta.links
    seen = ({(lk.target_id, lk.type) for lk in links.removed}, {(lk.target_id, lk.type) for lk in links.added})
    assert seen == ({("SPEC-2", "satisfies")}, {("SPEC-2", "refines")}), (
        f"expected removed satisfies and added refines to SPEC-2, got removed/added {seen}"
    )


def test_ver21_table_cell_changed(tmp_path):
    """MELLON-VER-SRS-21: one table cell 10 -> 20, with a table at both, makes SPEC-3 Modified."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_table(f, "SPEC-3", _table("20"))
    c2 = r.commit("table cell")
    change = _modified_only(r, c1, c2, "SPEC-3")
    t = change.delta.table
    assert t is not None and (t.table_a, t.table_b) == (_table("10"), _table("20")), (
        f"table change not reported as (speed,10) -> (speed,20): {t}"
    )


def test_ver21_table_removed(tmp_path):
    """MELLON-VER-SRS-21: removing SPEC-3's table makes it Modified, with no table at To."""
    r, f, c1 = build_spec(tmp_path)
    assert editor.clear_table(f, "SPEC-3"), "setup: table not removed"
    c2 = r.commit("table removed")
    change = _modified_only(r, c1, c2, "SPEC-3")
    t = change.delta.table
    assert t is not None and (t.table_a, t.table_b) == (_table("10"), None), f"table removal not reported: {t}"


def test_ver21_figure_replaced(tmp_path):
    """MELLON-VER-SRS-21: replacing SPEC-4's figure by a different SVG makes it Modified."""
    r, f, c1 = build_spec(tmp_path)
    svg = tmp_path / "fig2.svg"
    svg.write_bytes(SVG_2)
    editor.set_figure(f, "SPEC-4", svg)
    c2 = r.commit("figure replaced")
    change = _modified_only(r, c1, c2, "SPEC-4")
    fig = change.delta.figure
    assert fig is not None and (fig.svg_a, fig.svg_b) == (SVG_1, SVG_2), f"figure change not reported: {fig}"


def test_ver21_figure_removed(tmp_path):
    """MELLON-VER-SRS-21: removing SPEC-4's figure makes it Modified, with no figure at To."""
    r, f, c1 = build_spec(tmp_path)
    assert editor.clear_figure(f, "SPEC-4"), "setup: figure not removed"
    c2 = r.commit("figure removed")
    change = _modified_only(r, c1, c2, "SPEC-4")
    fig = change.delta.figure
    assert fig is not None and (fig.svg_a, fig.svg_b) == (SVG_1, None), f"figure removal not reported: {fig}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-22: line endings, indentation and attribute order are no change
# --------------------------------------------------------------------------


def _reserialize(data: bytes, indent: str, reorder=None) -> bytes:
    """`data` re-indented with `indent`, with each element's XML attributes reordered by
    `reorder(element)`; the declaration and the text inside elements are kept."""
    root = etree.fromstring(data)
    if reorder is not None:
        for el in root.iter():
            items = reorder(el, list(el.attrib.items()))
            for key in list(el.attrib):
                del el.attrib[key]
            for key, value in items:
                el.set(key, value)
    etree.indent(root, space=indent)
    return DECL + etree.tostring(root, encoding="UTF-8", xml_declaration=False) + b"\n"


@pytest.mark.parametrize("indent", ["    ", "\t"], ids=["four-spaces", "tab"])
def test_ver22_reindented_object_files_are_no_change(tmp_path, indent):
    """MELLON-VER-SRS-22: every object file of SPEC re-indented (4 spaces, or a tab), the
    text inside elements unchanged: no object changed and none Moved."""
    r, _, c1 = build_spec(tmp_path)
    for path in sorted((r.root / "SPEC" / "objects").glob("*.xml")):
        old = path.read_bytes()
        new = _reserialize(old, indent)
        assert new != old, f"setup: {path.name} unchanged by re-indenting"
        path.write_bytes(new)
    c2 = r.commit("re-indent objects")
    d = compare(r, c1, c2)
    assert d.changes == (), f"re-indenting must be no change, got {kinds_by_object(d)}"


def test_ver22_reordered_link_attributes_are_no_change(tmp_path):
    """MELLON-VER-SRS-22: a <link> with its XML attributes in the order type, target-id,
    target-doc, ack is no change."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", c1[:7])]))
    c2 = r.commit("explicit ack")
    order = ["type", "target-id", "target-doc", "ack"]
    old = r.read("SPEC/links/SPEC-1.xml")
    new = _reserialize(old, "  ", lambda el, items: sorted(items, key=lambda kv: order.index(kv[0]))
                       if el.tag == "link" else items)
    assert b'<link type="satisfies" target-id="SPEC-2" target-doc="SPEC" ack=' in new, "setup: not reordered"
    r.write("SPEC/links/SPEC-1.xml", new)
    c3 = r.commit("reorder link attributes")
    d = compare(r, c2, c3)
    assert d.changes == (), f"reordering XML attributes must be no change, got {kinds_by_object(d)}"


def test_ver22_reindented_manifest_with_reordered_attributes_is_no_change(tmp_path):
    """MELLON-VER-SRS-22: the manifest re-indented with 4 spaces and the XML attributes of
    each <attribute> element reversed: no object changed and none Moved. (A <ref> has
    only its id attribute, so there is nothing to reorder on it.)"""
    r, _, c1 = build_spec(tmp_path)
    old = r.read("SPEC/SPEC.xml")
    new = _reserialize(old, "    ", lambda el, items: list(reversed(items)) if el.tag == "attribute" else items)
    assert b'<attribute label="Status" required="false" kind="enum" name="status">' in new, "setup: not reordered"
    r.write("SPEC/SPEC.xml", new)
    c2 = r.commit("re-indent manifest")
    d = compare(r, c1, c2)
    assert d.changes == (), f"re-indenting the manifest must be no change, got {kinds_by_object(d)}"


@pytest.mark.parametrize(
    "rel", ["SPEC/objects/SPEC-1.xml", "SPEC/links/SPEC-1.xml", "SPEC/SPEC.xml"],
    ids=["object-file", "links-file", "manifest"],
)
def test_ver22_crlf_line_endings_are_no_change(tmp_path, rel):
    """MELLON-VER-SRS-22: a file committed with CRLF line endings instead of LF is no change."""
    r, _, c1 = build_spec(tmp_path)
    r.repo.config["core.autocrlf"] = "false"
    data = r.read(rel)
    assert b"\r\n" not in data, "setup: file already has CRLF"
    r.write(rel, data.replace(b"\n", b"\r\n"))
    c2 = r.commit("CRLF")
    blob = r.repo.revparse_single(f"{c2}:{rel}")
    assert b"\r\n" in blob.data, "setup: git didn't keep the CRLF line endings"
    d = compare(r, c1, c2)
    assert d.changes == (), f"CRLF line endings in {rel} must be no change, got {kinds_by_object(d)}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-23: Moved
# --------------------------------------------------------------------------


def test_ver23_swapped_siblings_only_the_later_at_from_is_moved(tmp_path):
    """MELLON-VER-SRS-23 (definition 'Moved', tie rule): under SPEC-0, SPEC-1 SPEC-2 SPEC-3
    becomes SPEC-2 SPEC-1 SPEC-3. The largest same-order sets {1,3} and {2,3} are equally
    large, so SPEC-2, the one that comes later at From, is the only one Moved."""
    r, f, c1 = build_spec(tmp_path)
    editor.move_object(f, "SPEC-2", before="SPEC-1")
    c2 = r.commit("swap")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-2"}, f"expected only SPEC-2 Moved, got {sorted(moved_ids(d))}"


def test_ver23_tie_between_pairs_moves_the_later_pair(tmp_path):
    """MELLON-VER-SRS-23 (definition 'Moved', tie rule): A B C D -> C D A B. The largest
    same-order sets {A,B} and {C,D} are equally large; C and D come later at From, so
    exactly C and D are Moved."""
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    head = editor.create_object(f, None, "Heading")
    kids = [editor.create_object(f, head, f"Item {i}.") for i in range(4)]
    assert kids == ["SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"], f"setup: unexpected ids {kids}"
    c1 = r.commit("seed")
    editor.move_object(f, "SPEC-3", before="SPEC-1")
    editor.move_object(f, "SPEC-4", before="SPEC-1")
    c2 = r.commit("rotate")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-3", "SPEC-4"}, f"expected SPEC-3 and SPEC-4 Moved, got {sorted(moved_ids(d))}"


def test_ver23_largest_same_order_set_is_kept(tmp_path):
    """MELLON-VER-SRS-23 (definition 'Moved'): A B C D -> B D C A. The same-order pairs
    are {B,C} and {B,D}; no three keep their order, and A pairs with none. So two
    siblings stay; of the tie, D comes later at From and is Moved: exactly A and D."""
    r = GitRepo(tmp_path / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    head = editor.create_object(f, None, "Heading")
    kids = [editor.create_object(f, head, f"Item {i}.") for i in range(4)]
    assert kids == ["SPEC-1", "SPEC-2", "SPEC-3", "SPEC-4"], f"setup: unexpected ids {kids}"
    c1 = r.commit("seed")
    editor.move_object(f, "SPEC-1", after="SPEC-4")
    editor.move_object(f, "SPEC-4", before="SPEC-3")
    c2 = r.commit("reorder")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-1", "SPEC-4"}, f"expected SPEC-1 and SPEC-4 Moved, got {sorted(moved_ids(d))}"


def test_ver23_heading_moved_with_sub_objects_moves_only_the_heading(tmp_path):
    """MELLON-VER-SRS-23: SPEC-0 moved, with SPEC-1..3 below it, under SPEC-4. SPEC-0's
    parent changed; its sub-objects keep their parent and order: only SPEC-0 is Moved."""
    r, f, c1 = build_spec(tmp_path)
    editor.move_object(f, "SPEC-0", parent_id="SPEC-4")
    c2 = r.commit("move heading")
    d = compare(r, c1, c2)
    assert moved_ids(d) == {"SPEC-0"}, f"expected only SPEC-0 Moved, got {sorted(moved_ids(d))}"


def test_ver23_moved_together_with_link_reviewed(tmp_path):
    """MELLON-VER-SRS-23 (Moved goes with Link reviewed): SPEC-2 moved to the top level and
    only its link's acknowledged commit changed: its changes are exactly Moved and
    Link reviewed."""
    r, f, c1 = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-2.xml", links_xml("SPEC-2", [("SPEC", "SPEC-4", "satisfies", c1[:7])]))
    c2 = r.commit("link on SPEC-2")
    editor.move_object(f, "SPEC-2", to_top=True)
    r.write("SPEC/links/SPEC-2.xml", links_xml("SPEC-2", [("SPEC", "SPEC-4", "satisfies", c2[:7])]))
    c3 = r.commit("move and review")
    change = change_for(compare(r, c2, c3), "SPEC-2")
    kinds = tuple(change.kinds) if change else ()
    assert sorted(kinds) == sorted((Change.MOVED, Change.LINK_REVIEWED)), (
        f"SPEC-2 should be exactly Moved and Link reviewed, got {kinds}"
    )


# Generated inputs (T11): many cases of up to 8 siblings, each under its own heading
# of one document, permuted, with some siblings deleted and some inserted.

GEN_CASES = 300
GEN_SEED = 20261106


def _lcs_len(xs: list[str], ys: list[str]) -> int:
    prev = [0] * (len(ys) + 1)
    for x in xs:
        cur = [0]
        for j, y in enumerate(ys):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def _gen_manifest(outline: list[tuple[str, list[str]]]) -> bytes:
    lines = [
        '<document id="GEN">', "  <metadata>", "    <title>Generated</title>", "  </metadata>",
        "  <attributes>", '    <attribute name="type" kind="enum" required="true">',
        "      <option>Heading</option>", "      <option>Information</option>", "    </attribute>",
        "  </attributes>", "  <outline>",
    ]
    for head, kids in outline:
        if kids:
            lines.append(f'    <ref id="{head}">')
            lines += [f'      <ref id="{k}"/>' for k in kids]
            lines.append("    </ref>")
        else:
            lines.append(f'    <ref id="{head}"/>')
    lines += ["  </outline>", "</document>", ""]
    return DECL + "\n".join(lines).encode("utf-8")


def _gen_object(obj_id: str, kind: str) -> bytes:
    return DECL + (
        f'<object id="{obj_id}">\n  <attr name="type">{kind}</attr>\n'
        f"  <description>Text of {obj_id}.</description>\n</object>\n"
    ).encode("utf-8")


@pytest.fixture(scope="module")
def generated_moves(tmp_path_factory):
    """Returns (cases, kinds by object). Each case is a dict with the heading, the
    siblings at From and at To, the kept siblings in From and in To order, and whether
    the kept siblings were left in order (only insertions and deletions)."""
    rng = random.Random(GEN_SEED)
    cases = []
    next_id = 1000
    for k in range(GEN_CASES):
        n = rng.randint(1, 8)
        before = [f"GEN-{next_id + i}" for i in range(n)]
        next_id += n
        kept = [x for x in before if rng.random() >= 0.2]
        in_order = rng.random() < 0.25
        kept_to = kept[:]
        if not in_order:
            rng.shuffle(kept_to)
        inserted = [f"GEN-{next_id + i}" for i in range(rng.randint(0, 3))]
        next_id += len(inserted)
        after = kept_to[:]
        for x in inserted:
            after.insert(rng.randint(0, len(after)), x)
        cases.append({"head": f"GEN-{k}", "from": before, "to": after,
                      "kept_from": kept, "kept_to": kept_to, "in_order": in_order})

    def files(side: str) -> dict[str, bytes]:
        out = {"GEN/GEN.xml": _gen_manifest([(c["head"], c[side]) for c in cases])}
        for c in cases:
            out[f"GEN/objects/{c['head']}.xml"] = _gen_object(c["head"], "Heading")
            for x in c[side]:
                out[f"GEN/objects/{x}.xml"] = _gen_object(x, "Information")
        return out

    r = GitRepo(tmp_path_factory.mktemp("generated") / "repo")
    c1, c2 = r.commit_snapshots([("from", files("from")), ("to", files("to"))])
    in_tree = {e.name for e in r.repo[pygit2.Oid(hex=c2)].peel(pygit2.Commit).tree["GEN"]["objects"]}
    expected = {f"{x}.xml" for c in cases for x in [c["head"], *c["to"]]}
    assert in_tree == expected, "setup: the To commit doesn't hold the expected object files"
    return cases, kinds_by_object(compare(r, c1, c2, doc_id="GEN"))


def test_ver23_generated_moved_count_is_common_minus_lcs(generated_moves):
    """MELLON-VER-SRS-23 (definition 'Moved'), generated: in each case the number of Moved
    siblings is the number present at both versions minus the length of their longest
    common subsequence (the largest set whose order is the same at From and at To)."""
    cases, kinds = generated_moves
    bad = []
    for c in cases:
        moved = [x for x in c["kept_from"] if Change.MOVED in kinds.get(x, ())]
        expected = len(c["kept_from"]) - _lcs_len(c["kept_from"], c["kept_to"])
        if len(moved) != expected:
            bad.append(f"{c['from']} -> {c['to']}: Moved {moved}, expected {expected} Moved")
    assert not bad, f"{len(bad)} of {len(cases)} cases wrong, e.g.:\n" + "\n".join(bad[:5])


def test_ver23_generated_unmoved_siblings_keep_their_order(generated_moves):
    """MELLON-VER-SRS-23 (definition 'Moved'), generated: the siblings present at both
    versions that aren't Moved are in the same order at From and at To."""
    cases, kinds = generated_moves
    bad = []
    for c in cases:
        stay_from = [x for x in c["kept_from"] if Change.MOVED not in kinds.get(x, ())]
        stay_to = [x for x in c["kept_to"] if Change.MOVED not in kinds.get(x, ())]
        if stay_from != stay_to:
            bad.append(f"{c['from']} -> {c['to']}: not Moved {stay_from} at From but {stay_to} at To")
    assert not bad, f"{len(bad)} of {len(cases)} cases wrong, e.g.:\n" + "\n".join(bad[:5])


def test_ver23_generated_insertions_and_deletions_move_nothing(generated_moves):
    """MELLON-VER-SRS-23 (definition: inserting or deleting a sibling moves nothing),
    generated: in the cases with only insertions and deletions, nothing is Moved."""
    cases, kinds = generated_moves
    only = [c for c in cases if c["in_order"]]
    assert len(only) >= 30, f"setup: only {len(only)} insertion/deletion cases"
    bad = []
    for c in only:
        moved = sorted(x for x in set(c["from"]) | set(c["to"]) if Change.MOVED in kinds.get(x, ()))
        if moved:
            bad.append(f"{c['from']} -> {c['to']}: Moved {moved}")
    assert not bad, f"{len(bad)} of {len(only)} cases wrong, e.g.:\n" + "\n".join(bad[:5])


# --------------------------------------------------------------------------
# MELLON-VER-SRS-24: Link reviewed
# --------------------------------------------------------------------------


def test_ver24_first_acknowledgement_is_link_reviewed(tmp_path):
    """MELLON-VER-SRS-24: a link with no acknowledged commit at From that gets one at To,
    nothing else changed: SPEC-1 is exactly Link reviewed."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-4", "refines", None)]))
    c2 = r.commit("no ack")
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-4", "refines", c2[:7])]))
    c3 = r.commit("first ack")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.LINK_REVIEWED,), (
        f"SPEC-1 should be exactly Link reviewed, got {change and change.kinds}"
    )


def test_ver24_ack_and_description_changed_together_is_modified(tmp_path):
    """MELLON-VER-SRS-24 (definition 'Link reviewed': its ONLY change is to the acks):
    the ack and the description changed in one commit: SPEC-1 is exactly Modified."""
    r, f, _ = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", "1111111")]))
    c2 = r.commit("ack 1")
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", [("SPEC", "SPEC-2", "satisfies", c2[:7])]))
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    c3 = r.commit("ack 2 and text")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.MODIFIED,), (
        f"SPEC-1 should be exactly Modified, got {change and change.kinds}"
    )


def test_ver24_two_links_reacknowledged_is_one_link_reviewed_change(tmp_path):
    """MELLON-VER-SRS-24 (and -53's data): both of SPEC-1's links re-acknowledged: one
    Link reviewed change for SPEC-1 that lists both links with their ack at From and To."""
    r, _, _ = build_spec(tmp_path)
    links_a = [("SPEC", "SPEC-2", "satisfies", "1111111"), ("SPEC", "SPEC-4", "refines", "2222222")]
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", links_a))
    c2 = r.commit("acks at From")
    links_b = [("SPEC", "SPEC-2", "satisfies", c2[:7]), ("SPEC", "SPEC-4", "refines", c2[:7])]
    r.write("SPEC/links/SPEC-1.xml", links_xml("SPEC-1", links_b))
    c3 = r.commit("acks at To")
    d = compare(r, c2, c3)
    change = change_for(d, "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.LINK_REVIEWED,), (
        f"SPEC-1 should be listed once, exactly Link reviewed, got {change and change.kinds}"
    )
    acks = {(a.target_doc, a.target_id, a.type, a.ack_a, a.ack_b) for a in change.delta.links.ack_changed}
    assert acks == {("SPEC", "SPEC-2", "satisfies", "1111111", c2[:7]),
                    ("SPEC", "SPEC-4", "refines", "2222222", c2[:7])}, f"reviewed links listed: {acks}"


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


def test_ver26_error_names_document_and_both_hashes(tmp_path):
    """MELLON-VER-SRS-26: SPEC between c1 and c2 (given as 7-hex hashes) in a repository
    holding only OTHER: an error naming SPEC and both versions as given."""
    r, c1, c2 = build_other_only(tmp_path)
    with pytest.raises(Exception) as info:
        compare(r, c1[:7], c2[:7])
    message = str(info.value)
    missing = [w for w in ("SPEC", c1[:7], c2[:7]) if w not in message]
    assert not missing, f"error {message!r} doesn't name {missing}"


def test_ver26_error_names_document_and_both_names(tmp_path):
    """MELLON-VER-SRS-26: SPEC between the tag 'rel-a' and the branch 'next' in a
    repository holding only OTHER: an error naming SPEC, 'rel-a' and 'next'."""
    r, c1, c2 = build_other_only(tmp_path)
    repo = r.repo
    git_ops.create_tag(repo, "rel-a", pygit2.Oid(hex=c1), "release a")
    repo.branches.local.create("next", repo[pygit2.Oid(hex=c2)])
    with pytest.raises(Exception) as info:
        compare(r, "rel-a", "next")
    message = str(info.value)
    missing = [w for w in ("SPEC", "rel-a", "next") if w not in message]
    assert not missing, f"error {message!r} doesn't name {missing}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-27, -28: unreadable files
# --------------------------------------------------------------------------


def build_broken_at_from(tmp_path: Path) -> tuple[GitRepo, Path, str, str]:
    """c2: SPEC-1's object file not well-formed. c3: SPEC-1 valid again with the text
    'Fixed text.', and SPEC-2's description changed to 'Requirement 2, changed.'."""
    r, f, _ = build_spec(tmp_path)
    good = r.read("SPEC/objects/SPEC-1.xml")
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("break SPEC-1")
    r.write("SPEC/objects/SPEC-1.xml", good)
    editor.set_description(f, "SPEC-1", "Fixed text.")
    editor.set_description(f, "SPEC-2", "Requirement 2, changed.")
    c3 = r.commit("fix SPEC-1, change SPEC-2")
    return r, f, c2, c3


def test_ver27_object_file_broken_at_from_is_unreadable_at_from(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1's file isn't well-formed at From: SPEC-1 is reported
    unreadable at From and readable at To."""
    r, _, c2, c3 = build_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None, "SPEC-1, unreadable at From, isn't listed"
    err_a, err_b = change.errors
    assert (err_a is not None, err_b is None) == (True, True), (
        f"expected unreadable at From only, got errors {change.errors}"
    )


def test_ver27_object_broken_at_from_has_its_fields_at_to(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1, unreadable at From, still has its fields at To."""
    r, _, c2, c3 = build_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-1")
    obj = change.after.obj if change and change.after else None
    assert obj is not None and obj.description == "Fixed text.", f"SPEC-1's fields at To not available: {obj}"


def test_ver28_other_objects_still_reported_when_one_is_unreadable(tmp_path):
    """MELLON-VER-SRS-28: with SPEC-1 unreadable at From, SPEC-2's change is still
    reported: exactly Modified, description 'Requirement 2.' -> 'Requirement 2, changed.'."""
    r, _, c2, c3 = build_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-2")
    got = (tuple(change.kinds), change.delta.description and
           (change.delta.description.text_a, change.delta.description.text_b)) if change else None
    assert got == ((Change.MODIFIED,), ("Requirement 2.", "Requirement 2, changed.")), (
        f"SPEC-2's change isn't reported as expected: {got}"
    )


def test_ver27_object_file_broken_differently_at_both_is_unreadable_at_both(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1's file isn't well-formed at From nor (differently) at To:
    it is reported unreadable at both."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("break SPEC-1")
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1 + b"  <attr name=\n")
    c3 = r.commit("break SPEC-1 differently")
    change = change_for(compare(r, c2, c3), "SPEC-1")
    errors = change.errors if change else None
    assert errors is not None and None not in errors, f"expected unreadable at both versions, got {errors}"


def test_ver27_object_file_broken_identically_at_both_isnt_listed(tmp_path):
    """MELLON-VER-SRS-27: 'If an object's file or links file can't be read at From or at
    To, and its content isn't the same at both, ...' SPEC-1's file is broken with the
    same bytes at both, and only SPEC-2 changes: SPEC-1 isn't listed."""
    r, _, _ = build_spec(tmp_path)
    r.write("SPEC/objects/SPEC-1.xml", BROKEN_OBJECT_1)
    c2 = r.commit("break SPEC-1")
    # By hand: the editor can't load SPEC while SPEC-1's file is broken.
    data = r.read("SPEC/objects/SPEC-2.xml")
    old = b"<description>Requirement 2.</description>"
    assert old in data, "setup: SPEC-2 description not found"
    r.write("SPEC/objects/SPEC-2.xml", data.replace(old, b"<description>Requirement 2, changed.</description>"))
    c3 = r.commit("change SPEC-2 only")
    d = compare(r, c2, c3)
    assert change_for(d, "SPEC-1") is None, f"SPEC-1 is the same at both and shouldn't be listed: {change_for(d, 'SPEC-1')}"
    assert change_for(d, "SPEC-2") is not None, "setup: SPEC-2's change should be listed"


def test_ver27_links_file_broken_at_to_is_unreadable_at_to(tmp_path):
    """MELLON-VER-SRS-27: SPEC-1's links file isn't well-formed at To: SPEC-1 is
    reported unreadable at To."""
    r, _, c1 = build_spec(tmp_path)
    r.write("SPEC/links/SPEC-1.xml", DECL + b'<links source="SPEC-1">\n  <link target-doc="SPEC"\n')
    c2 = r.commit("break links")
    change = change_for(compare(r, c1, c2), "SPEC-1")
    errors = change.errors if change else None
    assert errors is not None and errors[0] is None and errors[1] is not None, (
        f"expected SPEC-1 unreadable at To (its links), got {errors}"
    )


def test_ver21_figure_not_well_formed_at_to_is_still_reported(tmp_path):
    """MELLON-VER-SRS-21 with -27: SPEC-4's figure replaced by an SVG that isn't
    well-formed. The figure field differs, so SPEC-4 must be listed. Requirement gap:
    -27 names only the object file and the links file, so whether this is Modified or
    'unreadable at To' isn't decided; the test only checks that SPEC-4 is reported."""
    r, f, c1 = build_spec(tmp_path)
    # By hand, as a git client could: the editor refuses an SVG that isn't well-formed.
    editor.figure_path(f, "SPEC-4").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect\n')
    c2 = r.commit("broken figure")
    change = change_for(compare(r, c1, c2), "SPEC-4")
    assert change is not None and (Change.MODIFIED in change.kinds or change.errors[1] is not None), (
        f"SPEC-4's figure changed but SPEC-4 isn't reported: {change}"
    )


def build_merge_in_progress(tmp_path: Path) -> GitRepo:
    """A merge that stopped on SPEC-1: main and side both changed its description; side
    also changed SPEC-2 to 'Side two.', which merged cleanly into the working copy."""
    r, f, c1 = build_spec(tmp_path)
    repo = r.repo
    main = repo.head.shorthand
    repo.branches.local.create("side", repo[pygit2.Oid(hex=c1)])
    editor.set_description(f, "SPEC-1", "Main text.")
    r.commit("main edit")
    repo = r.repo
    repo.checkout(repo.branches.local["side"])
    editor.set_description(f, "SPEC-1", "Side text.")
    editor.set_description(f, "SPEC-2", "Side two.")
    side = r.commit("side edit")
    repo = r.repo
    repo.checkout(repo.branches.local[main])
    repo.merge(pygit2.Oid(hex=side))
    assert b"<<<<<<<" in r.read("SPEC/objects/SPEC-1.xml"), "setup: no conflict markers in SPEC-1"
    assert (r.root / ".git" / "MERGE_HEAD").exists(), "setup: no merge in progress"
    return r


def test_ver27_conflict_markers_in_working_copy_are_unreadable_at_to(tmp_path):
    """MELLON-VER-SRS-27: during a merge in progress, SPEC-1's file in the working copy
    holds conflict markers: latest commit -> working copy reports SPEC-1 unreadable at To."""
    r = build_merge_in_progress(tmp_path)
    change = change_for(compare(r, diff.LATEST, diff.WORKING_COPY), "SPEC-1")
    errors = change.errors if change else None
    assert errors is not None and errors[1] is not None, f"SPEC-1 should be unreadable at To, got {errors}"


def test_ver28_merge_in_progress_other_objects_still_reported(tmp_path):
    """MELLON-VER-SRS-28: during that merge, SPEC-2's cleanly merged change is still
    reported: exactly Modified, 'Requirement 2.' -> 'Side two.'."""
    r = build_merge_in_progress(tmp_path)
    change = change_for(compare(r, diff.LATEST, diff.WORKING_COPY), "SPEC-2")
    got = (tuple(change.kinds), change.delta.description and
           (change.delta.description.text_a, change.delta.description.text_b)) if change else None
    assert got == ((Change.MODIFIED,), ("Requirement 2.", "Side two.")), f"SPEC-2's change not reported: {got}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-29: unreadable manifest
# --------------------------------------------------------------------------


def build_manifest_broken_at_from(tmp_path: Path) -> tuple[GitRepo, str, str]:
    """c2: the manifest isn't well-formed. c3: the manifest valid again, with SPEC-3 moved
    under SPEC-4, and SPEC-1's description changed."""
    r, f, _ = build_spec(tmp_path)
    good = r.read("SPEC/SPEC.xml")
    broken = good[: len(good) // 2]
    assert not_well_formed(broken), "setup: truncated manifest still parses"
    r.write("SPEC/SPEC.xml", broken)
    c2 = r.commit("break manifest")
    r.write("SPEC/SPEC.xml", good)
    editor.move_object(f, "SPEC-3", parent_id="SPEC-4")
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    c3 = r.commit("fix manifest, move SPEC-3, change SPEC-1")
    return r, c2, c3


def test_ver29_manifest_broken_at_from_is_reported(tmp_path):
    """MELLON-VER-SRS-29: the manifest can't be read at From: Mellon reports that."""
    r, c2, c3 = build_manifest_broken_at_from(tmp_path)
    d = compare(r, c2, c3)
    assert (d.outline_error_a is not None, d.outline_error_b is None) == (True, True), (
        f"expected an outline error at From only, got {d.outline_error_a!r}, {d.outline_error_b!r}"
    )


def test_ver29_manifest_broken_at_from_reports_no_object_moved(tmp_path):
    """MELLON-VER-SRS-29: with the manifest unreadable at From, SPEC-3 (moved under SPEC-4
    at To) isn't reported Moved, nor is any other object."""
    r, c2, c3 = build_manifest_broken_at_from(tmp_path)
    d = compare(r, c2, c3)
    assert moved_ids(d) == set(), f"no object may be Moved without the From outline, got {sorted(moved_ids(d))}"


def test_ver29_manifest_broken_at_from_other_changes_still_reported(tmp_path):
    """MELLON-VER-SRS-29 with -21: SPEC-1's description change is still reported, and as
    exactly Modified."""
    r, c2, c3 = build_manifest_broken_at_from(tmp_path)
    change = change_for(compare(r, c2, c3), "SPEC-1")
    assert change is not None and tuple(change.kinds) == (Change.MODIFIED,), (
        f"SPEC-1 should be exactly Modified, got {change and change.kinds}"
    )


def build_manifest_broken_at_to(tmp_path: Path) -> tuple[GitRepo, str, str]:
    """c2: SPEC-1's description changed, SPEC-3 moved under SPEC-4, then the manifest
    overwritten with one that isn't well-formed."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    editor.move_object(f, "SPEC-3", parent_id="SPEC-4")
    good = r.read("SPEC/SPEC.xml")
    broken = good[: len(good) // 2]
    assert not_well_formed(broken), "setup: truncated manifest still parses"
    r.write("SPEC/SPEC.xml", broken)
    c2 = r.commit("change SPEC-1, break manifest")
    return r, c1, c2


def test_ver29_manifest_broken_at_to_is_reported(tmp_path):
    """MELLON-VER-SRS-29: the manifest can't be read at To: Mellon reports that."""
    r, c1, c2 = build_manifest_broken_at_to(tmp_path)
    d = compare(r, c1, c2)
    assert (d.outline_error_a is None, d.outline_error_b is not None) == (True, True), (
        f"expected an outline error at To only, got {d.outline_error_a!r}, {d.outline_error_b!r}"
    )


def test_ver29_manifest_broken_at_to_object_is_modified_only(tmp_path):
    """MELLON-VER-SRS-29: with the manifest unreadable at To, SPEC-1 is exactly Modified
    (not Moved), and no object is Moved."""
    r, c1, c2 = build_manifest_broken_at_to(tmp_path)
    d = compare(r, c1, c2)
    change = change_for(d, "SPEC-1")
    got = (tuple(change.kinds) if change else None, sorted(moved_ids(d)))
    assert got == ((Change.MODIFIED,), []), f"expected SPEC-1 exactly Modified and nothing Moved, got {got}"


def test_ver29_manifest_missing_with_folder_present_is_reported(tmp_path):
    """MELLON-VER-SRS-29 (definition 'can't be read': the folder is there and the manifest
    isn't): Mellon reports the manifest as unreadable at To."""
    r, _, c1 = build_spec(tmp_path)
    r.delete("SPEC/SPEC.xml")
    c2 = r.commit("manifest removed")
    d = compare(r, c1, c2)
    assert d.outline_error_b is not None, "a missing manifest in a present folder should be reported at To"


def test_ver29_manifest_missing_objects_are_not_deleted(tmp_path):
    """MELLON-VER-SRS-20 with -29 (definition 'doesn't exist at a version': an object
    exists while its object file is there): with only the manifest gone at To, no
    object is reported Deleted."""
    r, _, c1 = build_spec(tmp_path)
    r.delete("SPEC/SPEC.xml")
    c2 = r.commit("manifest removed")
    d = compare(r, c1, c2)
    deleted = sorted(c.obj_id for c in d.changes if Change.DELETED in c.kinds)
    assert deleted == [], f"object files are all still there, but Deleted: {deleted}"


# --------------------------------------------------------------------------
# MELLON-VER-SRS-47: word marking
# --------------------------------------------------------------------------


def _sides(segments) -> tuple[list[str], list[str], list[str], list[str]]:
    """(From words, To words, deleted words, inserted words) read from the segments."""
    tags = {tag for tag, _ in segments}
    assert tags <= {"equal", "delete", "insert"}, f"unexpected segment tags {tags}"
    frm, to, deleted, inserted = [], [], [], []
    for tag, text in segments:
        words = _words(text)
        if tag == "equal":
            frm += words
            to += words
        elif tag == "delete":
            frm += words
            deleted += words
        else:
            to += words
            inserted += words
    return frm, to, deleted, inserted


VOCAB = ["a", "b", "c", "the", "VCU", "shall", "stop.", "stop!", "<", "&", "x1", "x1."]
VOCAB_NO_PUNCTUATION = ["a", "b", "c", "the", "VCU", "shall", "stop", "<", "&", "x1"]
SPACES = [" ", "  ", "\t", "\n", " \n  "]

# definitions.md, Word (decided 2026-11-07): a run of letters, digits and
# underscores, or one other character that isn't whitespace.
_WORD_RE = re.compile(r"\w+|[^\w\s]")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _text(rng: random.Random, words: list[str]) -> str:
    text = "".join(w + rng.choice(SPACES) for w in words).rstrip(" \t\n") if words else ""
    if rng.random() < 0.2:
        text = rng.choice(SPACES) + text
    if rng.random() < 0.2:
        text += rng.choice(SPACES)
    return text


def _word_pairs(vocab: list[str], n: int = 400, seed: int = 4747) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    pairs = []
    for _ in range(n):
        a = [rng.choice(vocab) for _ in range(rng.randint(0, 10))]
        if rng.random() < 0.3:
            b = [rng.choice(vocab) for _ in range(rng.randint(0, 10))]
        else:
            b = a[:]
            for _ in range(rng.randint(1, 4)):
                op = rng.choice(["insert", "delete", "replace"])
                if op == "insert" or not b:
                    b.insert(rng.randint(0, len(b)), rng.choice(vocab))
                elif op == "delete":
                    b.pop(rng.randrange(len(b)))
                else:
                    b[rng.randrange(len(b))] = rng.choice(vocab)
        pairs.append((_text(rng, a), _text(rng, b)))
    return pairs


def _side_errors(vocab: list[str], side: int) -> list[str]:
    bad = []
    for a, b in _word_pairs(vocab):
        words = _sides(diff.word_diff(a, b))[side]
        if words != _words((a, b)[side]):
            bad.append(f"{a!r} -> {b!r}: {'From' if side == 0 else 'To'} read as {words}")
    return bad


def test_ver47_generated_from_is_unmarked_and_deleted_words():
    """MELLON-VER-SRS-47, generated: for each pair, the unmarked and deleted words, in
    order, are exactly the words of From, so no word of From is left out or split."""
    bad = _side_errors(VOCAB, 0)
    assert not bad, f"{len(bad)} pairs wrong, e.g.:\n" + "\n".join(bad[:5])


def test_ver47_generated_to_is_unmarked_and_inserted_words():
    """MELLON-VER-SRS-47, generated: for each pair, the unmarked and inserted words, in
    order, are exactly the words of To; with the From test this makes the unmarked
    words a common subsequence, the same at From and at To."""
    bad = _side_errors(VOCAB, 1)
    assert not bad, f"{len(bad)} pairs wrong, e.g.:\n" + "\n".join(bad[:5])


@pytest.mark.parametrize("side", [0, 1], ids=["from", "to"])
def test_ver47_generated_without_punctuation_unmarked_words_are_common(side):
    """MELLON-VER-SRS-47, generated, with words that hold no punctuation: From is its
    unmarked and deleted words in order, and To its unmarked and inserted words."""
    bad = _side_errors(VOCAB_NO_PUNCTUATION, side)
    assert not bad, f"{len(bad)} pairs wrong, e.g.:\n" + "\n".join(bad[:5])


@pytest.mark.parametrize(
    "a, b, deleted, inserted",
    [
        ("", "New text", [], ["New", "text"]),
        ("Old text", "", ["Old", "text"], []),
        ("stop.", "stop!", ["."], ["!"]),
    ],
    ids=["empty-to-text", "text-to-empty", "punctuation"],
)
def test_ver47_edge_cases_marked_words(a, b, deleted, inserted):
    """MELLON-VER-SRS-47: '' -> 'New text' marks both words inserted and nothing at From;
    'Old text' -> '' marks both deleted; 'stop.' -> 'stop!' marks only '.' and '!', each
    a word of its own (definition of Word), so 'stop' is unmarked at both."""
    _, _, got_deleted, got_inserted = _sides(diff.word_diff(a, b))
    assert (got_deleted, got_inserted) == (deleted, inserted), (
        f"{a!r} -> {b!r}: deleted {got_deleted}, inserted {got_inserted}"
    )


def test_ver47_only_the_changed_word_is_marked():
    """MELLON-VER-SRS-47: 'a < b & c' -> 'a < d & c' marks only b (deleted) and d
    (inserted): a, <, & and c weren't deleted or inserted, and the rationale asks that
    few words be marked. The one common subsequence of length 4 makes this unique."""
    _, _, deleted, inserted = _sides(diff.word_diff("a < b & c", "a < d & c"))
    assert (deleted, inserted) == (["b"], ["d"]), f"deleted {deleted}, inserted {inserted}"


class _TextOf(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def test_ver47_dialog_shows_lt_and_amp_literally(qapp):
    """MELLON-VER-SRS-47 (dialog side): the marked-up description 'a < b & c' -> 'a < d & c'
    reads, as text, exactly 'a < b & c' at From and 'a < d & c' at To, so '<' and '&'
    are shown literally. (The theme tokens are stand-ins: every token is one colour.)"""
    from mellon.gui.dialogs.compare_dialog import words_html

    tokens = defaultdict(lambda: "#808080")
    shown = []
    for side in ("a", "b"):
        parser = _TextOf()
        parser.feed(words_html("a < b & c", "a < d & c", side, tokens))
        parser.close()
        shown.append("".join(parser.parts).split())
    assert shown == [["a", "<", "b", "&", "c"], ["a", "<", "d", "&", "c"]], f"dialog text read {shown}"


def test_ver47_compared_description_words_cover_both_texts(tmp_path):
    """MELLON-VER-SRS-47 through a comparison: SPEC-1 'The VCU shall stop.' -> 'The VCU
    must stop.': the reported words give the From text from unmarked and deleted
    words, and the To text from unmarked and inserted words."""
    r, f, c1 = build_spec(tmp_path)
    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    c2 = r.commit("must")
    change = change_for(compare(r, c1, c2), "SPEC-1")
    frm, to, _, _ = _sides(change.delta.description.words())
    assert (frm, to) == (_words("The VCU shall stop."), _words("The VCU must stop.")), (
        f"words read From {frm}, To {to}"
    )


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


COMPARISONS = {
    "commit-to-commit": (lambda c: ("SPEC", c["c1"], c["c2"]), False),
    "issue-to-latest": (lambda c: ("SPEC", "SPEC-issue-1", diff.LATEST), False),
    "typed-prefix": (lambda c: ("SPEC", c["c1"][:6], c["c2"]), False),
    "latest-to-working-copy": (lambda c: ("SPEC", diff.LATEST, diff.WORKING_COPY), False),
    "working-copy-to-issue": (lambda c: ("SPEC", diff.WORKING_COPY, "SPEC-issue-1"), False),
    "unreadable-files": (lambda c: ("SPEC", c["c2"], c["c3"]), False),
    "bad-name": (lambda c: ("SPEC", "no-such-version", diff.LATEST), True),
    "document-at-neither": (lambda c: ("NOPE", c["c1"], c["c2"]), True),
}


@pytest.mark.parametrize("name", list(COMPARISONS))
def test_ver56_comparing_changes_no_file_index_or_reference(tmp_path, name):
    """MELLON-VER-SRS-56: a comparison (or a failing one) leaves every working-copy file,
    the git index, HEAD and every reference as they were."""
    r, commits = build_snapshot_repo(tmp_path)
    make_args, fails = COMPARISONS[name]
    doc_id, ref_a, ref_b = make_args(commits)
    before = snapshot(r.root)
    if fails:
        with pytest.raises(Exception):
            diff.compare_document(r.root, doc_id, ref_a, ref_b)
    else:
        diff.compare_document(r.root, doc_id, ref_a, ref_b)
    changed = snapshot_difference(before, snapshot(r.root))
    assert not changed, f"comparing {name} changed: {changed[:10]}"


# --------------------------------------------------------------------------
# The MCP server over stdio: MELLON-VER-SRS-26, -56, -59, -60
# --------------------------------------------------------------------------


def _mcp_command() -> str:
    exe = shutil.which("mellon-mcp")
    if exe is None:
        name = "mellon-mcp.exe" if os.name == "nt" else "mellon-mcp"
        candidate = Path(sys.executable).parent / name
        exe = str(candidate) if candidate.exists() else None
    if exe is None:
        pytest.fail("mellon-mcp isn't installed in this environment")
    return exe


def mcp_session(root: Path, calls: list[dict], log: Path, on_ready=None, on_done=None) -> list[tuple[bool, str]]:
    """Start the MCP server over stdio on `root`, call compare_document with each
    argument dict in `calls`, and return (is error, text) for each."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    params = StdioServerParameters(command=_mcp_command(), args=["--repo-root", str(root)], env=env)

    async def run() -> list[tuple[bool, str]]:
        results = []
        with open(log, "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if on_ready:
                        on_ready()
                    for args in calls:
                        res = await session.call_tool("compare_document", args)
                        text = "".join(getattr(c, "text", "") or "" for c in res.content)
                        results.append((bool(res.isError), text))
                    if on_done:
                        on_done()
        return results

    return asyncio.run(asyncio.wait_for(run(), timeout=120))


def _is_error(result: tuple[bool, str]) -> bool:
    is_error, text = result
    if is_error:
        return True
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and "error" in payload


def build_mcp_spec(tmp: Path) -> tuple[GitRepo, dict[str, str]]:
    """SPEC with Issue 1 on "seed" and the latest commit "edits":

    seed:  SPEC-0 Heading "Introduction"
             SPEC-1 "The VCU shall stop."   SPEC-2 "Requirement 2."   SPEC-3 "Requirement 3."
           SPEC-4 Heading "Interfaces"
             SPEC-5 "Requirement 5." -satisfies-> SPEC-1 (ack c0)
             SPEC-6 "Requirement 6." -refines-> SPEC-2 (ack c0)
             SPEC-7 "Requirement 7."
    edits: SPEC-1 -> "The VCU must stop."; SPEC-2 rationale (label "Rationale") set to
           "Because."; SPEC-3 deleted; SPEC-8 "Requirement 8." added under SPEC-0;
           SPEC-7 moved to the top level after SPEC-4; SPEC-5's link re-acknowledged
           (ack seed); SPEC-6's link to SPEC-2 removed and a link -satisfies-> SPEC-1 added.
    """
    r = GitRepo(tmp / "repo")
    f = editor.create_document(r.root, "SPEC", title="Spec")
    manifest = r.read("SPEC/SPEC.xml")
    plain = b'<attribute name="rationale" kind="text" required="false"/>'
    assert plain in manifest, "setup: rationale declaration not found"
    r.write("SPEC/SPEC.xml", manifest.replace(plain, plain[:-2] + b' label="Rationale"/>'))
    editor.add_attr_option(f, "type", "Requirement")
    texts = {"SPEC-1": "The VCU shall stop.", "SPEC-2": "Requirement 2.", "SPEC-3": "Requirement 3.",
             "SPEC-5": "Requirement 5.", "SPEC-6": "Requirement 6.", "SPEC-7": "Requirement 7."}
    ids = [editor.create_object(f, None, "Introduction")]
    ids += [editor.create_object(f, "SPEC-0", texts[i]) for i in ("SPEC-1", "SPEC-2", "SPEC-3")]
    ids.append(editor.create_object(f, None, "Interfaces"))
    ids += [editor.create_object(f, "SPEC-4", texts[i]) for i in ("SPEC-5", "SPEC-6", "SPEC-7")]
    assert ids == [f"SPEC-{i}" for i in range(8)], f"setup: unexpected ids {ids}"
    for obj_id in ids:
        editor.set_attribute(f, obj_id, "type", "Heading" if obj_id in ("SPEC-0", "SPEC-4") else "Requirement")
    c0 = r.commit("objects")
    r.write("SPEC/links/SPEC-5.xml", links_xml("SPEC-5", [("SPEC", "SPEC-1", "satisfies", c0[:7])]))
    r.write("SPEC/links/SPEC-6.xml", links_xml("SPEC-6", [("SPEC", "SPEC-2", "refines", c0[:7])]))
    seed = r.commit("seed")
    r.tag_issue("SPEC", "1", seed)

    editor.set_description(f, "SPEC-1", "The VCU must stop.")
    editor.set_attribute(f, "SPEC-2", "rationale", "Because.")
    assert editor.delete_object(f, "SPEC-3", force=True).performed, "setup: SPEC-3 not deleted"
    added = editor.create_object(f, "SPEC-0", "Requirement 8.")
    assert added == "SPEC-8", f"setup: added object got id {added}"
    editor.set_attribute(f, "SPEC-8", "type", "Requirement")
    editor.move_object(f, "SPEC-7", to_top=True, after="SPEC-4")
    r.write("SPEC/links/SPEC-5.xml", links_xml("SPEC-5", [("SPEC", "SPEC-1", "satisfies", seed[:7])]))
    r.write("SPEC/links/SPEC-6.xml", links_xml("SPEC-6", [("SPEC", "SPEC-1", "satisfies", seed[:7])]))
    latest = r.commit("edits")
    return r, {"c0": c0, "seed": seed, "latest": latest}


def _unique_prefix(repo: pygit2.Repository, hex_id: str, length: int) -> bool:
    prefix = hex_id[:length]
    return sum(1 for oid in repo.odb if str(oid).startswith(prefix)) == 1


@pytest.fixture(scope="module")
def mcp_spec(tmp_path_factory):
    """One MCP server session over stdio on the SPEC repository of `build_mcp_spec`.
    Returns (commits, results by call name)."""
    tmp = tmp_path_factory.mktemp("mcp_spec")
    r, commits = build_mcp_spec(tmp)
    seed = commits["seed"]
    assert _unique_prefix(r.repo, seed, 4), "setup: the 4-hex prefix of seed isn't unique"
    calls = {
        "issue-to-latest": {"doc_id": "SPEC", "from_ref": "SPEC-issue-1", "to_ref": diff.LATEST},
        "bad-to": {"doc_id": "SPEC", "from_ref": "SPEC-issue-1", "to_ref": "no-such-version"},
        "3-hex-from": {"doc_id": "SPEC", "from_ref": seed[:3], "to_ref": diff.LATEST},
        "4-hex-from": {"doc_id": "SPEC", "from_ref": seed[:4], "to_ref": diff.LATEST},
    }
    results = mcp_session(r.root, list(calls.values()), tmp / "mcp.log")
    return commits, dict(zip(calls, results))


def _payload(result: tuple[bool, str]) -> dict:
    is_error, text = result
    assert not is_error, f"the compare tool failed: {text[:500]}"
    return json.loads(text)


def _entry(payload: dict, obj_id: str) -> dict:
    found = [c for c in payload["changes"] if c["obj_id"] == obj_id]
    assert len(found) == 1, f"{obj_id} is listed {len(found)} times in the MCP result"
    return found[0]


def test_ver59_mcp_reports_each_changed_object_with_its_changes(mcp_spec):
    """MELLON-VER-SRS-59: Issue 1 -> latest commit lists exactly SPEC-1, -2, -6 Modified,
    SPEC-3 Deleted, SPEC-8 Added, SPEC-7 Moved and SPEC-5 Link reviewed."""
    _, results = mcp_spec
    payload = _payload(results["issue-to-latest"])
    got = {c["obj_id"]: sorted(c["changes"]) for c in payload["changes"]}
    expected = {"SPEC-1": ["Modified"], "SPEC-2": ["Modified"], "SPEC-3": ["Deleted"], "SPEC-5": ["Link reviewed"],
                "SPEC-6": ["Modified"], "SPEC-7": ["Moved"], "SPEC-8": ["Added"]}
    assert got == expected, f"MCP changes {got}"


def test_ver59_mcp_description_at_both(mcp_spec):
    """MELLON-VER-SRS-59: SPEC-1's description from 'The VCU shall stop.' to 'The VCU must stop.'."""
    _, results = mcp_spec
    entry = _entry(_payload(results["issue-to-latest"]), "SPEC-1")
    assert entry.get("description") == {"from": "The VCU shall stop.", "to": "The VCU must stop."}, (
        f"SPEC-1 description reported as {entry.get('description')}"
    )


def test_ver59_mcp_attribute_at_both_with_label(mcp_spec):
    """MELLON-VER-SRS-59 (with -45, -49): SPEC-2's rationale from not set to 'Because.',
    named by its label at To, 'Rationale'."""
    _, results = mcp_spec
    entry = _entry(_payload(results["issue-to-latest"]), "SPEC-2")
    attrs = [a for a in entry.get("attrs", []) if a.get("name") == "rationale"]
    got = [(a.get("label"), a.get("from"), a.get("to")) for a in attrs]
    assert got == [("Rationale", None, "Because.")], f"SPEC-2 rationale reported as {attrs}"


def test_ver59_mcp_deleted_object_fields_at_from(mcp_spec):
    """MELLON-VER-SRS-59 (with -50): SPEC-3, Deleted, comes with its fields at From:
    description 'Requirement 3.', type Requirement, under SPEC-0."""
    _, results = mcp_spec
    obj = _entry(_payload(results["issue-to-latest"]), "SPEC-3").get("object") or {}
    got = (obj.get("description"), (obj.get("attrs") or {}).get("type"), (obj.get("position") or {}).get("parent"))
    assert got == ("Requirement 3.", "Requirement", "SPEC-0"), f"SPEC-3 fields at From: {obj}"


def test_ver59_mcp_added_object_fields_at_to(mcp_spec):
    """MELLON-VER-SRS-59 (with -50): SPEC-8, Added, comes with its fields at To:
    description 'Requirement 8.', type Requirement, under SPEC-0."""
    _, results = mcp_spec
    obj = _entry(_payload(results["issue-to-latest"]), "SPEC-8").get("object") or {}
    got = (obj.get("description"), (obj.get("attrs") or {}).get("type"), (obj.get("position") or {}).get("parent"))
    assert got == ("Requirement 8.", "Requirement", "SPEC-0"), f"SPEC-8 fields at To: {obj}"


def test_ver59_mcp_moved_parent_and_position_at_both(mcp_spec):
    """MELLON-VER-SRS-59 (with -52): SPEC-7 under SPEC-4 after SPEC-6 at From, at the top
    level after SPEC-4 at To."""
    _, results = mcp_spec
    moved = _entry(_payload(results["issue-to-latest"]), "SPEC-7").get("moved") or {}
    got = {side: ((moved.get(side) or {}).get("parent"), (moved.get(side) or {}).get("after"))
           for side in ("from", "to")}
    assert got == {"from": ("SPEC-4", "SPEC-6"), "to": (None, "SPEC-4")}, f"SPEC-7 positions: {moved}"


def test_ver59_mcp_link_reviewed_ack_at_both(mcp_spec):
    """MELLON-VER-SRS-59 (with -53): SPEC-5's link to SPEC-1 with its acknowledged commit
    at From (c0) and at To (seed)."""
    commits, results = mcp_spec
    entry = _entry(_payload(results["issue-to-latest"]), "SPEC-5")
    records = [rec for group in (entry.get("links") or {}).values() for rec in group
               if isinstance(rec, dict) and rec.get("target_id") == "SPEC-1"]
    values = [set(map(str, rec.values())) for rec in records]
    ok = [v for v in values if {commits["c0"][:7], commits["seed"][:7]} <= v]
    assert len(ok) == 1, f"SPEC-5's reviewed link with acks {commits['c0'][:7]} -> {commits['seed'][:7]} not found in {entry}"


def test_ver59_mcp_link_added_and_removed(mcp_spec):
    """MELLON-VER-SRS-59 (with -46): SPEC-6's link -refines-> SPEC-2 removed and its link
    -satisfies-> SPEC-1 added."""
    _, results = mcp_spec
    links = _entry(_payload(results["issue-to-latest"]), "SPEC-6").get("links") or {}
    got = {kind: sorted((rec.get("target_id"), rec.get("type")) for rec in links.get(kind, []))
           for kind in ("added", "removed")}
    assert got == {"added": [("SPEC-1", "satisfies")], "removed": [("SPEC-2", "refines")]}, f"SPEC-6 links: {links}"


def test_ver60_mcp_unknown_to_version_error_names_it(mcp_spec):
    """MELLON-VER-SRS-60: to_ref 'no-such-version' gives an error naming 'no-such-version'."""
    _, results = mcp_spec
    result = results["bad-to"]
    assert _is_error(result) and "no-such-version" in result[1], f"expected an error naming it, got {result}"


def test_ver60_mcp_three_hex_prefix_error_names_it(mcp_spec):
    """MELLON-VER-SRS-60 (with -9: 4 hex digits is the shortest prefix git accepts): the
    first 3 hex digits of a real commit as from_ref resolve to no commit: an error naming
    the prefix."""
    commits, results = mcp_spec
    prefix = commits["seed"][:3]
    is_error, text = results["3-hex-from"]
    named = re.search(rf"(?<![0-9a-f]){prefix}(?![0-9a-f])", text) is not None
    assert _is_error(results["3-hex-from"]) and named, f"expected an error naming {prefix!r}, got {(is_error, text)}"


def test_ver60_mcp_four_hex_prefix_is_the_same_version(mcp_spec):
    """MELLON-VER-SRS-60 boundary (just on the limit): a unique 4-hex prefix of the Issue 1
    commit resolves to that commit, so the result is the same as from 'SPEC-issue-1'.
    Assumption: the MCP tool accepts the names SRS-9 lists for the dialog, since -59 says
    agents 'must get the same answer as a person'."""
    _, results = mcp_spec
    assert not _is_error(results["4-hex-from"]), f"a unique 4-hex prefix was refused: {results['4-hex-from']}"
    by_prefix = {c["obj_id"]: c["changes"] for c in _payload(results["4-hex-from"])["changes"]}
    by_issue = {c["obj_id"]: c["changes"] for c in _payload(results["issue-to-latest"])["changes"]}
    assert by_prefix == by_issue, f"from the prefix {by_prefix}, from the issue {by_issue}"


@pytest.fixture(scope="module")
def mcp_other_only(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mcp_other")
    r, c1, c2 = build_other_only(tmp)
    repo = r.repo
    git_ops.create_tag(repo, "rel-a", pygit2.Oid(hex=c1), "release a")
    repo.branches.local.create("next", repo[pygit2.Oid(hex=c2)])
    calls = [{"doc_id": "SPEC", "from_ref": c1[:7], "to_ref": c2[:7]},
             {"doc_id": "SPEC", "from_ref": "rel-a", "to_ref": "next"}]
    return (c1, c2), mcp_session(r.root, calls, tmp / "mcp.log")


def test_ver26_mcp_error_names_document_and_both_hashes(mcp_other_only):
    """MELLON-VER-SRS-26 over MCP: SPEC between two 7-hex hashes in a repository holding
    only OTHER: an error naming SPEC and both hashes."""
    (c1, c2), results = mcp_other_only
    result = results[0]
    missing = [w for w in ("SPEC", c1[:7], c2[:7]) if w not in result[1]]
    assert _is_error(result) and not missing, f"expected an error naming SPEC and both, missing {missing}: {result}"


def test_ver26_mcp_error_names_document_and_both_names(mcp_other_only):
    """MELLON-VER-SRS-26 over MCP: SPEC between 'rel-a' and 'next' in a repository holding
    only OTHER: an error naming SPEC, 'rel-a' and 'next'."""
    _, results = mcp_other_only
    result = results[1]
    missing = [w for w in ("SPEC", "rel-a", "next") if w not in result[1]]
    assert _is_error(result) and not missing, f"expected an error naming SPEC and both, missing {missing}: {result}"


def test_ver56_mcp_compare_changes_no_file_index_or_reference(tmp_path):
    """MELLON-VER-SRS-56 over MCP: compare calls (latest -> working copy, Issue 1 -> latest,
    and a failing one) leave the working copy, the index, HEAD and every reference as
    they were once the server was up."""
    r, _ = build_snapshot_repo(tmp_path)
    snaps = []
    calls = [{"doc_id": "SPEC", "from_ref": diff.LATEST, "to_ref": diff.WORKING_COPY},
             {"doc_id": "SPEC", "from_ref": "SPEC-issue-1", "to_ref": diff.LATEST},
             {"doc_id": "SPEC", "from_ref": "no-such-version", "to_ref": diff.LATEST}]
    log = tmp_path / "mcp.log"
    results = mcp_session(r.root, calls, log,
                          on_ready=lambda: snaps.append(snapshot(r.root)),
                          on_done=lambda: snaps.append(snapshot(r.root)))
    assert [_is_error(x) for x in results] == [False, False, True], f"setup: unexpected results {results}"
    changed = snapshot_difference(snaps[0], snaps[1])
    assert not changed, f"comparing over MCP changed: {changed[:10]}"
