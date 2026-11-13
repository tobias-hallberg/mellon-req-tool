"""Tests for git-backed suspect-link detection.

Each test builds a real pygit2 repo in tmp_path containing two documents
(a child DOC and a parent PARENT) so we can exercise cross-doc link scanning.
"""

from __future__ import annotations

import os
from pathlib import Path

import pygit2
import pytest

from mellon import load_document
from mellon.editor import add_link
from mellon.git_ops import last_commit_for_path
from mellon.suspects import LinkState, ack_all, scan_document, write_ack
from tests.test_git_ops import _commit_on


PARENT_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="PARENT">
  <metadata><title>P</title></metadata>
  <attributes>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
    </attribute>
  </attributes>
  <outline><ref id="PARENT-42"/></outline>
</document>
"""

PARENT_OBJ_V1 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="PARENT-42">
  <attr name="status">Draft</attr>
  <description>original parent text</description>
</object>
"""

PARENT_OBJ_V2 = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="PARENT-42">
  <attr name="status">Draft</attr>
  <description>revised parent text</description>
</object>
"""

DOC_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="DOC">
  <metadata><title>D</title></metadata>
  <parents><parent doc="PARENT"/></parents>
  <attributes>
    <attribute name="status" kind="enum" required="true">
      <option>Draft</option>
    </attribute>
  </attributes>
  <outline><ref id="DOC-1"/></outline>
</document>
"""

DOC_OBJ = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="DOC-1">
  <attr name="status">Draft</attr>
  <description>child</description>
</object>
"""


def _commit_all(repo: pygit2.Repository, message: str) -> str:
    """Stage everything in the workdir and make a commit. Returns short hash."""
    index = repo.index
    index.add_all()
    index.write()
    tree = index.write_tree()
    sig = pygit2.Signature("Test", "test@example.com")
    parents = []
    if not repo.is_empty:
        parents = [repo.head.target]
    oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
    return str(oid)[:7]


def _link_xml(ack: str | None) -> str:
    ack_attr = f' ack="{ack}"' if ack else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<links source="DOC-1">\n'
        f'  <link target-doc="PARENT" target-id="PARENT-42" type="satisfies"{ack_attr}/>\n'
        '</links>\n'
    )


@pytest.fixture
def repo_tree(tmp_path: Path):
    """Build a two-doc repo with one commit.

    Returns (repo_root, doc_folder, first_commit_short_hash).
    """
    root = tmp_path

    parent = root / "PARENT"
    (parent / "objects").mkdir(parents=True)
    (parent / "PARENT.xml").write_text(PARENT_MANIFEST, encoding="utf-8")
    (parent / "objects" / "PARENT-42.xml").write_text(PARENT_OBJ_V1, encoding="utf-8")

    doc = root / "DOC"
    (doc / "objects").mkdir(parents=True)
    (doc / "links").mkdir()
    (doc / "DOC.xml").write_text(DOC_MANIFEST, encoding="utf-8")
    (doc / "objects" / "DOC-1.xml").write_text(DOC_OBJ, encoding="utf-8")
    # Links file written per-test so ack value is explicit.

    repo = pygit2.init_repository(str(root))
    # Write links with a placeholder ack; update after commit.
    (doc / "links" / "DOC-1.xml").write_text(_link_xml("0000000"), encoding="utf-8")
    c1 = _commit_all(repo, "initial")
    return root, doc, c1


def test_last_commit_for_path_matches_head(repo_tree):
    root, _doc, c1 = repo_tree
    repo = pygit2.Repository(str(root))
    assert last_commit_for_path(repo, "PARENT/objects/PARENT-42.xml") == c1


def test_ok_when_ack_equals_current(repo_tree):
    root, doc, c1 = repo_tree
    # Rewrite link with correct ack and commit.
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    _commit_all(repo, "ack")

    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert len(statuses) == 1
    assert statuses[0].state is LinkState.OK
    assert statuses[0].current_hash == c1


def test_suspect_when_target_changed(repo_tree):
    root, doc, c1 = repo_tree
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    _commit_all(repo, "ack")

    # Modify the parent and commit.
    (root / "PARENT" / "objects" / "PARENT-42.xml").write_text(
        PARENT_OBJ_V2, encoding="utf-8"
    )
    c2 = _commit_all(repo, "revise parent")
    assert c2 != c1

    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].current_hash == c2
    assert statuses[0].link.ack == c1


def test_never_reviewed_is_suspect(repo_tree):
    _root, doc, _c1 = repo_tree
    # Our fixture wrote a bogus ack; replace with one that has no @ack at all.
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(None), encoding="utf-8")
    # No need to commit — scan_document reads files from disk, not HEAD.

    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert "never reviewed" in statuses[0].reason


def test_broken_link_when_target_missing(repo_tree):
    root, doc, _c1 = repo_tree
    # Link points to a target that doesn't exist.
    (doc / "links" / "DOC-1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<links source="DOC-1">\n'
        '  <link target-doc="PARENT" target-id="PARENT-999" type="satisfies"/>\n'
        '</links>\n',
        encoding="utf-8",
    )
    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert statuses[0].state is LinkState.BROKEN


def _write_link_to(doc: Path, target_id: str, ack: str | None) -> None:
    ack_attr = f' ack="{ack}"' if ack else ""
    (doc / "links" / "DOC-1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<links source="DOC-1">\n'
        f'  <link target-doc="PARENT" target-id="{target_id}" type="satisfies"{ack_attr}/>\n'
        '</links>\n',
        encoding="utf-8",
    )


def test_target_on_disk_but_never_committed_is_suspect_and_not_acked(repo_tree):
    """MELLON-TRACE-SRS-45: the target exists, so the link isn't broken; with
    no last change there is nothing to acknowledge, so ack_all leaves it."""
    root, doc, _c1 = repo_tree
    (root / "PARENT" / "objects" / "PARENT-43.xml").write_text(
        PARENT_OBJ_V1.replace("PARENT-42", "PARENT-43"), encoding="utf-8"
    )
    _write_link_to(doc, "PARENT-43", None)
    before = (doc / "links" / "DOC-1.xml").read_bytes()

    statuses = scan_document(load_document(doc), doc)

    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].reason == "never reviewed"
    assert statuses[0].current_hash is None
    assert ack_all(doc, statuses) == []
    assert (doc / "links" / "DOC-1.xml").read_bytes() == before


def test_trace_srs_47_recorded_ack_to_never_committed_target_is_suspect(repo_tree):
    """MELLON-TRACE-SRS-47: a link that records an acknowledged commit, to a
    target in the working copy that was never committed (no last change), is
    suspect: the recorded commit doesn't match no last change."""
    root, doc, c1 = repo_tree
    (root / "PARENT" / "objects" / "PARENT-43.xml").write_text(
        PARENT_OBJ_V1.replace("PARENT-42", "PARENT-43"), encoding="utf-8"
    )
    _write_link_to(doc, "PARENT-43", c1)

    (status,) = scan_document(load_document(doc), doc)

    assert status.state is LinkState.SUSPECT
    assert status.current_hash is None


