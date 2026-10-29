"""Tests for git-backed suspect-link detection.

Each test builds a real pygit2 repo in tmp_path containing two documents
(a child DOC and a parent PARENT) so we can exercise cross-doc link scanning.
"""

from __future__ import annotations

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
