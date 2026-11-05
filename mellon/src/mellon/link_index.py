"""The last commit of every object in a document, from one walk of history.

A suspect link is current when its ack equals the last commit that changed its
target. `git_ops.last_commit_for_paths` answers that for one target by walking
history, which costs targets x commits over a whole document. This module
follows the same rule but moves all of a document's objects down history
together, and only reads a document's large `objects` tree for commits that
changed it.

An object's answer depends only on the commit asked about, and history below a
commit can't change. So answers kept for a commit never go stale: a walk that
reaches a remembered commit stops there, which makes a one-commit HEAD move
cost one tree diff. A rebase or branch switch just walks further back. The
answers are also kept in the git directory, so a new process needn't start over.
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import json
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pygit2

from .git_ops import _replace_file, _short, last_commit_for_paths

_INDEX_FILE = "mellon-link-index.json"
_FORMAT_VERSION = 1
# Shorter walks are cheap to redo, and not worth a disk write per commit.
_SAVE_AFTER_COMMITS = 50


@dataclass
class _Known:
    commit: str
    hashes: dict[str, str | None]


_lock = threading.Lock()
_known: dict[tuple[str, str], _Known] = {}
_loaded_gitdirs: set[str] = set()
_use_disk = True


def current_hashes(
    repo: pygit2.Repository, target_doc: str, ids: Iterable[str] = ()
) -> dict[str, str | None]:
    """Object id -> short hash of the last commit on HEAD that changed it.

    An object is `<doc>/objects/<id>.xml` and `<doc>/figures/<id>.svg` as one
    unit, and each answer equals `last_commit_for_paths` for those two paths.
    Covers every object in `target_doc` at HEAD, plus `ids`, which may name
    objects that aren't there (None if they aren't anywhere in history).

    The result may be the kept copy: read it, don't change it.
    """
    wanted = set(ids)
    # Not a name in the `objects` folder, so a tree diff of it won't show them.
    nested = {i for i in wanted if "/" in i or "\\" in i}
    wanted -= nested
    try:
        head = repo.head.peel(pygit2.Commit)
    except (pygit2.GitError, KeyError):
        return dict.fromkeys(wanted | nested)

    gitdir = repo.path
    key = (gitdir, target_doc)
    with _lock:
        if _use_disk and gitdir not in _loaded_gitdirs:
            _loaded_gitdirs.add(gitdir)
            _load(gitdir)
        known = _known.get(key)
        visited = 0
        if known is not None and known.commit == str(head.id):
            missing = wanted - known.hashes.keys()
            if missing:
                answers, visited = _walk(repo, head, target_doc, missing, None)
                known.hashes.update(answers)
            hashes = known.hashes
        else:
            wanted |= _names(repo, head.tree, f"{target_doc}/objects", ".xml")
            hashes, visited = _walk(repo, head, target_doc, wanted, known)
            _known[key] = _Known(str(head.id), hashes)
        if _use_disk and visited >= _SAVE_AFTER_COMMITS:
            _save(gitdir)
    if nested:
        hashes = dict(hashes)
        for i in nested:
            hashes[i] = last_commit_for_paths(
                repo,
                [f"{target_doc}/objects/{i}.xml", f"{target_doc}/figures/{i}.svg"],
            )
    return hashes


@contextlib.contextmanager
def from_scratch() -> Iterator[None]:
    """Inside this block, nothing kept earlier or on disk is used, and nothing
    is written to disk. For the CI gate, which shouldn't trust local state."""
    global _use_disk
    with _lock:
        saved = _use_disk
        _use_disk = False
        _known.clear()
        _loaded_gitdirs.clear()
    try:
        yield
    finally:
        with _lock:
            _use_disk = saved
            _known.clear()