def test_ack_all_never_rewrites_a_broken_link(repo_tree):
    _root, doc, _c1 = repo_tree
    _write_link_to(doc, "PARENT-999", "aaaaaaa")
    before = (doc / "links" / "DOC-1.xml").read_bytes()

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.BROKEN

    assert ack_all(doc, statuses) == []
    assert (doc / "links" / "DOC-1.xml").read_bytes() == before


def test_two_commit_rule(repo_tree):
    """A commit can't hold its own hash: revising the parent and touching the
    ack in one commit stays suspect, and a second commit records the review."""
    root, doc, c1 = repo_tree
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    _commit_all(repo, "ack")

    (root / "PARENT" / "objects" / "PARENT-42.xml").write_text(
        PARENT_OBJ_V2, encoding="utf-8"
    )
    # The best the first commit can do is the hash it doesn't have yet's parent.
    (doc / "links" / "DOC-1.xml").write_text(
        _link_xml(str(repo.head.target)[:7]), encoding="utf-8"
    )
    revision = _commit_all(repo, "revise parent and ack together")

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].current_hash == revision

    assert len(ack_all(doc, statuses)) == 1
    _commit_all(repo, "record the review")

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.OK
    # The second commit didn't touch the parent, so it isn't the target's hash.
    assert statuses[0].current_hash == revision


def test_write_ack_updates_single_link(repo_tree):
    _root, doc, _c1 = repo_tree
    (doc / "links" / "DOC-1.xml").write_text(_link_xml("aaaaaaa"), encoding="utf-8")
    changed = write_ack(
        doc / "links" / "DOC-1.xml",
        target_doc="PARENT",
        target_id="PARENT-42",
        new_ack="bbbbbbb",
    )
    assert changed is True
    assert 'ack="bbbbbbb"' in (doc / "links" / "DOC-1.xml").read_text(encoding="utf-8")


def test_suspect_when_only_figure_changed(repo_tree):
    """Editing only the parent's figure must flip the link to SUSPECT."""
    root, doc, c1 = repo_tree
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    _commit_all(repo, "ack")

    # Add a figure for the parent — object xml stays byte-identical.
    figures = root / "PARENT" / "figures"
    figures.mkdir()
    (figures / "PARENT-42.svg").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>\n',
        encoding="utf-8",
    )
    c_fig = _commit_all(repo, "add figure to parent")
    assert c_fig != c1

    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].current_hash == c_fig


