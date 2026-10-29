"""Tests for src/mellon/merge.py — resolving git merges of documents.

Each test builds two branches on a fresh clone of the fixture bundle, merges
them with git so the merge stops with conflicts, and resolves it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pygit2
import pytest

from mellon import (
    add_link,
    add_parent,
    create_object,
    delete_object,
    load_document,
    set_attribute,
    set_description,
    set_metadata,
)
from mellon import merge
from mellon.merge import MergeError, _merge_outline
from mellon.repo import load_all

BUNDLE = Path(__file__).resolve().parents[2] / "req-test.bundle"

pytestmark = pytest.mark.skipif(not BUNDLE.exists(), reason="req-test.bundle missing")


def _git(root: Path, *args: str, when: int | None = None, check: bool = True):
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        check=check, env=env,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(BUNDLE), str(root)], check=True)
    _git(root, "config", "user.name", "tester")
    _git(root, "config", "user.email", "t@x")
    return root


def _commit(root: Path, message: str, when: int | None = None) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message, when=when)


def _branch(root: Path, name: str) -> None:
    _git(root, "checkout", "-q", "-B", name, "master")


def _merge(root: Path, into: str, other: str) -> None:
    _git(root, "checkout", "-q", into)
    r = _git(root, "merge", "--no-edit", other, check=False)
    assert r.returncode != 0, "expected the merge to stop with conflicts"


def _conflicts(root: Path) -> list[str]:
    index = pygit2.Repository(str(root)).index
    if index.conflicts is None:
        return []
    return sorted(
        next(e.path for e in entry if e is not None) for entry in index.conflicts
    )


SRS, STS, SYRS = "VCU3-SRS", "VCU3-STS", "VCU3-SyRS"
OBJ1 = "VCU3-SRS/objects/VCU3-SRS-1.xml"


def _new_req(folder: Path, parent: str, text: str) -> str:
    obj_id = create_object(folder, parent_id=parent, description=text)
    set_attribute(folder, obj_id, "type", "Requirement")
    set_attribute(folder, obj_id, "status", "Draft")
    return obj_id


def _clash(repo: Path, *, a_first: bool = True) -> None:
    """Branch a adds two requirements under SRS-14 (one links up); branch b
    adds one under SRS-19 and the STS verifies it. Both start at SRS-34."""
    srs, sts = repo / SRS, repo / STS
    early, late = 1_700_000_000, 1_700_000_100
    t_a, t_b = (early, late) if a_first else (late, early)
    _branch(repo, "a")
    a1 = _new_req(srs, "VCU3-SRS-14", "A first")
    _new_req(srs, "VCU3-SRS-14", "A second")
    add_link(srs, a1, "VCU3-SyRS", "VCU3-SyRS-45", "satisfies")
    _commit(repo, "a", when=t_a)
    _branch(repo, "b")
    b1 = _new_req(srs, "VCU3-SRS-19", "B only")
    add_link(srs, b1, "VCU3-SyRS", "VCU3-SyRS-49", "satisfies")
    add_link(sts, "VCU3-STS-1", SRS, b1, "verifies")
    _commit(repo, "b", when=t_b)
    assert a1 == b1 == "VCU3-SRS-34"
    _merge(repo, "a", "b")


def _both_edit(repo: Path, edit_a, edit_b) -> None:
    """Branches a and b each change the fixture with their own edit."""
    _branch(repo, "a")
    edit_a(repo)
    _commit(repo, "a")
    _branch(repo, "b")
    edit_b(repo)
    _commit(repo, "b")
    _merge(repo, "a", "b")


def _outline_parent(doc, obj_id: str) -> str | None:
    def walk(nodes, parent):
        for node in nodes:
            if node.ref_id == obj_id:
                return parent
            hit = walk(node.children, node.ref_id)
            if hit is not None:
                return hit
        return None

    return walk(doc.outline, None)


def _links_to(doc, source: str) -> set[tuple[str, str]]:
    return {(l.target_doc, l.target_id) for l in doc.links.get(source, ())}


def test_plan_needs_a_merge_in_progress(repo: Path):
    with pytest.raises(MergeError, match="no merge is in progress"):
        merge.plan(repo)


# --- same id created on both branches ---------------------------------------------


def test_the_later_object_is_renumbered_everywhere(repo: Path):
    _clash(repo)  # a committed first: b's VCU3-SRS-34 yields
    plan = merge.plan(repo)
    assert (plan.into, plan.merging) == ("a", "b")
    [r] = plan.renumbered
    assert (r.obj_id, r.new_id, r.side, r.text) == (
        "VCU3-SRS-34", "VCU3-SRS-36", "theirs", "B only"
    )
    assert plan.by_hand == () and plan.outside == ()

    merge.resolve(repo)

    assert _conflicts(repo) == []
    docs = load_all(repo)
    srs, sts = docs[SRS].document, docs[STS].document
    assert srs.objects["VCU3-SRS-34"].description == "A first"
    assert srs.objects["VCU3-SRS-36"].description == "B only"
    assert [l.target_id for l in srs.links["VCU3-SRS-34"]] == ["VCU3-SyRS-45"]
    assert [l.target_id for l in srs.links["VCU3-SRS-36"]] == ["VCU3-SyRS-49"]
    # Each where its branch put it; no duplicate refs.
    assert _outline_parent(srs, "VCU3-SRS-34") == "VCU3-SRS-14"
    assert _outline_parent(srs, "VCU3-SRS-35") == "VCU3-SRS-14"
    assert _outline_parent(srs, "VCU3-SRS-36") == "VCU3-SRS-19"
    ids = srs.iter_outline_ids()
    assert len(ids) == len(set(ids))
    # B's STS link follows B's object.
    assert (SRS, "VCU3-SRS-36") in _links_to(sts, "VCU3-STS-1")
    assert (SRS, "VCU3-SRS-34") not in _links_to(sts, "VCU3-STS-1")
    # The git client can finish the merge.
    _git(repo, "commit", "-q", "--no-edit")


def test_whichever_side_committed_later_yields(repo: Path):
    _clash(repo, a_first=False)  # b committed first: our VCU3-SRS-34 yields
    [r] = merge.plan(repo).renumbered
    assert (r.side, r.text) == ("ours", "A first")

    merge.resolve(repo)

    srs = load_document(repo / SRS)
    assert srs.objects["VCU3-SRS-34"].description == "B only"
    assert srs.objects["VCU3-SRS-36"].description == "A first"
    assert [l.target_id for l in srs.links["VCU3-SRS-36"]] == ["VCU3-SyRS-45"]
    assert _outline_parent(srs, "VCU3-SRS-36") == "VCU3-SRS-14"
    assert _outline_parent(srs, "VCU3-SRS-34") == "VCU3-SRS-19"
    sts = load_document(repo / STS)
    assert (SRS, "VCU3-SRS-34") in _links_to(sts, "VCU3-STS-1")


# --- the same requirement changed on both branches --------------------------------


def test_different_fields_merge_by_themselves(repo: Path):
    """Rationale changed on a, text on b: adjacent lines, so git conflicts,
    but per field it's no conflict at all."""
    _both_edit(
        repo,
        lambda r: set_attribute(r / SRS, "VCU3-SRS-1", "rationale", "Mine."),
        lambda r: set_description(r / SRS, "VCU3-SRS-1", "Theirs."),
    )
    assert _conflicts(repo) == [OBJ1]
    assert merge.plan(repo).by_hand == ()
    merge.resolve(repo)
    obj = load_document(repo / SRS).objects["VCU3-SRS-1"]
    assert (obj.attrs["rationale"], obj.description) == ("Mine.", "Theirs.")
    assert _conflicts(repo) == []


