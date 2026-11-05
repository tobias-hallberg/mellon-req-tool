"""Tests for the one-walk last-commit index.

The index replaces per-target walks in the suspect scan, where a wrong answer
hides a suspect link. So most of this compares it with the rule it must follow,
`last_commit_for_paths`, and with `git log`, on random commit graphs.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

import pygit2
import pytest

from mellon import link_index
from mellon.check import main as check_main
from mellon.editor import add_link
from mellon.git_ops import last_commit_for_paths
from mellon.link_index import current_hashes, from_scratch

BLOB = pygit2.GIT_FILEMODE_BLOB
EXECUTABLE = pygit2.GIT_FILEMODE_BLOB_EXECUTABLE
IDS = [f"D-{i}" for i in range(1, 7)]
# In no commit at all.
NEVER = "D-99"


@pytest.fixture(autouse=True)
def _forget_kept_answers():
    link_index._known.clear()
    link_index._loaded_gitdirs.clear()
    yield
    link_index._known.clear()
    link_index._loaded_gitdirs.clear()


def _obj(i: str) -> str:
    return f"D/objects/{i}.xml"


def _fig(i: str) -> str:
    return f"D/figures/{i}.svg"


def _commit(
    repo: pygit2.Repository,
    parents: list[pygit2.Oid],
    files: dict[str, tuple[bytes, int]],
    when: int = 1_700_000_000,
) -> pygit2.Oid:
    index = pygit2.Index()
    for path, (data, mode) in files.items():
        index.add(pygit2.IndexEntry(path, repo.create_blob(data), mode))
    tree = index.write_tree(repo)
    sig = pygit2.Signature("t", "t@example.invalid", when, 0)
    return repo.create_commit(None, sig, sig, "c", tree, parents)


def _random_history(
    repo: pygit2.Repository, rng: random.Random, size: int, *, modes: bool
) -> list[pygit2.Oid]:
    """A random commit graph: branches, merges taking either side or new text,
    figures, deletions, recreations, shuffled commit times and, optionally,
    mode-only changes."""
    states: list[dict[str, tuple[bytes, int]]] = [
        {_obj(i): (b"v0", BLOB) for i in IDS[:4]}
    ]
    commits = [_commit(repo, [], states[0], rng.randint(1, 10**9))]
    for n in range(1, size):
        if len(commits) > 2 and rng.random() < 0.25:
            a, b = rng.sample(range(len(commits)), 2)
            parents = [commits[a], commits[b]]
            state = dict(states[a])
            for path in set(states[a]) | set(states[b]):
                if rng.random() < 0.5:
                    if path in states[b]:
                        state[path] = states[b][path]
                    else:
                        state.pop(path, None)
        else:
            # Mostly extend a recent commit, sometimes fork an old one.
            a = len(commits) - 1 if rng.random() < 0.7 else rng.randrange(len(commits))
            parents = [commits[a]]
            state = dict(states[a])
        for _ in range(rng.randint(0, 2)):
            i = rng.choice(IDS)
            path = rng.choice((_obj(i), _obj(i), _fig(i)))
            kind = rng.random()
            if kind < 0.6:
                state[path] = (f"v{n}".encode(), BLOB)
            elif kind < 0.8:
                state.pop(path, None)
            elif kind < 0.9 and modes and path in state:
                data, mode = state[path]
                state[path] = (data, EXECUTABLE if mode == BLOB else BLOB)
            else:
                # A change outside the document's objects and figures.
                state["D/D.xml"] = (f"m{n}".encode(), BLOB)
        states.append(state)
        commits.append(_commit(repo, parents, state, rng.randint(1, 10**9)))
    return commits


def _set_head(repo: pygit2.Repository, commit: pygit2.Oid) -> None:
    repo.set_head(commit)


def _reference(repo: pygit2.Repository, i: str) -> str | None:
    return last_commit_for_paths(repo, [_obj(i), _fig(i)])


@pytest.mark.parametrize("seed", range(12))
def test_every_head_matches_a_per_target_walk(tmp_path: Path, seed: int) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    rng = random.Random(seed)
    for commit in _random_history(repo, rng, 40, modes=True):
        _set_head(repo, commit)
        link_index._known.clear()  # a fresh walk
        got = current_hashes(repo, "D", [*IDS, NEVER])
        assert {i: got[i] for i in IDS} == {i: _reference(repo, i) for i in IDS}
        assert got[NEVER] is None


@pytest.mark.parametrize("seed", range(6))
def test_matches_git_log(tmp_path: Path, seed: int) -> None:
    """Mode changes are left out: `git log` counts them and Mellon doesn't."""
    repo = pygit2.init_repository(str(tmp_path))
    rng = random.Random(100 + seed)
    commits = _random_history(repo, rng, 30, modes=False)
    for commit in rng.sample(commits, 4):
        _set_head(repo, commit)
        got = current_hashes(repo, "D", IDS)
        for i in IDS:
            out = subprocess.run(
                ["git", "-C", str(tmp_path), "log", "-1", "--format=%H", "HEAD", "--", _obj(i), _fig(i)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert got[i] == (out[:7] or None), f"{i} at {commit}"


@pytest.mark.parametrize("seed", range(8))
def test_kept_answers_stay_right_across_head_moves(tmp_path: Path, seed: int) -> None:
    """Forward, back and across branches, in random order, without forgetting."""
    repo = pygit2.init_repository(str(tmp_path))
    rng = random.Random(200 + seed)
    commits = _random_history(repo, rng, 40, modes=True)
    for commit in rng.choices(commits, k=40):
        _set_head(repo, commit)
        got = current_hashes(repo, "D", IDS)
        assert {i: got[i] for i in IDS} == {i: _reference(repo, i) for i in IDS}


def _linear(repo: pygit2.Repository, count: int) -> pygit2.Oid:
    head = _commit(repo, [], {_obj("D-1"): (b"v0", BLOB), _obj("D-2"): (b"v0", BLOB)})
    for n in range(1, count):
        head = _commit(repo, [head], {_obj("D-1"): (f"v{n}".encode(), BLOB), _obj("D-2"): (b"v0", BLOB)}, 1_700_000_000 + n)
    repo.set_head(head)
    return head


def test_empty_repo_answers_none(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    assert current_hashes(repo, "D", ["D-1"]) == {"D-1": None}


def test_covers_every_object_at_head_without_being_asked(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    head = _linear(repo, 3)
    assert current_hashes(repo, "D")["D-1"] == str(head)[:7]


def test_object_only_in_a_nested_folder(tmp_path: Path) -> None:
    """A hand-written target id with a slash still gets the per-target answer."""
    repo = pygit2.init_repository(str(tmp_path))
    head = _commit(repo, [], {"D/objects/sub/x.xml": (b"v0", BLOB)})
    repo.set_head(head)
    assert current_hashes(repo, "D", ["sub/x"])["sub/x"] == str(head)[:7]


def test_folder_named_like_an_object(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    first = _commit(repo, [], {"D/objects/x.xml/a": (b"v0", BLOB), _obj("D-1"): (b"v0", BLOB)})
    second = _commit(repo, [first], {"D/objects/x.xml/a": (b"v1", BLOB), _obj("D-1"): (b"v0", BLOB)})
    repo.set_head(second)
    got = current_hashes(repo, "D", ["x"])
    assert got["x"] == _reference(repo, "x") == str(second)[:7]
    assert got["D-1"] == str(first)[:7]


def test_objects_path_that_is_a_file(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    first = _commit(repo, [], {"D/objects": (b"not a folder", BLOB)})
    second = _commit(repo, [first], {_obj("D-1"): (b"v0", BLOB)})
    repo.set_head(second)
    assert current_hashes(repo, "D")["D-1"] == _reference(repo, "D-1") == str(second)[:7]


# --- the index file -------------------------------------------------------


def _index_file(repo: pygit2.Repository) -> Path:
    return Path(repo.path) / "mellon-link-index.json"


def test_long_walk_is_saved_and_used_by_the_next_process(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    _linear(repo, 60)
    expected = current_hashes(repo, "D")
    assert _index_file(repo).is_file()

    # A new process: only the file is left. Proof it is used: the walk stops
    # at once, so no commit needs reading beyond HEAD.
    link_index._known.clear()
    link_index._loaded_gitdirs.clear()
    assert current_hashes(repo, "D") == expected
    known = link_index._known[(repo.path, "D")]
    answers, visited = link_index._walk(
        repo, repo.head.peel(pygit2.Commit), "D", set(expected), known
    )
    assert visited == 1 and answers == expected


def test_short_walk_writes_nothing(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    _linear(repo, 5)
    current_hashes(repo, "D")
    assert not _index_file(repo).exists()


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'{"version": 999, "docs": {}}',
        b'{"version": 1, "docs": []}',
        b'{"version": 1, "docs": {"D": 1}}',
        b'{"version": 1, "docs": {"D": {"commit": 1, "hashes": {}}}}',
        b'{"version": 1, "docs": {"D": {"commit": "x", "hashes": {"D-1": 1}}}}',
    ],
)
def test_unusable_index_file_is_ignored(tmp_path: Path, content: bytes) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    head = _linear(repo, 3)
    _index_file(repo).write_bytes(content)
    assert current_hashes(repo, "D")["D-1"] == str(head)[:7]


def test_index_for_a_commit_off_history_is_harmless(tmp_path: Path) -> None:
    """After a rebase, say. The walk never reaches that commit."""
    repo = pygit2.init_repository(str(tmp_path))
    head = _linear(repo, 3)
    _index_file(repo).write_text(
        json.dumps({"version": 1, "docs": {"D": {"commit": "0" * 40, "hashes": {"D-1": "bad"}}}})
    )
    assert current_hashes(repo, "D")["D-1"] == str(head)[:7]


def test_write_failure_is_ignored(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    head = _linear(repo, 60)
    _index_file(repo).mkdir()  # can't be replaced by a file
    assert current_hashes(repo, "D")["D-1"] == str(head)[:7]


# --- the CI gate ----------------------------------------------------------


MANIFEST = """\
<?xml version="1.0" encoding="UTF-8"?>
<document id="{id}">
  <metadata><title>{id}</title></metadata>
  {parents}
  <attributes>
    <attribute name="status" kind="enum" required="true"><option>Draft</option></attribute>
  </attributes>
  <outline><ref id="{id}-1"/></outline>
</document>
"""
OBJECT = """\
<?xml version="1.0" encoding="UTF-8"?>
<object id="{id}-1">
  <attr name="status">Draft</attr>
  <description>{text}</description>
</object>
"""


def _commit_all(root: Path, message: str) -> str:
    repo = pygit2.Repository(str(root))
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("t", "t@example.invalid")
    parents = [] if repo.head_is_unborn else [repo.head.target]
    oid = repo.create_commit("HEAD", sig, sig, message, repo.index.write_tree(), parents)
    return str(oid)


def test_gate_ignores_the_index_file_and_kept_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A forged index says the revised parent is unchanged; the gate must still
    report the link as suspect, and the file must survive untouched."""
    pygit2.init_repository(str(tmp_path))
    for doc, parents in (("PARENT", ""), ("CHILD", '<parents><parent doc="PARENT"/></parents>')):
        (tmp_path / doc / "objects").mkdir(parents=True)
        (tmp_path / doc / f"{doc}.xml").write_text(MANIFEST.format(id=doc, parents=parents), encoding="utf-8")
        (tmp_path / doc / "objects" / f"{doc}-1.xml").write_text(OBJECT.format(id=doc, text="v1"), encoding="utf-8")
    first = _commit_all(tmp_path, "documents")
    assert add_link(tmp_path / "CHILD", "CHILD-1", "PARENT", "PARENT-1", "satisfies")
    _commit_all(tmp_path, "link")
    (tmp_path / "PARENT" / "objects" / "PARENT-1.xml").write_text(OBJECT.format(id="PARENT", text="v2"), encoding="utf-8")
    head = _commit_all(tmp_path, "revise parent")

    repo = pygit2.Repository(str(tmp_path))
    forged = json.dumps(
        {"version": 1, "docs": {"PARENT": {"commit": head, "hashes": {"PARENT-1": first[:7]}}}}
    )
    _index_file(repo).write_text(forged)
    link_index._known[(repo.path, "PARENT")] = link_index._Known(head, {"PARENT-1": first[:7]})

    assert check_main(["--repo-root", str(tmp_path)]) == 1
    assert "SUSPECT CHILD-1 -> PARENT/PARENT-1" in capsys.readouterr().out
    assert _index_file(repo).read_text() == forged


def test_from_scratch_restores_disk_use(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    _linear(repo, 60)
    with from_scratch():
        current_hashes(repo, "D")
        assert not _index_file(repo).exists()
    current_hashes(repo, "D")
    assert _index_file(repo).is_file()