def test_ack_all_round_trip_with_figure(repo_tree):
    """Acking after a figure-only change writes the figure commit's hash."""
    root, doc, c1 = repo_tree
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    _commit_all(repo, "ack")

    figures = root / "PARENT" / "figures"
    figures.mkdir()
    (figures / "PARENT-42.svg").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>\n',
        encoding="utf-8",
    )
    c_fig = _commit_all(repo, "add figure to parent")

    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert statuses[0].state is LinkState.SUSPECT

    written = ack_all(doc, statuses)
    assert len(written) == 1

    loaded2 = load_document(doc)
    statuses2 = scan_document(loaded2, doc)
    assert statuses2[0].state is LinkState.OK
    assert statuses2[0].link.ack == c_fig


def test_suspect_when_figure_removed(repo_tree):
    root, doc, _c1 = repo_tree
    figures = root / "PARENT" / "figures"
    figures.mkdir()
    (figures / "PARENT-42.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>\n',
        encoding="utf-8",
    )
    repo = pygit2.Repository(str(root))
    c_fig = _commit_all(repo, "add figure to parent")
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c_fig), encoding="utf-8")
    _commit_all(repo, "ack")
    assert scan_document(load_document(doc), doc)[0].state is LinkState.OK

    (figures / "PARENT-42.svg").unlink()
    c_removed = _commit_all(repo, "remove figure")

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].current_hash == c_removed


def test_suspect_when_parent_commit_reverted(repo_tree):
    """The text is back to an earlier version, but not the one last reviewed."""
    root, doc, _c1 = repo_tree
    parent = root / "PARENT" / "objects" / "PARENT-42.xml"
    repo = pygit2.Repository(str(root))
    parent.write_text(PARENT_OBJ_V2, encoding="utf-8")
    c2 = _commit_all(repo, "revise parent")
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c2), encoding="utf-8")
    _commit_all(repo, "ack")

    parent.write_text(PARENT_OBJ_V1, encoding="utf-8")
    c_revert = _commit_all(repo, "revert parent")

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].current_hash == c_revert


def test_ack_all_round_trip(repo_tree):
    """The canonical flow: suspect -> ack -> ok, all without touching source."""
    root, doc, c1 = repo_tree
    (doc / "links" / "DOC-1.xml").write_text(_link_xml(c1), encoding="utf-8")
    repo = pygit2.Repository(str(root))
    _commit_all(repo, "ack")

    # Revise the parent; link goes suspect.
    (root / "PARENT" / "objects" / "PARENT-42.xml").write_text(
        PARENT_OBJ_V2, encoding="utf-8"
    )
    c2 = _commit_all(repo, "revise parent")

    loaded = load_document(doc)
    statuses = scan_document(loaded, doc)
    assert statuses[0].state is LinkState.SUSPECT

    # Ack writes the new hash to disk; nothing else changed.
    written = ack_all(doc, statuses)
    assert len(written) == 1

    # Reload and re-scan — should be OK now.
    loaded2 = load_document(doc)
    statuses2 = scan_document(loaded2, doc)
    assert statuses2[0].state is LinkState.OK
    assert statuses2[0].link.ack == c2

    # Source object file must be byte-identical — acknowledgment doesn't touch it.
    assert (doc / "objects" / "DOC-1.xml").read_text(encoding="utf-8") == DOC_OBJ


# --- merge histories -------------------------------------------------------
#
# Branches and merges are laid out as raw commits (no checkout or merge
# machinery), then HEAD is reset onto the tip so the work tree matches.

_PARENT_REL = "PARENT/objects/PARENT-42.xml"
_LINKS_REL = "DOC/links/DOC-1.xml"
_DOC_OBJ_REL = "DOC/objects/DOC-1.xml"
_DOC_OBJ_V2 = DOC_OBJ.replace("<description>child</description>", "<description>child v2</description>")


def _short(oid: pygit2.Oid) -> str:
    return str(oid)[:7]


def _revise_and_ack_on_branch(repo: pygit2.Repository):
    """Branch A revises the parent and acks the link; main makes its own commit.

    Returns (a_revise, a_ack, main_tip).
    """
    base = repo.head.target
    a_revise = _commit_on(repo, [base], {_PARENT_REL: PARENT_OBJ_V2}, "revise parent")
    a_ack = _commit_on(repo, [a_revise], {_LINKS_REL: _link_xml(_short(a_revise))}, "ack")
    main_tip = _commit_on(repo, [base], {_DOC_OBJ_REL: _DOC_OBJ_V2}, "main work")
    return a_revise, a_ack, main_tip


