"""Tests for src/mellon/merge.py — resolving git merges of documents.

Each test builds two branches on a fresh clone of the fixture bundle, merges
them with git so the merge stops with conflicts, and resolves it.
"""

from __future__ import annotations

import os
import shutil
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
from mellon import editor, git_ops, merge
from mellon.merge import MergeError, _merge_outline
from mellon.repo import load_all
from tests.test_editor import (
    DOC,
    delete,
    new_document,
    number_of,
    populate,
    read_next,
    write_next,
)

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


# --- the next object number in a merge (MELLON-VER-SRS-64, -65) -------------
#
# Written blind from the requirements; see test_editor's object ID tests.
# These build their own repository rather than cloning the fixture.

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git executable not on PATH")
BASE_COUNT = 3  # base objects <doc>-0..<doc>-2, base number 3


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([GIT, "-C", str(root), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"setup: git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    pygit2.init_repository(str(root))
    for key, value in (
        ("core.autocrlf", "false"),
        ("user.name", "Blind Test"),
        ("user.email", "blind@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(root, "config", key, value)
    return root


def run_steps(root: Path, doc_id: str, steps, label: str) -> list[str]:
    """Apply one side's steps to one document; return the IDs it created.

    Steps: ("create", text), ("delete", n), ("describe", n, text), ("commit",).
    """
    folder = root / doc_id
    created: list[str] = []
    for step in steps:
        kind = step[0]
        if kind == "create":
            created.append(editor.create_object(folder, description=f"{label}: {step[1]}"))
        elif kind == "delete":
            delete(folder, f"{doc_id}-{step[1]}")
        elif kind == "describe":
            editor.set_description(folder, f"{doc_id}-{step[1]}", f"{label}: {step[2]}")
        elif kind == "commit":
            git_ops.commit(root, f"{label} {doc_id} intermediate")
        else:  # pragma: no cover - test bug
            raise ValueError(kind)
    return created


def conflicted_merge(tmp_path: Path, ours_steps=None, theirs_steps=None, docs=(DOC,), per_doc=None):
    """Build base, ours and theirs, then start a merge that stops with conflicts.

    ``per_doc`` maps a document ID to its ``(ours_steps, theirs_steps)``;
    without it every document in ``docs`` gets ``ours_steps``/``theirs_steps``.

    Each side's manifest is set to the number MELLON-EDIT-SRS-2 gives it (one
    more than the highest it ever created, at least the base's 3), so these
    tests check the merge alone, whatever the editor records.

    Returns ``(root, numbers, created)`` with ``numbers[(side, doc)]`` the
    side's next object number and ``created[(side, doc)]`` the IDs it created.
    """
    if per_doc is None:
        per_doc = {doc_id: (ours_steps, theirs_steps) for doc_id in docs}
    docs = tuple(per_doc)
    root = make_repo(tmp_path)
    for doc_id in docs:
        folder = new_document(root, doc_id)
        ids = populate(folder, BASE_COUNT, label="base")
        assert ids == [f"{doc_id}-{i}" for i in range(BASE_COUNT)], f"setup: base objects are {ids}"
        write_next(folder, str(BASE_COUNT))
    git_ops.commit(root, "base")
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git(root, "branch", "theirs")

    numbers: dict[tuple[str, str], int] = {}
    created: dict[tuple[str, str], list[str]] = {}
    for index, side, branch in ((0, "ours", main), (1, "theirs", "theirs")):
        git(root, "checkout", branch)
        for doc_id in docs:
            made = run_steps(root, doc_id, per_doc[doc_id][index], side)
            number = max([BASE_COUNT] + [number_of(i, doc_id) + 1 for i in made])
            write_next(root / doc_id, str(number))
            numbers[(side, doc_id)] = number
            created[(side, doc_id)] = made
        git_ops.commit(root, f"{side} final")

    git(root, "checkout", main)
    result = git(root, "merge", "--no-edit", "theirs", check=False)
    assert result.returncode != 0, f"setup: the merge should stop with conflicts:\n{result.stdout}"
    assert merge.pending(root), "setup: no merge in progress after a conflicting merge"
    return root, numbers, created


# Both sides create one object, which takes the same new ID 3 on each.
BOTH_CREATE_ONE = ([("create", "three")], [("create", "three")])


def used_and_deleted(on: str):
    """One side creates 3..6 and deletes 4..6 (number 7); the other creates 3
    (number 4). Both created ID 3, so one of them is renumbered."""
    heavy = [("create", "a"), ("create", "b"), ("create", "c"), ("create", "d"), ("commit",),
             ("delete", 6), ("delete", 5), ("delete", 4)]
    light = [("create", "x")]
    return (heavy, light) if on == "ours" else (light, heavy)


def by_hand_only(on: str):
    """Both sides change object 0's description (a same-field conflict, so no
    renumbering). One side created and deleted 3..6 (number 7), the other
    created and deleted 3 (number 4)."""
    heavy = [("describe", 0, "changed"), ("create", "a"), ("create", "b"), ("create", "c"),
             ("create", "d"), ("commit",), ("delete", 6), ("delete", 5), ("delete", 4), ("delete", 3)]
    light = [("describe", 0, "changed"), ("create", "x"), ("commit",), ("delete", 3)]
    return (heavy, light) if on == "ours" else (light, heavy)


def merged_highest(folder: Path) -> int:
    doc = load_document(folder)
    return max(number_of(i, folder.name) for i in doc.objects)


def expected_merged_number(root: Path, numbers, doc_id: str) -> int:
    """MELLON-VER-SRS-64's formula on the merged work tree."""
    return max(numbers[("ours", doc_id)], numbers[("theirs", doc_id)], merged_highest(root / doc_id) + 1)


def hand_edits_for_objects(root: Path) -> dict[str, dict[str, str]]:
    """Take ours for every field conflict in an object file. Leaves anything
    else (such as a manifest) to Mellon."""
    edits: dict[str, dict[str, str]] = {}
    for hand in merge.plan(root).by_hand:
        if "objects" in Path(hand.path).parts:
            edits[hand.path] = {c.field: c.ours for c in hand.conflicts}
    assert edits, "setup: expected a by-hand conflict in an object file"
    return edits


@needs_git
def test_ver_srs_64_both_sides_create_same_id(tmp_path):
    """MELLON-VER-SRS-64: both sides create ID 3 (numbers 4 and 4); after
    resolving, the merged number is max(4, 4, merged highest + 1)."""
    root, numbers, _ = conflicted_merge(tmp_path, *BOTH_CREATE_ONE)
    merge.resolve(root)
    expected = expected_merged_number(root, numbers, DOC)
    assert read_next(root / DOC) == str(expected), (
        f"merged next-object-number is {read_next(root / DOC)!r}, expected '{expected}'"
    )


@needs_git
@pytest.mark.parametrize("on", ["ours", "theirs"])
def test_ver_srs_64_side_that_used_and_deleted_numbers(tmp_path, on):
    """MELLON-VER-SRS-64: one side used and deleted 4..6 (number 7), the other
    reached 4; after resolving, the merged number is max(7, 4, merged highest
    + 1)."""
    root, numbers, _ = conflicted_merge(tmp_path, *used_and_deleted(on))
    merge.resolve(root)
    expected = expected_merged_number(root, numbers, DOC)
    assert read_next(root / DOC) == str(expected), (
        f"deleted numbers on {on}: merged next-object-number is {read_next(root / DOC)!r}, expected '{expected}'"
    )


@needs_git
@pytest.mark.parametrize("on", ["ours", "theirs"])
def test_ver_srs_64_side_number_above_merged_objects(tmp_path, on):
    """MELLON-VER-SRS-64: the merge has only a by-hand conflict. One side's
    number is 7 after creating and deleting 3..6, the other's is 4, and the
    merged document holds objects 0..2, so the merged number is max(7, 4, 3) =
    7, not 3 (which would give out 3..6 again)."""
    root, _, _ = conflicted_merge(tmp_path, *by_hand_only(on))
    merge.resolve(root, edits=hand_edits_for_objects(root))
    assert read_next(root / DOC) == "7", (
        f"side with deleted numbers is {on}: merged next-object-number is {read_next(root / DOC)!r}, expected '7'"
    )


@needs_git
def test_ver_srs_64_each_merged_document(tmp_path):
    """MELLON-VER-SRS-64 ('each merged document'): two documents conflict in the
    same merge, AAA with the used-and-deleted numbers on ours and BBB with them
    on theirs; each one's number follows the formula on its own objects."""
    per_doc = {"AAA": used_and_deleted("ours"), "BBB": used_and_deleted("theirs")}
    docs = tuple(per_doc)
    root, numbers, _ = conflicted_merge(tmp_path, per_doc=per_doc)
    merge.resolve(root)
    got = {d: read_next(root / d) for d in docs}
    expected = {d: str(expected_merged_number(root, numbers, d)) for d in docs}
    assert got == expected, f"merged next-object-numbers are {got}, expected {expected}"


@needs_git
def test_ver_srs_65_renumbered_at_or_above_both_numbers(tmp_path):
    """MELLON-VER-SRS-65: both sides create ID 3 (numbers 4 and 4); the plan
    renumbers one to a number of at least 4."""
    root, numbers, _ = conflicted_merge(tmp_path, *BOTH_CREATE_ONE)
    floor = max(numbers[("ours", DOC)], numbers[("theirs", DOC)])
    renumbered = [r for r in merge.plan(root).renumbered if r.doc_id == DOC]
    assert renumbered, "setup: expected the plan to renumber one of the two objects created as ID 3"
    low = [r.new_id for r in renumbered if number_of(r.new_id) < floor]
    assert not low, f"renumbered to {low}, below both sides' next object number {floor}"


@needs_git
@pytest.mark.parametrize("on", ["ours", "theirs"])
def test_ver_srs_65_plan_skips_numbers_one_side_used_and_deleted(tmp_path, on):
    """MELLON-VER-SRS-65: one side used and deleted 4..6 (number 7), the other
    never saw them (number 4); the plan renumbers the clashing ID 3 to 7 or
    higher, never to 4..6."""
    root, numbers, _ = conflicted_merge(tmp_path, *used_and_deleted(on))
    floor = max(numbers[("ours", DOC)], numbers[("theirs", DOC)])
    renumbered = [r for r in merge.plan(root).renumbered if r.doc_id == DOC]
    assert renumbered, "setup: expected the plan to renumber one of the two objects created as ID 3"
    low = [r.new_id for r in renumbered if number_of(r.new_id) < floor]
    assert not low, f"deleted numbers on {on}: renumbered to {low}, below the next object number {floor}"


@needs_git
@pytest.mark.parametrize("on", ["ours", "theirs"])
def test_ver_srs_65_resolved_object_skips_numbers_one_side_used_and_deleted(tmp_path, on):
    """MELLON-VER-SRS-65: as above, checked in the resolved work tree: the
    merged document holds 0..3 and one renumbered object, whose number is 7
    or higher."""
    root, numbers, _ = conflicted_merge(tmp_path, *used_and_deleted(on))
    floor = max(numbers[("ours", DOC)], numbers[("theirs", DOC)])
    merge.resolve(root)
    known = {f"{DOC}-{i}" for i in range(BASE_COUNT + 1)}
    extra = sorted(set(load_document(root / DOC).objects) - known)
    assert len(extra) == 1, f"setup: expected one renumbered object beside {sorted(known)}, found {extra}"
    assert number_of(extra[0]) >= floor, (
        f"deleted numbers on {on}: renumbered object is {extra[0]}, below the next object number {floor}"
    )


@needs_git
@pytest.mark.parametrize("on", ["ours", "theirs"])
def test_ver_srs_64_create_after_merge_gives_an_id_never_given_out(tmp_path, on):
    """MELLON-VER-SRS-64 and -65 with MELLON-EDIT-SRS-1 (the definition: an
    object ID is never given to a second object): after resolving, a new
    object gets an ID that neither branch nor the renumbering ever gave out."""
    root, _, created = conflicted_merge(tmp_path, *used_and_deleted(on))
    merge.resolve(root)
    issued = {f"{DOC}-{i}" for i in range(BASE_COUNT)}
    issued |= set(created[("ours", DOC)]) | set(created[("theirs", DOC)])
    issued |= set(load_document(root / DOC).objects)
    new_id = editor.create_object(root / DOC, description="after merge")
    assert new_id not in issued, f"after the merge {new_id} was given out again; already given: {sorted(issued)}"


# Beyond the blind tests: documents from a Mellon that doesn't record the
# number, as the fixture's are.


def test_resolve_records_the_number_in_a_document_neither_side_numbered(repo: Path):
    """MELLON-VER-SRS-64 for a document whose manifest neither side touched
    and which records no number: resolving records one more than its
    highest object number, VCU3-SRS-33."""
    _both_edit(
        repo,
        lambda r: set_description(r / SRS, "VCU3-SRS-1", "A text"),
        lambda r: set_description(r / SRS, "VCU3-SRS-1", "B text"),
    )
    merge.resolve(repo)
    assert _conflicts(repo) == []
    assert read_next(repo / SRS) == "34"


def test_a_side_without_a_number_counts_as_0(repo: Path):
    """MELLON-VER-SRS-64, -65 when theirs comes from a Mellon that doesn't
    record the number: ours used and deleted VCU3-SRS-35, so the clashing
    VCU3-SRS-34 of theirs becomes VCU3-SRS-36, and resolving again gives
    the same number."""
    srs = repo / SRS
    _branch(repo, "a")
    _new_req(srs, "VCU3-SRS-14", "A first")
    _new_req(srs, "VCU3-SRS-14", "A second")
    delete_object(srs, "VCU3-SRS-35")
    _commit(repo, "a", when=1_700_000_000)
    _branch(repo, "b")
    _new_req(srs, "VCU3-SRS-19", "B only")
    write_next(srs, None)
    _commit(repo, "b", when=1_700_000_100)
    _merge(repo, "a", "b")

    [r] = merge.plan(repo).renumbered
    assert (r.obj_id, r.new_id, r.side) == ("VCU3-SRS-34", "VCU3-SRS-36", "theirs")
    merge.resolve(repo)
    assert read_next(srs) == "37"
    merge.resolve(repo)
    assert read_next(srs) == "37"
    assert load_document(srs).objects["VCU3-SRS-36"].description == "B only"


# An object ID whose part after the document ID isn't a whole number of ASCII
# digits has no object number, as git clients and hand edits can make.
UNNUMBERED = f"{DOC}-²"


def _write_unnumbered(folder: Path, text: str) -> None:
    (folder / "objects" / f"{UNNUMBERED}.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<object id="{UNNUMBERED}">\n'
        f"  <description>{text}</description>\n</object>\n",
        encoding="utf-8",
    )


def unnumbered_merge(tmp_path: Path, *, in_base: bool) -> Path:
    """Base DOC-0..2 (number 3); each side creates DOC-3 (number 4), and the merge
    stops on that conflict. DOC-² is in the base (`in_base`), or written by hand on
    each side with different text."""
    root = make_repo(tmp_path)
    folder = new_document(root, DOC)
    populate(folder, BASE_COUNT, label="base")
    write_next(folder, str(BASE_COUNT))
    if in_base:
        _write_unnumbered(folder, "base")
    git_ops.commit(root, "base")
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git(root, "branch", "theirs")
    for side, branch in (("ours", main), ("theirs", "theirs")):
        git(root, "checkout", branch)
        if not in_base:
            _write_unnumbered(folder, side)
        assert editor.create_object(folder, description=f"{side}: three") == f"{DOC}-3", "setup"
        write_next(folder, str(BASE_COUNT + 1))
        git_ops.commit(root, f"{side} final")
    git(root, "checkout", main)
    result = git(root, "merge", "--no-edit", "theirs", check=False)
    assert result.returncode != 0, f"setup: the merge should stop with conflicts:\n{result.stdout}"
    return root


@needs_git
@pytest.mark.parametrize("in_base", [True, False], ids=["in-base", "on-both-sides"])
def test_object_id_without_a_number_is_renumbered_past_both_numbers(tmp_path, in_base):
    """MELLON-VER-SRS-65: with DOC-² in the merge, the plan still renumbers the
    objects created under one ID on both sides, each to at least 4, both sides' next
    object number. DOC-² has no object number, so it can't lower or raise one."""
    root = unnumbered_merge(tmp_path, in_base=in_base)
    renumbered = [r for r in merge.plan(root).renumbered if r.doc_id == DOC]
    assert renumbered, "setup: expected the plan to renumber one of the two objects created as DOC-3"
    low = [r.new_id for r in renumbered if number_of(r.new_id) < BASE_COUNT + 1]
    assert not low, f"renumbered to {low}, below both sides' next object number {BASE_COUNT + 1}"


@needs_git
def test_object_id_without_a_number_leaves_the_merged_number(tmp_path):
    """MELLON-VER-SRS-64: with DOC-² in the base, resolving sets the merged next
    object number to max(4, 4, one more than the highest object number among the
    merged objects), DOC-² having none."""
    root = unnumbered_merge(tmp_path, in_base=True)
    merge.resolve(root)
    stems = [p.stem for p in (root / DOC / "objects").glob("*.xml")]
    assert UNNUMBERED in stems, f"setup: {UNNUMBERED} should be kept: {stems}"
    highest = max(number_of(s) for s in stems if s != UNNUMBERED)
    assert read_next(root / DOC) == str(max(BASE_COUNT + 1, highest + 1))


# MELLON-VER-SRS blind tests
import stat  # noqa: E402

from tests.ver_support import (  # noqa: E402
    SVG_BLUE,
    _attr,
    _branch as ver_branch,
    _changed,
    _commit as ver_commit,
    _description,
    _git as ver_git,
    _git_cli_home,  # noqa: F401, F811 -- fixture
    _index_entries,
    _links,
    _make_doc_repo,
    _manifest,
    _merge_stops,
    _obj,
    _tree,
    _unmerged,
    _work_files,
    _write,
    _xml,
)


def test_ver_srs_310_criss_cross_merge_uses_the_base_git_merge_base_prints(tmp_path):
    """MELLON-VER-SRS-310: When ours and theirs have more than one merge base,
    Mellon merges from the one that `git merge-base` reports. Two fields are
    built so that, from either base, one takes ours and the other theirs."""
    root = _make_doc_repo(tmp_path / "repo")
    f = root / "DOC/objects/DOC-1.xml"
    ver_branch(root, "other")
    _write(f, _obj("DOC-1", "From X.", priority="High"))
    x1 = ver_commit(root, "X1", tick=10)
    ver_git(root, "checkout", "-q", "main")
    _write(f, _obj("DOC-1", "From Y.", priority="Low"))
    y1 = ver_commit(root, "Y1", tick=11)
    ver_git(root, "merge", "--no-commit", x1, check=False)
    _write(f, _obj("DOC-1", "From X.", priority="Low"))
    ver_commit(root, "Y2 merges X1", tick=12)
    ver_git(root, "checkout", "-q", "other")
    ver_git(root, "merge", "--no-commit", y1, check=False)
    _write(f, _obj("DOC-1", "From Y.", priority="High"))
    ver_commit(root, "X2 merges Y1", tick=13)
    _merge_stops(root, "other", "DOC/objects/DOC-1.xml")
    bases = ver_git(root, "merge-base", "--all", "HEAD", "MERGE_HEAD").stdout.split()
    assert sorted(bases) == sorted([x1, y1]), "the history doesn't have two merge bases"
    base = ver_git(root, "merge-base", "HEAD", "MERGE_HEAD").stdout.strip()
    base_file = _tree(root, base)["DOC/objects/DOC-1.xml"]
    ours = {"priority": "Low", "description": "From X."}
    theirs = {"priority": "High", "description": "From Y."}
    at_base = {"priority": _attr(base_file, "priority"), "description": _description(base_file)}
    expected = {k: theirs[k] if ours[k] == at_base[k] else ours[k] for k in ours}

    merge.resolve(root)

    merged = f.read_bytes()
    assert {"priority": _attr(merged, "priority"), "description": _description(merged)} == expected, (
        f"merged from a base other than {base[:7]}"
    )


@pytest.mark.parametrize("deleting", ["ours", "theirs"])
def test_ver_srs_313_object_deleted_on_one_side_stays_deleted(tmp_path, deleting):
    """MELLON-VER-SRS-313: When only one side of a merge deleted an object,
    Mellon takes that side's version: the object stays deleted, with no
    object file and no place in the outline. Both sides changed the title,
    so the manifest conflicts and Mellon merges it."""
    root = _make_doc_repo(tmp_path / "repo")

    def delete_side(title):
        (root / "DOC/objects/DOC-2.xml").unlink()
        _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1", "DOC-3"], title=title))

    def change_side(title):
        _write(root / "DOC/objects/DOC-1.xml", _obj("DOC-1", "One, changed."))
        _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1", "DOC-2", "DOC-3"], title=title))

    ver_branch(root, "side")
    (delete_side if deleting == "theirs" else change_side)("Theirs title")
    ver_commit(root, "side", tick=3)
    ver_git(root, "checkout", "-q", "main")
    (delete_side if deleting == "ours" else change_side)("Ours title")
    ver_commit(root, "main", tick=4)
    _merge_stops(root, "side", "DOC/DOC.xml")

    merge.resolve(root)

    refs = [r.get("id") for r in _xml((root / "DOC/DOC.xml").read_bytes()).iter("ref")]
    assert "DOC-2" not in refs, f"DOC-2 is back in the outline: {refs}"
    assert not (root / "DOC/objects/DOC-2.xml").exists(), "DOC-2's object file is back"
    assert refs == ["DOC-1", "DOC-3"]


def _link_set(path: Path) -> dict[str, tuple[str, str | None]]:
    return {l.get("target-id"): (l.get("type"), l.get("ack")) for l in _xml(path.read_bytes()).iter("link")}


def test_ver_srs_316_link_removed_on_one_side_is_removed(tmp_path):
    """MELLON-VER-SRS-316: When one side removed a link and the other side
    left it unchanged, Mellon removes the link. Ours removes DOC-1 -> DOC-3,
    theirs adds DOC-1 -> DOC-4, so the links file conflicts."""
    objects = {f"DOC-{i}": _obj(f"DOC-{i}", f"Object {i}.") for i in range(1, 5)}
    lf = "DOC/links/DOC-1.xml"
    base_links = [("DOC", "DOC-2", "refines", None), ("DOC", "DOC-3", "refines", None)]
    root = _make_doc_repo(tmp_path / "repo", objects, extra={lf: _links("DOC-1", base_links)})
    ver_branch(root, "side")
    _write(root / lf, _links("DOC-1", base_links + [("DOC", "DOC-4", "refines", None)]))
    ver_commit(root, "theirs adds DOC-4", tick=3)
    ver_git(root, "checkout", "-q", "main")
    _write(root / lf, _links("DOC-1", base_links[:1]))
    ver_commit(root, "ours removes DOC-3", tick=4)
    _merge_stops(root, "side", lf)

    merge.resolve(root)

    assert sorted(_link_set(root / lf)) == ["DOC-2", "DOC-4"]


@pytest.mark.parametrize("field", ["ack", "type"])
def test_ver_srs_317_link_changed_on_both_sides_keeps_ours(tmp_path, field):
    """MELLON-VER-SRS-317: When both sides changed the link type, or the
    acknowledged commit, of one link to different values, Mellon keeps the
    link as ours has it."""
    objects = {f"DOC-{i}": _obj(f"DOC-{i}", f"Object {i}.") for i in range(1, 3)}
    lf = "DOC/links/DOC-1.xml"

    def link(ltype="refines", ack="1111111"):
        return _links("DOC-1", [("DOC", "DOC-2", ltype, ack)])

    root = _make_doc_repo(tmp_path / "repo", objects, extra={lf: link()})
    ours, theirs = (("refines", "2222222"), ("refines", "3333333")) if field == "ack" else (
        ("satisfies", "1111111"), ("verifies", "1111111"))
    ver_branch(root, "side")
    _write(root / lf, link(*theirs))
    ver_commit(root, "theirs", tick=3)
    ver_git(root, "checkout", "-q", "main")
    _write(root / lf, link(*ours))
    ver_commit(root, "ours", tick=4)
    _merge_stops(root, "side", lf)

    merge.resolve(root)

    assert _link_set(root / lf) == {"DOC-2": ours}


def test_ver_srs_321_parent_documents_added_on_both_sides_are_all_kept(tmp_path):
    """MELLON-VER-SRS-321: When both sides changed a document's parent
    documents, Mellon keeps each parent document that either side declares:
    the base parent, ours' P1 and theirs' P2."""
    extra = {}
    for p in ("BASEP", "PA", "PB"):
        extra[f"{p}/{p}.xml"] = _manifest(p, [f"{p}-1"])
        extra[f"{p}/objects/{p}-1.xml"] = _obj(f"{p}-1", f"{p} object.")
    objects = {"DOC-1": _obj("DOC-1", "One.")}
    root = _make_doc_repo(tmp_path / "repo", objects, extra=extra, parents=("BASEP",))
    ver_branch(root, "side")
    _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1"], title="Base title", parents=("BASEP", "PB")))
    ver_commit(root, "theirs adds PB", tick=3)
    ver_git(root, "checkout", "-q", "main")
    _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1"], title="Base title", parents=("BASEP", "PA")))
    ver_commit(root, "ours adds PA", tick=4)
    _merge_stops(root, "side", "DOC/DOC.xml")

    merge.resolve(root)

    parents = sorted(p.get("doc") for p in _xml((root / "DOC/DOC.xml").read_bytes()).iter("parent"))
    assert parents == ["BASEP", "PA", "PB"]


def test_ver_srs_337_renumbered_object_takes_its_files_with_the_new_id(tmp_path):
    """MELLON-VER-SRS-337: When Mellon renumbers an object in a merge, Mellon
    gives the object's object file, links file and figure the new object ID.
    Theirs' DOC-3, created later, has a figure and a links file; the figure's
    content is unchanged."""
    objects = {"DOC-1": _obj("DOC-1", "One."), "DOC-2": _obj("DOC-2", "Two.")}
    root = _make_doc_repo(tmp_path / "repo", objects, next_number=3)
    ver_git(root, "checkout", "-q", "main")
    _write(root / "DOC/objects/DOC-3.xml", _obj("DOC-3", "Ours' new object."))
    _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1", "DOC-2", "DOC-3"], title="Base title", next_number=4))
    ver_commit(root, "ours creates DOC-3", tick=3)
    ver_branch(root, "side", "main~1")
    _write(root / "DOC/objects/DOC-3.xml", _obj("DOC-3", "Theirs' new object."))
    _write(root / "DOC/figures/DOC-3.svg", SVG_BLUE)
    _write(root / "DOC/links/DOC-3.xml", _links("DOC-3", [("DOC", "DOC-1", "refines", None)]))
    _write(root / "DOC/DOC.xml", _manifest("DOC", ["DOC-1", "DOC-2", "DOC-3"], title="Base title", next_number=4))
    ver_commit(root, "theirs creates DOC-3, later", tick=9)
    _merge_stops(root, "side", "DOC/objects/DOC-3.xml")
    plan = merge.plan(root)
    assert len(plan.renumbered) == 1, plan.renumbered
    new_id = plan.renumbered[0].new_id
    assert new_id not in ("DOC-1", "DOC-2", "DOC-3")

    merge.resolve(root)

    doc = root / "DOC"
    assert _description((doc / "objects/DOC-3.xml").read_bytes()) == "Ours' new object."
    moved = _xml((doc / f"objects/{new_id}.xml").read_bytes())
    assert (moved.get("id"), moved.findtext("description")) == (new_id, "Theirs' new object.")
    assert _xml((doc / f"links/{new_id}.xml").read_bytes()).get("source") == new_id
    assert (doc / f"figures/{new_id}.svg").read_bytes() == SVG_BLUE.encode(), "the figure's content changed"
    assert not (doc / "figures/DOC-3.svg").exists(), "the figure stayed under the old ID"
    assert not (doc / "links/DOC-3.xml").exists(), "the links file stayed under the old ID"


def _resolve_fails_partway(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """Two conflicting object files; a clean resolution in a copy, then a
    resolution of the original that fails on the read-only second file after
    writing other files, and a second resolution once the file is writable."""
    root = _make_doc_repo(tmp_path / "repo")
    files = ["DOC/objects/DOC-1.xml", "DOC/objects/DOC-2.xml"]
    ver_branch(root, "side")
    for rel in files:
        _write(root / rel, _obj(Path(rel).stem, f"Theirs {Path(rel).stem}."))
    ver_commit(root, "theirs", tick=3)
    ver_git(root, "checkout", "-q", "main")
    for rel in files:
        _write(root / rel, _obj(Path(rel).stem, f"Ours {Path(rel).stem}."))
    ver_commit(root, "ours", tick=4)
    _merge_stops(root, "side", *files)
    edits = {rel: {"description": f"Merged {Path(rel).stem}."} for rel in files}
    reference = tmp_path / "reference"
    shutil.copytree(root, reference)
    merge.resolve(reference, edits=edits)

    before = _work_files(root, "DOC")
    stuck = root / files[1]
    stuck.chmod(stat.S_IREAD)
    try:
        with pytest.raises(Exception):
            merge.resolve(root, edits=edits)
        written = sorted(_changed(before, _work_files(root, "DOC")))
    finally:
        stuck.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert written and files[1] not in written, f"setup: the first resolution didn't fail partway ({written})"

    merge.resolve(root, edits=edits)
    return root, reference, files


def test_ver_srs_366_resolving_again_after_a_partial_failure_writes_the_merged_files(tmp_path):
    """MELLON-VER-SRS-366: If resolving a merge fails partway, Mellon completes
    the resolution when the user resolves again: every file in the document
    folder is as a resolution that never failed wrote it, and no file is unmerged."""
    root, reference, _ = _resolve_fails_partway(tmp_path)

    assert _work_files(root, "DOC") == _work_files(reference, "DOC"), "the files differ from a clean resolution"
    assert _unmerged(root) == [], "a file is still unmerged"


def test_ver_srs_366_resolving_again_after_a_partial_failure_records_the_same_index(tmp_path):
    """MELLON-VER-SRS-366: once resolved again, the git index holds what a
    resolution that never failed records, so the git client's merge commit
    is the same."""
    root, reference, _ = _resolve_fails_partway(tmp_path)

    assert _index_entries(root) == _index_entries(reference), "the git index differs from a clean resolution"