def _walk(
    repo: pygit2.Repository,
    head: pygit2.Commit,
    doc: str,
    ids: set[str],
    known: _Known | None,
) -> tuple[dict[str, str | None], int]:
    """Answers for `ids`, and the number of commits visited.

    Same rule as `last_commit_for_paths`: an object moves to the first parent
    where it is identical, and a commit that differs from every parent is its
    answer. Each answer depends only on the commit and the object, so the
    visiting order doesn't matter: if clock skew brings more objects to a commit
    already visited, it is simply visited again.
    """
    objects_rel, figures_rel = f"{doc}/objects", f"{doc}/figures"
    answers: dict[str, str | None] = {}
    pending = {head.id: set(ids)}
    order = itertools.count()
    queue = [(-head.commit_time, next(order), head)]
    visited = 0
    while queue:
        commit = heapq.heappop(queue)[2]
        units = pending.pop(commit.id, None)
        if not units:
            continue
        visited += 1

        if known is not None and str(commit.id) == known.commit:
            hits = units & known.hashes.keys()
            for unit in hits:
                answers[unit] = known.hashes[unit]
            units -= hits
            if not units:
                continue

        short = _short(commit.id)
        if not commit.parents:
            present = _names(repo, commit.tree, objects_rel, ".xml")
            present |= _names(repo, commit.tree, figures_rel, ".svg")
            for unit in units:
                answers[unit] = short if unit in present else None
            continue

        objects_id = _subtree_id(commit.tree, objects_rel)
        figures_id = _subtree_id(commit.tree, figures_rel)
        for parent in commit.parents:
            changed = _changed_units(
                repo, _subtree_id(parent.tree, objects_rel), objects_id, ".xml"
            )
            changed |= _changed_units(
                repo, _subtree_id(parent.tree, figures_rel), figures_id, ".svg"
            )
            stay = units & changed
            units -= stay
            if units:
                if parent.id in pending:
                    pending[parent.id] |= units
                else:
                    pending[parent.id] = units
                    heapq.heappush(queue, (-parent.commit_time, next(order), parent))
            units = stay
            if not units:
                break
        for unit in units:
            answers[unit] = short
    return answers, visited


def _subtree_id(tree: pygit2.Tree, rel: str) -> pygit2.Oid | None:
    """The id of the folder at `rel`, without reading it. None if there is no
    such folder: no path below it can exist, so it counts as empty."""
    try:
        entry = tree[rel]
    except KeyError:
        return None
    return entry.id if isinstance(entry, pygit2.Tree) else None


def _names(repo: pygit2.Repository, tree: pygit2.Tree, rel: str, suffix: str) -> set[str]:
    """Stems of the `suffix` entries directly in the folder at `rel`."""
    folder = _subtree_id(tree, rel)
    if folder is None:
        return set()
    return {
        entry.name[: -len(suffix)]
        for entry in repo[folder]
        if entry.name.endswith(suffix)
    }


def _changed_units(
    repo: pygit2.Repository,
    old: pygit2.Oid | None,
    new: pygit2.Oid | None,
    suffix: str,
) -> set[str]:
    """Stems of the `suffix` entries that differ between two folders.

    Equal ids mean no folder is read. Compares contents only, like
    `last_commit_for_paths`, so a mode change doesn't count, and without rename
    detection, so a rename changes both names.
    """
    if old == new:
        return set()
    if old is None:
        diff = repo[new].diff_to_tree(swap=True)
    elif new is None:
        diff = repo[old].diff_to_tree()
    else:
        diff = repo[old].diff_to_tree(repo[new])
    out: set[str] = set()
    for delta in diff.deltas:
        if delta.old_file.id != delta.new_file.id:
            # A diff recurses, so a change inside a folder named like an
            # object shows up as a path below that name.
            name = delta.new_file.path.split("/", 1)[0]
            if name.endswith(suffix):
                out.add(name[: -len(suffix)])
    return out


def _load(gitdir: str) -> None:
    """Read the index file. Anything unusable is skipped: it only costs a walk."""
    try:
        data = json.loads((Path(gitdir) / _INDEX_FILE).read_bytes())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("version") != _FORMAT_VERSION:
        return
    docs = data.get("docs")
    if not isinstance(docs, dict):
        return
    for doc, entry in docs.items():
        if not isinstance(entry, dict):
            continue
        commit, hashes = entry.get("commit"), entry.get("hashes")
        if (
            isinstance(commit, str)
            and isinstance(hashes, dict)
            and all(v is None or isinstance(v, str) for v in hashes.values())
        ):
            _known.setdefault((gitdir, doc), _Known(commit, hashes))


def _save(gitdir: str) -> None:
    """Write the index file. A failure only costs a walk in the next process."""
    docs = {
        doc: {"commit": known.commit, "hashes": known.hashes}
        for (dir_, doc), known in _known.items()
        if dir_ == gitdir
    }
    data = json.dumps({"version": _FORMAT_VERSION, "docs": docs})
    with contextlib.suppress(OSError):
        _replace_file(Path(gitdir) / _INDEX_FILE, data.encode("utf-8"))