def test_ack_on_branch_stays_ok_when_main_merges_branch(repo_tree):
    """main pulls the branch: the merge commit itself did not change the parent."""
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    a_revise, a_ack, main_tip = _revise_and_ack_on_branch(repo)
    merge = _commit_on(
        repo,
        [main_tip, a_ack],
        {_PARENT_REL: PARENT_OBJ_V2, _LINKS_REL: _link_xml(_short(a_revise))},
        "merge branch A",
    )
    repo.reset(merge, pygit2.GIT_RESET_HARD)

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.OK
    assert statuses[0].current_hash == _short(a_revise)


def test_ack_on_branch_stays_ok_when_branch_merges_main(repo_tree):
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    a_revise, a_ack, main_tip = _revise_and_ack_on_branch(repo)
    merge = _commit_on(repo, [a_ack, main_tip], {_DOC_OBJ_REL: _DOC_OBJ_V2}, "merge main")
    repo.reset(merge, pygit2.GIT_RESET_HARD)

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.OK
    assert statuses[0].current_hash == _short(a_revise)


def test_ack_on_branch_stays_ok_after_fast_forward(repo_tree):
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    a_revise, a_ack, _main_tip = _revise_and_ack_on_branch(repo)
    repo.reset(a_ack, pygit2.GIT_RESET_HARD)

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.OK
    assert statuses[0].current_hash == _short(a_revise)


def _assert_ack_invalidated(repo, doc, tip, a_revise, new_revise):
    """The rewritten history gave the parent a new hash the ack doesn't know."""
    repo.reset(tip, pygit2.GIT_RESET_HARD)
    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].link.ack == _short(a_revise)
    assert statuses[0].current_hash == _short(new_revise)


def test_squash_merge_invalidates_ack(repo_tree):
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    a_revise, _a_ack, main_tip = _revise_and_ack_on_branch(repo)
    squash = _commit_on(
        repo,
        [main_tip],
        {_PARENT_REL: PARENT_OBJ_V2, _LINKS_REL: _link_xml(_short(a_revise))},
        "squash branch A",
    )
    _assert_ack_invalidated(repo, doc, squash, a_revise, squash)


@pytest.mark.parametrize("how", ["rebase", "cherry-pick"])
def test_replayed_commits_invalidate_ack(repo_tree, how):
    """A rebase and a cherry-pick both replay the commits on a new parent."""
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    a_revise, _a_ack, main_tip = _revise_and_ack_on_branch(repo)
    replayed = _commit_on(repo, [main_tip], {_PARENT_REL: PARENT_OBJ_V2}, f"{how}: revise")
    tip = _commit_on(
        repo, [replayed], {_LINKS_REL: _link_xml(_short(a_revise))}, f"{how}: ack"
    )
    _assert_ack_invalidated(repo, doc, tip, a_revise, replayed)


def test_add_link_right_after_merge_is_ok(repo_tree):
    """main revises the parent, an unrelated branch is merged, then a link is
    added: add_link's ack and the scan's current hash must agree."""
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    base = repo.head.target
    main_revise = _commit_on(repo, [base], {_PARENT_REL: PARENT_OBJ_V2}, "revise parent")
    feat = _commit_on(repo, [base], {_DOC_OBJ_REL: _DOC_OBJ_V2}, "unrelated work")
    merge = _commit_on(repo, [main_revise, feat], {_DOC_OBJ_REL: _DOC_OBJ_V2}, "merge feat")
    repo.reset(merge, pygit2.GIT_RESET_HARD)
    (doc / "links" / "DOC-1.xml").unlink()

    ack = add_link(doc, "DOC-1", "PARENT", "PARENT-42", "satisfies")

    assert ack == _short(main_revise)
    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.OK


def test_merge_resolution_with_new_parent_text_is_suspect(repo_tree):
    """A conflict resolution that writes text neither side had is a change."""
    root, doc, _c1 = repo_tree
    repo = pygit2.Repository(str(root))
    base = repo.head.target
    a_revise = _commit_on(repo, [base], {_PARENT_REL: PARENT_OBJ_V2}, "branch revise")
    main_revise = _commit_on(
        repo, [base], {_PARENT_REL: PARENT_OBJ_V2.replace("revised", "main")}, "main revise"
    )
    main_ack = _commit_on(
        repo, [main_revise], {_LINKS_REL: _link_xml(_short(main_revise))}, "ack"
    )
    merge = _commit_on(
        repo,
        [main_ack, a_revise],
        {_PARENT_REL: PARENT_OBJ_V2.replace("revised", "merged")},
        "merge with resolution",
    )
    repo.reset(merge, pygit2.GIT_RESET_HARD)

    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.SUSPECT
    assert statuses[0].current_hash == _short(merge)