def test_same_field_on_both_is_merged_by_hand(repo: Path):
    _both_edit(
        repo,
        lambda r: (
            set_description(r / SRS, "VCU3-SRS-1", "Mine."),
            set_attribute(r / SRS, "VCU3-SRS-1", "status", "Approved"),
        ),
        lambda r: (
            set_description(r / SRS, "VCU3-SRS-1", "Theirs."),
            set_attribute(r / SRS, "VCU3-SRS-1", "status", "Reviewed"),
            set_attribute(r / SRS, "VCU3-SRS-1", "priority", "Should"),
        ),
    )
    [hand] = merge.plan(repo).by_hand
    assert (hand.path, hand.title, hand.deleted_on) == (OBJ1, "VCU3-SRS-1", None)
    conflicts = {c.field: c for c in hand.conflicts}
    assert set(conflicts) == {"description", "attr:status"}
    text, status = conflicts["description"], conflicts["attr:status"]
    assert (text.label, text.kind, text.ours, text.theirs) == (
        "Text", "text", "Mine.", "Theirs."
    )
    assert (status.kind, status.ours, status.theirs) == ("value", "Approved", "Reviewed")
    assert hand.from_theirs == ("priority",)
    # What it traces to, with the parent's text.
    syrs42 = load_document(repo / SYRS).objects["VCU3-SyRS-42"].description
    assert [(l.target_id, l.link_type, l.text) for l in hand.links] == [
        ("VCU3-SyRS-42", "satisfies", " ".join(syrs42.split()))
    ]

    # Defaults: mine for every conflict; the one-sided priority comes along.
    merge.resolve(repo)
    obj = load_document(repo / SRS).objects["VCU3-SRS-1"]
    assert (obj.description, obj.attrs["status"], obj.attrs["priority"]) == (
        "Mine.", "Approved", "Should"
    )
    assert _conflicts(repo) == []

    # It can run again: a merged text, and theirs for the status.
    merge.resolve(repo, edits={OBJ1: {
        "description": "Mine and theirs.", "attr:status": "theirs",
    }})
    obj = load_document(repo / SRS).objects["VCU3-SRS-1"]
    assert (obj.description, obj.attrs["status"]) == ("Mine and theirs.", "Reviewed")