def test_add_link_to_target_with_newer_figure_is_ok(repo_tree):
    """add_link acks the figure commit when the figure changed after the XML."""
    root, doc, _c1 = repo_tree
    figures = root / "PARENT" / "figures"
    figures.mkdir()
    (figures / "PARENT-42.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>\n',
        encoding="utf-8",
    )
    repo = pygit2.Repository(str(root))
    c_fig = _commit_all(repo, "add figure to parent")
    (doc / "links" / "DOC-1.xml").unlink()

    ack = add_link(doc, "DOC-1", "PARENT", "PARENT-42", "satisfies")

    assert ack == c_fig
    statuses = scan_document(load_document(doc), doc)
    assert statuses[0].state is LinkState.OK


# --- MELLON-TRACE-SRS-77 / -79: a link target never reaches outside the repository ---


def guard_paths_outside(monkeypatch, root: Path) -> tuple[list[str], list[str]]:
    """Record each path that the file system is asked about, split into those
    inside `root` and those outside it. A path outside is answered as missing
    without being looked up, so a test never reaches another folder or share."""
    inside: list[str] = []
    outside: list[str] = []
    top = os.path.normcase(os.path.abspath(root))

    def is_outside(path) -> bool:
        s = os.path.normcase(os.path.abspath(os.fspath(path)))
        if s.startswith(("\\\\", "//")):
            return True
        try:
            return os.path.commonpath([s, top]) != top
        except ValueError:  # another drive
            return True

    def guard(real):
        def call(self_or_path, *args, **kwargs):
            if isinstance(self_or_path, (str, os.PathLike)):
                if is_outside(self_or_path):
                    outside.append(os.fspath(self_or_path))
                    raise FileNotFoundError(os.fspath(self_or_path))
                inside.append(os.fspath(self_or_path))
            return real(self_or_path, *args, **kwargs)
        return call

    monkeypatch.setattr(os, "stat", guard(os.stat))
    monkeypatch.setattr(os, "scandir", guard(os.scandir))
    monkeypatch.setattr(os.path, "realpath", guard(os.path.realpath))
    for name in ("is_file", "is_dir", "exists", "stat", "open", "read_text", "read_bytes"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))
    return inside, outside


def _links_to(doc: Path, target_doc: str, target_id: str) -> None:
    (doc / "links" / "DOC-1.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<links source="DOC-1">\n'
        f'  <link target-doc="{target_doc}" target-id="{target_id}" type="satisfies"/>\n'
        '</links>\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("target_doc", "target_id"),
    [
        ("../OUT", "OUT-1"),  # a document folder outside the repository
        ("PARENT", "../../../OUT-1"),  # an object file outside the repository
        ("\\\\mellon-test-host\\share", "share-1"),  # a Windows share
        ("PARENT", "PARENT-42/../../../../OUT-1"),
    ],
)
def test_trace_srs_77_target_that_is_no_object_id_is_broken_without_access(
    repo_tree, monkeypatch, target_doc, target_id
):
    """MELLON-TRACE-SRS-77 and -79: a link whose target isn't an object ID is
    broken, and working out its state opens nothing outside the repository."""
    root, doc, _c1 = repo_tree
    _links_to(doc, target_doc, target_id)
    loaded = load_document(doc)
    inside, outside = guard_paths_outside(monkeypatch, root)

    statuses = scan_document(loaded, doc)

    assert outside == []
    assert [s.state for s in statuses] == [LinkState.BROKEN]
    assert statuses[0].current_hash is None
    assert inside, "the guard saw no file system access at all"


def test_trace_srs_77_an_object_id_of_another_document_is_broken(repo_tree):
    """MELLON-TRACE-SRS-77: an ID that names an object of another document
    isn't an object ID of the target document."""
    _root, doc, _c1 = repo_tree
    _links_to(doc, "PARENT", "DOC-1")

    statuses = scan_document(load_document(doc), doc)

    assert [s.state for s in statuses] == [LinkState.BROKEN]


# MELLON-TRACE-SRS blind tests
import shutil  # noqa: E402

from mellon.service.document import DocumentService  # noqa: E402
from tests.trace_support import (  # noqa: E402
    SVG,
    T0,
    Repo,
    _repo_with_never_committed_target,
    _under,
    basic_repo,
    link_status,
    recorded_paths,
)


def assert_state(root, key, state, current=None, step=""):
    """Check a link's state (and the target's last change) both from scratch and with kept answers."""
    for scratch in (True, False):
        st = link_status(root, *key, scratch=scratch)
        where = f"{step} ({'from scratch' if scratch else 'with kept answers'})"
        assert st.state.value == state, f"{where}: {key} is {st.state.value} ({st.reason}), expected {state}"
        if current is not None:
            assert st.current_hash == current[:7], (
                f"{where}: last change of {key[2]}/{key[3]} is {st.current_hash}, expected {current[:7]}"
            )


@pytest.mark.parametrize("layer", ["library"])
@pytest.mark.parametrize("source,target,state", [
    pytest.param("CHILD-1", "PARENT-43", "suspect", id="never-committed-target"),
    pytest.param("CHILD-2", "PARENT-1", "suspect", id="committed-target"),
    pytest.param("CHILD-3", "PARENT-44", "broken", id="missing-target"),
])
def test_trace_srs_45_link_with_no_acknowledged_commit_is_suspect(tmp_path, mcp, layer, source, target, state):
    """MELLON-TRACE-SRS-45: If a link has no acknowledged commit, Mellon shall report the link as
    suspect. Condition: no acknowledged commit, with a target that exists in the working copy and was
    never committed (no last change, CF-T15), a committed target, and, as the boundary, a missing
    target, which is broken (MELLON-TRACE-SRS-44 comes first)."""
    r = _repo_with_never_committed_target(tmp_path)
    if layer == "library":
        st = link_status(r.root, "CHILD", source, "PARENT", target, scratch=True)
        got = (st.state.value, st.reason)
    else:
        rows = [e for e in mcp.serve(r.root).suspects("CHILD")
                if e["source_id"] == source and e["link"]["target_id"] == target]
        got = (rows[0]["state"], rows[0]["reason"]) if rows else ("ok", "not listed")
    assert got[0] == state, f"{source} -> PARENT/{target}: {got}"


# ===========================================================================
# Changes to a target (MELLON-TRACE-SRS-59, -61, -62, -63, -64)
# ===========================================================================

KEY_42 = ("CHILD", "CHILD-1", "PARENT", "PARENT-42")


def _merge_history(tmp_path, *, keep_figure: bool, side_newer: bool):
    """base -> A (main) changes PARENT-42's object file; base -> B (side) adds its figure;
    the link is acknowledged at A; then A and B are merged."""
    r = Repo(tmp_path / "repo")
    r.add_doc("PARENT", {"PARENT-42": "original parent text"})
    r.add_doc("CHILD", {"CHILD-1": "child"}, parents=["PARENT"])
    base = r.commit("base", when=T0)
    r.write("PARENT/figures/PARENT-42.svg", SVG)
    b = r.commit("B adds the figure", parents=[base], branch="refs/heads/side", when=T0 + (300 if side_newer else 100))
    r.delete("PARENT/figures/PARENT-42.svg")
    r.set_object("PARENT", "PARENT-42", "revised parent text")
    a = r.commit("A revises the text", parents=[base], when=T0 + 200)
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-42", "satisfies", a[:7])])
    a2 = r.commit("acknowledge at A", when=T0 + 250)
    if keep_figure:
        r.write("PARENT/figures/PARENT-42.svg", SVG)
    m = r.commit("merge side", parents=[a2, b], when=T0 + 400)
    return r, {"base": base, "a": a, "b": b, "m": m}


def test_trace_srs_59_merge_taking_file_from_one_side_and_figure_from_other_is_a_change(tmp_path):
    """MELLON-TRACE-SRS-59: Mellon shall count a merge commit as a change to the target only when the
    target object's file and figure, taken together, differ between the merge commit and each of its
    parents. Condition: the merge takes A's object file and B's figure (A dated after B), so taken
    together it differs from both parents. Expected: the last change is the merge, and a link
    acknowledged at A is suspect."""
    r, c = _merge_history(tmp_path, keep_figure=True, side_newer=False)
    assert_state(r.root, KEY_42, "suspect", c["m"], "after the merge")


def test_trace_srs_59_merge_equal_to_one_parent_is_no_change(tmp_path):
    """MELLON-TRACE-SRS-59 (other side, with MELLON-TRACE-SRS-60): a merge whose target object file
    and figure equal one parent's is no change. Condition: the merge keeps A's file and drops B's
    figure; B is dated after A. Expected: the last change is A, and the link acknowledged at A is OK."""
    r, c = _merge_history(tmp_path, keep_figure=False, side_newer=True)
    assert_state(r.root, KEY_42, "ok", c["a"], "after the merge")


def _blobs(tree, prefix="") -> dict:
    out = {}
    for e in tree:
        path = f"{prefix}{e.name}"
        if e.type_str == "tree":
            out.update(_blobs(e, path + "/"))
        else:
            out[path] = (str(e.id), e.filemode)
    return out