def _deleted_vs_changed(repo: Path) -> str:
    _both_edit(
        repo,
        lambda r: set_description(r / SRS, "VCU3-SRS-10", "Edited on a."),
        lambda r: delete_object(r / SRS, "VCU3-SRS-10", force=True),
    )
    [hand] = merge.plan(repo).by_hand
    assert (hand.title, hand.deleted_on, hand.conflicts) == (
        "VCU3-SRS-10", "theirs", ()
    )
    return hand.path


def test_deleted_on_one_side_changed_on_the_other_is_kept_by_default(repo: Path):
    _deleted_vs_changed(repo)
    merge.resolve(repo)
    doc = load_document(repo / SRS)
    assert doc.objects["VCU3-SRS-10"].description == "Edited on a."
    assert "VCU3-SRS-10" in doc.iter_outline_ids()
    assert _conflicts(repo) == []


def test_deleted_on_one_side_can_be_deleted(repo: Path):
    path = _deleted_vs_changed(repo)
    merge.resolve(repo, delete={path})
    doc = load_document(repo / SRS)
    assert "VCU3-SRS-10" not in doc.objects
    assert "VCU3-SRS-10" not in doc.iter_outline_ids()
    assert _conflicts(repo) == []


def test_document_properties_merge_field_by_field(repo: Path):
    """The title changed on both is a hand merge; a parent added on one side
    is simply united in."""
    _both_edit(
        repo,
        lambda r: set_metadata(r / SRS, "title", "SRS, mine"),
        lambda r: (
            set_metadata(r / SRS, "title", "SRS, theirs"),
            add_parent(r / SRS, STS),
        ),
    )
    [hand] = merge.plan(repo).by_hand
    assert hand.title == "document properties"
    [c] = hand.conflicts
    assert (c.field, c.label, c.kind, c.ours, c.theirs) == (
        "meta:title", "Title", "text", "SRS, mine", "SRS, theirs"
    )

    merge.resolve(repo, edits={hand.path: {"meta:title": "SRS, merged"}})
    doc = load_document(repo / SRS)
    assert doc.metadata.title == "SRS, merged"
    assert set(doc.parents) == {SYRS, STS}
    assert _conflicts(repo) == []


def test_links_merge_as_a_set(repo: Path):
    """Both branches add a link from VCU3-STS-1: git conflicts on the links
    file; both links are kept without asking."""
    _both_edit(
        repo,
        lambda r: add_link(r / STS, "VCU3-STS-1", SRS, "VCU3-SRS-2", "verifies"),
        lambda r: add_link(r / STS, "VCU3-STS-1", SRS, "VCU3-SRS-11", "verifies"),
    )
    assert _conflicts(repo) == ["VCU3-STS/links/VCU3-STS-1.xml"]
    assert merge.plan(repo).by_hand == ()
    merge.resolve(repo)
    targets = {t for _, t in _links_to(load_document(repo / STS), "VCU3-STS-1")}
    assert {"VCU3-SRS-1", "VCU3-SRS-2", "VCU3-SRS-11"} <= targets


def test_conflicts_outside_documents_are_reported_not_touched(repo: Path):
    def notes(text):
        return lambda r: (r / "NOTES.txt").write_text(text, encoding="utf-8")

    _both_edit(repo, notes("a\n"), notes("b\n"))
    plan = merge.plan(repo)
    assert plan.outside == ("NOTES.txt",)
    merge.resolve(repo)
    assert _conflicts(repo) == ["NOTES.txt"]


def test_merged_text_with_control_characters_is_refused(repo: Path):
    _both_edit(
        repo,
        lambda r: set_description(r / SRS, "VCU3-SRS-1", "Mine."),
        lambda r: set_description(r / SRS, "VCU3-SRS-1", "Theirs."),
    )
    with pytest.raises(MergeError, match="control character"):
        merge.resolve(repo, edits={OBJ1: {"description": "bad\x01text"}})
    assert _conflicts(repo) == [OBJ1]  # nothing written


# --- outline merge ----------------------------------------------------------------


def test_outline_merge_applies_theirs_moves_adds_and_deletes():
    base = [["H1", [["A", []], ["B", []]]], ["H2", [["C", []]]]]
    ours = [["H1", [["A", []], ["B", []], ["O", []]]], ["H2", [["C", []]]]]
    # theirs: moved A under H2 after C, deleted B, added T first under H1
    theirs = [["H1", [["T", []]]], ["H2", [["C", []], ["A", []]]]]
    merged = _merge_outline(base, ours, theirs)
    assert merged == [["H1", [["T", []], ["O", []]]], ["H2", [["C", []], ["A", []]]]]


def test_outline_merge_keeps_ours_when_both_moved():
    base = [["H1", [["A", []]]], ["H2", []], ["H3", []]]
    ours = [["H1", []], ["H2", [["A", []]]], ["H3", []]]
    theirs = [["H1", []], ["H2", []], ["H3", [["A", []]]]]
    assert _merge_outline(base, ours, theirs) == ours