def _assert_mode_only(r: Repo, commit_hash: str, path: str) -> None:
    """Setup check: the commit differs from its parent only in `path`'s mode."""
    c = r.git[commit_hash]
    new, old = _blobs(c.tree), _blobs(c.parents[0].tree)
    changed = {p for p in set(new) | set(old) if new.get(p) != old.get(p)}
    assert changed == {path}, f"setup: commit changed {changed}"
    assert new[path][0] == old[path][0] and new[path][1] != old[path][1], f"setup: {old[path]} -> {new[path]}"


def test_trace_srs_61_mode_only_commits_are_no_change_to_the_target(tmp_path):
    """MELLON-TRACE-SRS-61: If a commit changes only the mode of the target's files, Mellon shall count
    the commit as no change to the target. Condition: an OK link; one commit makes only the object
    file executable, a later one only the figure. Expected: the link stays OK with the same last
    change. Control: a later content change turns it suspect."""
    r = Repo(tmp_path / "repo")
    r.add_doc("PARENT", {"PARENT-1": "parent one"})
    r.add_doc("CHILD", {"CHILD-1": "child"}, parents=["PARENT"])
    r.write("PARENT/figures/PARENT-1.svg", SVG)
    c0 = r.commit("seed")
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", c0[:7])])
    r.commit("acknowledge")
    key = ("CHILD", "CHILD-1", "PARENT", "PARENT-1")
    assert_state(r.root, key, "ok", c0, "after the acknowledgement")

    xml, svg = "PARENT/objects/PARENT-1.xml", "PARENT/figures/PARENT-1.svg"
    c2 = r.commit("object file mode only", modes={xml: 0o100755})
    _assert_mode_only(r, c2, xml)
    assert_state(r.root, key, "ok", c0, "after a commit changing only the object file's mode")

    c3 = r.commit("figure mode only", modes={xml: 0o100755, svg: 0o100755})
    _assert_mode_only(r, c3, svg)
    assert_state(r.root, key, "ok", c0, "after a commit changing only the figure's mode")

    r.set_object("PARENT", "PARENT-1", "parent one, revised")
    c4 = r.commit("content change")
    assert_state(r.root, key, "suspect", c4, "after a content change (control)")


def _ok_link_repo(tmp_path) -> tuple[Repo, str]:
    r = Repo(tmp_path / "repo")
    r.add_doc("PARENT", {"PARENT-0": ("Heading", "Heading"), "PARENT-1": "parent one", "PARENT-2": "parent two"})
    r.add_doc("CHILD", {"CHILD-1": "child one"}, parents=["PARENT"])
    c0 = r.commit("seed")
    r.set_links("CHILD", "CHILD-1", [("PARENT", "PARENT-1", "satisfies", c0[:7])])
    r.commit("acknowledge")
    return r, c0


KEY_1 = ("CHILD", "CHILD-1", "PARENT", "PARENT-1")


def test_trace_srs_62_changes_to_the_targets_outgoing_links_are_no_change(tmp_path):
    """MELLON-TRACE-SRS-62: Mellon shall count a change to the target object's outgoing links as no
    change to the target. Condition: an OK link to PARENT-1; commits that give PARENT-1 an outgoing
    link, acknowledge that link, then remove it. Expected: the link stays OK, last change unchanged."""
    r, c0 = _ok_link_repo(tmp_path)
    assert_state(r.root, KEY_1, "ok", c0, "before")
    r.set_links("PARENT", "PARENT-1", [("PARENT", "PARENT-2", "refines", None)])
    r.commit("PARENT-1 gets an outgoing link")
    assert_state(r.root, KEY_1, "ok", c0, "after PARENT-1 got an outgoing link")
    r.set_links("PARENT", "PARENT-1", [("PARENT", "PARENT-2", "refines", c0[:7])])
    r.commit("acknowledge PARENT-1's link")
    assert_state(r.root, KEY_1, "ok", c0, "after PARENT-1's link was acknowledged")
    r.set_links("PARENT", "PARENT-1", [])
    r.commit("remove PARENT-1's link")
    assert_state(r.root, KEY_1, "ok", c0, "after PARENT-1's link was removed")


def test_trace_srs_63_changes_to_the_target_documents_manifest_are_no_change(tmp_path):
    """MELLON-TRACE-SRS-63: Mellon shall count a change to the target document's manifest as no
    change to the target. Condition: an OK link to PARENT-1; commits that move PARENT-1 in the
    outline, change an attribute definition, and change the title. Expected: OK after each."""
    r, c0 = _ok_link_repo(tmp_path)
    moved = ["PARENT-0", ("PARENT-2", ["PARENT-1"])]
    r.set_manifest("PARENT", moved)
    r.commit("move PARENT-1 under PARENT-2")
    assert_state(r.root, KEY_1, "ok", c0, "after PARENT-1 moved in the outline")
    r.set_manifest("PARENT", moved, status_options=("Draft", "Approved", "Withdrawn"))
    r.commit("add a status option")
    assert_state(r.root, KEY_1, "ok", c0, "after an attribute definition changed")
    r.set_manifest("PARENT", moved, title="Renamed parent", status_options=("Draft", "Approved", "Withdrawn"))
    r.commit("rename the document")
    assert_state(r.root, KEY_1, "ok", c0, "after the title changed")


def test_trace_srs_64_changes_to_the_source_object_do_not_change_the_state(tmp_path):
    """MELLON-TRACE-SRS-64: Mellon shall work out a link's state without regard to changes to the
    link's source object. Condition: an OK link from CHILD-1; commits that change CHILD-1's
    description, then its status attribute. Expected: the link stays OK."""
    r, c0 = _ok_link_repo(tmp_path)
    r.set_object("CHILD", "CHILD-1", ("child one, rewritten",))
    r.commit("rewrite CHILD-1")
    assert_state(r.root, KEY_1, "ok", c0, "after CHILD-1's description changed")
    r.set_object("CHILD", "CHILD-1", ("child one, rewritten", "Requirement", "Approved"))
    r.commit("approve CHILD-1")
    assert_state(r.root, KEY_1, "ok", c0, "after CHILD-1's attribute changed")


# ===========================================================================
# Safeguards (MELLON-TRACE-SRS-77, -79)
# ===========================================================================

# An empty target ID is left out: Mellon reports the links file as a load error ("<link> requires
# @target-doc, @target-id, @type"), and MELLON-TRACE-SRS-77 doesn't say whether an empty target-id
# is a link with a target that isn't an object ID or a malformed file (requirement gap).
TARGETS_77 = {
    "nested-name": ("sub/x", {"sub", "x", "x.xml", "x.svg"}),
    "drive-path": ("C:/x", {"x", "x.xml", "x.svg"}),
    "rooted-path": ("/x", {"x", "x.xml", "x.svg"}),
    "trailing-space": ("PARENT-1 ", {"PARENT-1 ", "PARENT-1 .xml", "PARENT-1 .svg"}),
}


@pytest.mark.parametrize("case", list(TARGETS_77))
def test_trace_srs_77_target_that_is_no_object_id_is_broken_without_opening_files(tmp_path, case):
    """MELLON-TRACE-SRS-77: If a link's target isn't an object ID, Mellon shall report the link as
    broken without opening a file or folder for the target. Condition: target IDs 'sub/x', 'C:/x',
    '/x', '' and 'PARENT-1 ' (PARENT-1 exists; Windows drops a trailing space from a file name)."""
    target, names = TARGETS_77[case]
    r = basic_repo(tmp_path)
    r.set_links("CHILD", "CHILD-1", [("PARENT", target, "satisfies", r.c0[:7])])
    r.commit("odd target")
    warm = tmp_path / "warm"
    shutil.copytree(r.root, warm)
    DocumentService(warm).scan_suspects("CHILD")  # imports done before recording

    with recorded_paths(r.root) as (seen, outside):
        found = DocumentService(r.root).scan_suspects("CHILD")["CHILD"]
    states = [(s.link.target_id, s.state.value) for s in found if s.source_id == "CHILD-1"]
    assert states == [(target, "broken")], f"CHILD-1's link to {target!r}: {states}"
    assert any(_under(p, str(r.root)) for p in seen), "setup: the recorder saw none of Mellon's reads"
    assert outside == [], f"paths outside the repository were asked for: {outside}"
    for_target = [p for p in seen if os.path.basename(os.path.normpath(p)) in names]
    assert for_target == [], f"paths opened for target {target!r}: {for_target}"


# MELLON-TRACE-SRS-79, MELLON-STORE-SRS-229 (CV-TRACE-07): a link whose target
# document folder is a link to another project is broken; nothing behind the
# link is opened to decide that.
from mellon.suspects import target_exists  # noqa: E402
from tests.trace_support import linked_document_repo  # noqa: E402


def test_trace_srs_79_link_into_a_linked_folder_is_broken(tmp_path: Path) -> None:
    r = linked_document_repo(tmp_path)
    assert target_exists(r.root, "EVIL", "EVIL-0") is False
    states = scan_document(load_document(r.root / "CHILD"), r.root / "CHILD")
    assert [(s.link.target_doc, s.state) for s in states] == [("EVIL", LinkState.BROKEN)]
