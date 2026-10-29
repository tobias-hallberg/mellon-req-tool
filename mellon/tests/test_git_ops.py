"""Tests for the small git helpers that back the MCP commit / review tools.

Uses a self-contained tmp_path git repo (initialized with pygit2) so we don't
depend on the `req-test` fixture for unit coverage. Integration against the
real commit history lives in test_mcp_git.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pygit2
import pytest

from mellon.git_ops import (
    GitError,
    check_repo_root,
    commit,
    create_issue_tag,
    create_tag,
    current_issue,
    dirty_count,
    dirty_paths_under,
    discard_paths,
    discover_repo,
    head_summary,
    increment_issue,
    issue_tags_by_commit,
    last_commit_for_path,
    last_commit_for_paths,
    list_tree_filenames,
    log_for_path,
    show_blob_at,
    tag_name_error,
    tag_names,
    validate_issue,
)


@pytest.fixture
def tmp_git(tmp_path: Path) -> Path:
    """Initialize an empty git repo in tmp_path and return its root."""
    pygit2.init_repository(str(tmp_path), bare=False)
    return tmp_path


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- commit() -----------------------------------------------------------


def test_commit_creates_initial(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "first")
    short = commit(tmp_git, "initial commit")
    assert len(short) == 7
    repo = pygit2.Repository(str(tmp_git))
    assert str(repo.head.target).startswith(short)
    assert repo.head.peel(pygit2.Commit).message == "initial commit"


def test_commit_handles_modify_add_delete(tmp_git: Path) -> None:
    _write(tmp_git, "keep.txt", "keep me")
    _write(tmp_git, "modify.txt", "before")
    _write(tmp_git, "remove.txt", "to be removed")
    commit(tmp_git, "seed")

    _write(tmp_git, "modify.txt", "after")
    _write(tmp_git, "added.txt", "new file")
    (tmp_git / "remove.txt").unlink()

    short = commit(tmp_git, "mixed")
    repo = pygit2.Repository(str(tmp_git))
    tree = repo.head.peel(pygit2.Commit).tree
    assert "keep.txt" in tree
    assert "modify.txt" in tree
    assert repo[tree["modify.txt"].id].data.decode() == "after"
    assert "added.txt" in tree
    assert "remove.txt" not in tree
    # Short hash points at the latest commit.
    assert str(repo.head.target).startswith(short)


def test_commit_with_explicit_paths(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "a1")
    _write(tmp_git, "b.txt", "b1")
    commit(tmp_git, "seed")

    _write(tmp_git, "a.txt", "a2")
    _write(tmp_git, "b.txt", "b2")

    commit(tmp_git, "only a", paths=["a.txt"])
    repo = pygit2.Repository(str(tmp_git))
    tree = repo.head.peel(pygit2.Commit).tree
    assert repo[tree["a.txt"].id].data.decode() == "a2"
    # b.txt was NOT staged; the committed version is the seed one.
    assert repo[tree["b.txt"].id].data.decode() == "b1"


def test_commit_nothing_to_commit_errors(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "hello")
    commit(tmp_git, "seed")
    with pytest.raises(GitError, match="nothing to commit"):
        commit(tmp_git, "empty")


def test_commit_with_paths_leaves_out_prestaged_files(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "a1")
    commit(tmp_git, "seed")
    _write(tmp_git, "a.txt", "a2")
    _write(tmp_git, "other.txt", "someone else's staged work")
    repo = pygit2.Repository(str(tmp_git))
    repo.index.add("other.txt")
    repo.index.write()

    commit(tmp_git, "only a", paths=["a.txt"])

    repo = pygit2.Repository(str(tmp_git))
    tree = repo.head.peel(pygit2.Commit).tree
    assert repo[tree["a.txt"].id].data.decode() == "a2"
    assert "other.txt" not in tree
    status = repo.status()
    assert "a.txt" not in status  # committed path is clean
    assert status["other.txt"] == pygit2.GIT_STATUS_INDEX_NEW  # still staged


def test_commit_refuses_while_the_index_is_locked(tmp_git: Path) -> None:
    """A stale index.lock (a crashed git client) must stop the commit before
    anything lands, not after HEAD moved."""
    _write(tmp_git, "a.txt", "a1")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    _write(tmp_git, "a.txt", "a2")
    (Path(repo.path) / "index.lock").write_bytes(b"")

    with pytest.raises(GitError, match="index is locked"):
        commit(tmp_git, "blocked")

    assert pygit2.Repository(str(tmp_git)).head.target == head


def test_commit_writes_the_index_before_committing(
    tmp_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another git program taking index.lock mid-commit makes the index write
    fail; that must happen before the commit, so a failure means nothing
    was committed (and the index isn't left staging a revert)."""
    _write(tmp_git, "a.txt", "a1")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    lock = Path(repo.path) / "index.lock"
    _write(tmp_git, "a.txt", "a2")
    real_write = pygit2.Index.write

    def write_while_locked(self, *args, **kwargs):
        lock.write_bytes(b"")  # another client got there first
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(pygit2.Index, "write", write_while_locked)
    with pytest.raises(GitError, match="index is locked"):
        commit(tmp_git, "raced")
    monkeypatch.undo()

    assert pygit2.Repository(str(tmp_git)).head.target == head
    lock.unlink()
    commit(tmp_git, "retry")  # the change is still there to commit
    tree = pygit2.Repository(str(tmp_git)).head.peel(pygit2.Commit).tree
    assert tree["a.txt"].data == b"a2"


def test_commit_refuses_when_another_commit_lands_meanwhile(
    tmp_git: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit landing while this one hashes its files (the MCP server and
    the GUI on one repo) must not be silently reverted by this commit."""
    _write(tmp_git, "mine.txt", "m1")
    _write(tmp_git, "theirs.txt", "t1")
    commit(tmp_git, "seed")
    _write(tmp_git, "mine.txt", "m2")
    real_add = pygit2.Index.add
    fired: list[str] = []

    def add_while_another_commits(self, *args, **kwargs):
        if not fired:
            fired.append("x")
            _write(tmp_git, "theirs.txt", "t2")
            fired.append(commit(tmp_git, "their commit", paths=["theirs.txt"]))
        return real_add(self, *args, **kwargs)

    monkeypatch.setattr(pygit2.Index, "add", add_while_another_commits)
    with pytest.raises(GitError, match="commit again"):
        commit(tmp_git, "my commit", paths=["mine.txt"])
    monkeypatch.undo()

    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.peel(pygit2.Commit)
    assert str(head.id).startswith(fired[1])  # their commit is the tip...
    assert head.tree["theirs.txt"].data == b"t2"  # ...and keeps its change
    commit(tmp_git, "my commit", paths=["mine.txt"])  # retrying works
    tree = pygit2.Repository(str(tmp_git)).head.peel(pygit2.Commit).tree
    assert (tree["mine.txt"].data, tree["theirs.txt"].data) == (b"m2", b"t2")


def test_commit_refuses_during_merge(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "base")
    commit(tmp_git, "base")
    repo = pygit2.Repository(str(tmp_git))
    base = repo.head.target
    _write(tmp_git, "a.txt", "theirs")
    theirs = repo.revparse_single(commit(tmp_git, "theirs")).id
    repo.reset(base, pygit2.GIT_RESET_HARD)
    _write(tmp_git, "a.txt", "ours")
    commit(tmp_git, "ours")
    repo = pygit2.Repository(str(tmp_git))
    repo.merge(theirs)  # conflicts on a.txt
    _write(tmp_git, "a.txt", "resolved")

    with pytest.raises(GitError, match="finish the merge in your git client"):
        commit(tmp_git, "resolve", paths=["a.txt"])
    # Conflicts left in the index are refused too, even without MERGE_HEAD.
    repo.state_cleanup()
    with pytest.raises(GitError, match="finish the merge in your git client"):
        commit(tmp_git, "resolve", paths=["a.txt"])


@pytest.fixture
def no_git_identity(tmp_path_factory):
    """Point every libgit2 config level outside the repo at an empty dir."""
    empty = tmp_path_factory.mktemp("no-git-config")
    levels = (
        pygit2.GIT_CONFIG_LEVEL_PROGRAMDATA,
        pygit2.GIT_CONFIG_LEVEL_SYSTEM,
        pygit2.GIT_CONFIG_LEVEL_XDG,
        pygit2.GIT_CONFIG_LEVEL_GLOBAL,
    )
    saved = {level: pygit2.settings.search_path[level] for level in levels}
    for level in levels:
        pygit2.settings.search_path[level] = str(empty)
    yield
    for level, path in saved.items():
        pygit2.settings.search_path[level] = path


def test_commit_refuses_without_identity(tmp_git: Path, no_git_identity) -> None:
    _write(tmp_git, "a.txt", "x")
    with pytest.raises(GitError, match="git config --global user.email"):
        commit(tmp_git, "seed")
    assert pygit2.Repository(str(tmp_git)).head_is_unborn


def test_commit_wraps_pygit2_errors(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "seed")
    _write(tmp_git, "sub/b.txt", "y")
    with pytest.raises(GitError, match="is a directory"):
        commit(tmp_git, "a directory is not a path to stage", paths=["sub"])


# --- check_repo_root() --------------------------------------------------


def test_check_repo_root_accepts_work_tree_root(tmp_git: Path) -> None:
    assert check_repo_root(tmp_git) is None


def test_check_repo_root_rejects_non_git_dir(tmp_path: Path) -> None:
    assert "not inside a git repository" in check_repo_root(tmp_path)


def test_check_repo_root_rejects_subfolder(tmp_git: Path) -> None:
    (tmp_git / "reqs").mkdir()
    assert "not the root of its git work tree" in check_repo_root(tmp_git / "reqs")


def test_check_repo_root_rejects_shallow_clone(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    (Path(repo.path) / "shallow").write_bytes(f"{repo.head.target}\n".encode())
    assert "shallow clone" in check_repo_root(tmp_git)


def test_check_repo_root_reports_unopenable_repo(tmp_git: Path) -> None:
    """A repo git finds but can't open (here an unsupported format; also a
    corrupt one, or one another user owns) is reported, not raised."""
    pygit2.Repository(str(tmp_git)).config["core.repositoryformatversion"] = 99
    assert "cannot open git repository" in check_repo_root(tmp_git)


# --- show_blob_at() -----------------------------------------------------


def test_show_blob_at_current_and_historical(tmp_git: Path) -> None:
    _write(tmp_git, "file.txt", "v1")
    c1 = commit(tmp_git, "v1")
    _write(tmp_git, "file.txt", "v2")
    c2 = commit(tmp_git, "v2")

    repo = pygit2.Repository(str(tmp_git))
    assert show_blob_at(repo, "file.txt", c1) == "v1"
    assert show_blob_at(repo, "file.txt", c2) == "v2"
    # HEAD also works.
    assert show_blob_at(repo, "file.txt", "HEAD") == "v2"


def test_show_blob_at_unknown_ref_raises(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    with pytest.raises(GitError, match="cannot resolve"):
        show_blob_at(repo, "a.txt", "deadbeef")


def test_show_blob_at_path_not_in_tree(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    with pytest.raises(GitError, match="not present"):
        show_blob_at(repo, "nope.txt", "HEAD")


# --- list_tree_filenames() ----------------------------------------------


def test_list_tree_filenames_returns_blobs_only(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/objects/A.xml", "<a/>")
    _write(tmp_git, "DOC/objects/B.xml", "<b/>")
    _write(tmp_git, "DOC/objects/sub/C.xml", "<c/>")  # nested → skipped
    _write(tmp_git, "DOC/manifest.xml", "<m/>")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))

    names = list_tree_filenames(repo, "DOC/objects", "HEAD")
    assert names == ["A.xml", "B.xml"]  # subdirectory excluded, sorted


def test_list_tree_filenames_missing_dir_returns_empty(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/manifest.xml", "<m/>")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))

    assert list_tree_filenames(repo, "GHOST/objects", "HEAD") == []


def test_list_tree_filenames_unknown_ref_raises(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/manifest.xml", "<m/>")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))

    with pytest.raises(GitError, match="cannot resolve"):
        list_tree_filenames(repo, "DOC", "deadbeef")


def test_list_tree_filenames_root(tmp_git: Path) -> None:
    _write(tmp_git, "top.txt", "1")
    _write(tmp_git, "sub/inner.txt", "2")  # subdir excluded
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))

    assert list_tree_filenames(repo, "", "HEAD") == ["top.txt"]


# --- log_for_path() -----------------------------------------------------


def test_log_for_path_orders_newest_first(tmp_git: Path) -> None:
    _write(tmp_git, "file.txt", "v1")
    c1 = commit(tmp_git, "v1")
    _write(tmp_git, "other.txt", "x")
    commit(tmp_git, "other")  # does NOT touch file.txt
    _write(tmp_git, "file.txt", "v2")
    c2 = commit(tmp_git, "v2")

    repo = pygit2.Repository(str(tmp_git))
    entries = log_for_path(repo, "file.txt")
    assert [e["short_hash"] for e in entries] == [c2, c1]
    # The unrelated commit that didn't touch file.txt is filtered out.
    assert all(e["message"] in ("v1", "v2") for e in entries)


def test_log_for_path_respects_max_entries(tmp_git: Path) -> None:
    _write(tmp_git, "file.txt", "v1")
    commit(tmp_git, "v1")
    _write(tmp_git, "file.txt", "v2")
    commit(tmp_git, "v2")
    _write(tmp_git, "file.txt", "v3")
    commit(tmp_git, "v3")

    repo = pygit2.Repository(str(tmp_git))
    entries = log_for_path(repo, "file.txt", max_entries=2)
    assert len(entries) == 2
    assert [e["message"] for e in entries] == ["v3", "v2"]


def test_log_for_path_several_paths_as_one_unit(tmp_git: Path) -> None:
    """An object's XML plus its figure: a commit touching either is listed."""
    _write(tmp_git, "DOC/objects/A.xml", "<object/>")
    commit(tmp_git, "add A")
    _write(tmp_git, "DOC/figures/A.svg", "<svg/>")
    commit(tmp_git, "add figure")
    _write(tmp_git, "other.txt", "x")
    commit(tmp_git, "unrelated")
    _write(tmp_git, "DOC/figures/A.svg", "<svg><rect/></svg>")
    commit(tmp_git, "redraw figure")
    _write(tmp_git, "DOC/objects/A.xml", "<object><x/></object>")
    commit(tmp_git, "edit A")

    repo = pygit2.Repository(str(tmp_git))
    entries = log_for_path(repo, ["DOC/objects/A.xml", "DOC/figures/A.svg"])
    assert [e["message"] for e in entries] == [
        "edit A",
        "redraw figure",
        "add figure",
        "add A",
    ]
    # A single path still sees only its own commits.
    assert [e["message"] for e in log_for_path(repo, "DOC/objects/A.xml")] == [
        "edit A",
        "add A",
    ]


# --- last_commit_for_paths ----------------------------------------------


def test_last_commit_for_paths_returns_newer_of_two(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/objects/A.xml", "<a/>")
    c1 = commit(tmp_git, "add A")
    _write(tmp_git, "DOC/figures/A.svg", "<svg/>")
    c2 = commit(tmp_git, "add A figure")

    repo = pygit2.Repository(str(tmp_git))
    # Object alone: c1. Figure alone: c2. Together: newest is c2.
    assert last_commit_for_path(repo, "DOC/objects/A.xml") == c1
    assert last_commit_for_path(repo, "DOC/figures/A.svg") == c2
    assert last_commit_for_paths(
        repo, ["DOC/objects/A.xml", "DOC/figures/A.svg"]
    ) == c2


def test_last_commit_for_paths_one_missing(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/objects/A.xml", "<a/>")
    c1 = commit(tmp_git, "add A")

    repo = pygit2.Repository(str(tmp_git))
    # Figure path never existed — falls back to the object's commit.
    assert last_commit_for_paths(
        repo, ["DOC/objects/A.xml", "DOC/figures/A.svg"]
    ) == c1


def test_last_commit_for_paths_neither_exists(tmp_git: Path) -> None:
    _write(tmp_git, "OTHER/x.txt", "x")
    commit(tmp_git, "seed")

    repo = pygit2.Repository(str(tmp_git))
    assert last_commit_for_paths(
        repo, ["DOC/objects/A.xml", "DOC/figures/A.svg"]
    ) is None


def test_last_commit_for_paths_empty_repo(tmp_git: Path) -> None:
    repo = pygit2.Repository(str(tmp_git))
    assert last_commit_for_paths(repo, ["DOC/objects/A.xml"]) is None


def test_last_commit_for_paths_prefers_more_recent_change(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/objects/A.xml", "<a/>")
    _write(tmp_git, "DOC/figures/A.svg", "<svg/>")
    commit(tmp_git, "seed both")
    _write(tmp_git, "DOC/figures/A.svg", "<svg>v2</svg>")
    c2 = commit(tmp_git, "edit figure")
    _write(tmp_git, "unrelated.txt", "x")
    commit(tmp_git, "unrelated change")

    repo = pygit2.Repository(str(tmp_git))
    assert last_commit_for_paths(
        repo, ["DOC/objects/A.xml", "DOC/figures/A.svg"]
    ) == c2


def _commit_on(
    repo: pygit2.Repository, parents: list, files: dict[str, str], message: str = "c"
) -> pygit2.Oid:
    """Commit parents[0]'s tree plus `files` ({path: text}) without moving HEAD.

    Two parents make a merge commit; `files` is then its merge result.
    """
    index = pygit2.Index()
    index.read_tree(repo[parents[0]].tree)
    for rel, text in files.items():
        index.add(pygit2.IndexEntry(rel, repo.create_blob(text.encode()), pygit2.GIT_FILEMODE_BLOB))
    sig = pygit2.Signature("t", "t@x")
    return repo.create_commit(None, sig, sig, message, index.write_tree(repo), list(parents))


def test_last_commit_follows_the_parent_a_merge_matches(tmp_git: Path) -> None:
    """Like `git log -1 -- <path>`: a merge that took one side's text is not
    the change; that side's commit is. Same answer for both log helpers."""
    _write(tmp_git, "DOC/objects/A.xml", "v1")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    base = repo.head.target
    branch = _commit_on(repo, [base], {"DOC/objects/A.xml": "v2"})
    main = _commit_on(repo, [base], {"other.txt": "x"})
    merge = _commit_on(repo, [main, branch], {"DOC/objects/A.xml": "v2"})
    repo.reset(merge, pygit2.GIT_RESET_HARD)

    assert last_commit_for_paths(repo, ["DOC/objects/A.xml"]) == str(branch)[:7]
    assert last_commit_for_path(repo, "DOC/objects/A.xml") == str(branch)[:7]
    assert log_for_path(repo, "DOC/objects/A.xml")[0]["commit_oid"] == str(branch)


# --- head_summary / dirty_count -----------------------------------------


def test_head_summary_empty_repo(tmp_git: Path) -> None:
    repo = pygit2.Repository(str(tmp_git))
    branch, short = head_summary(repo)
    assert branch is None
    assert short is None


def test_head_summary_after_first_commit(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    short_written = commit(tmp_git, "first")
    repo = pygit2.Repository(str(tmp_git))
    branch, short = head_summary(repo)
    # pygit2.init_repository initializes on 'main' or 'master' depending on
    # libgit2 defaults — both are acceptable, just check it's something.
    assert branch in ("main", "master")
    assert short == short_written


def test_head_summary_detached(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "first")
    _write(tmp_git, "a.txt", "y")
    commit(tmp_git, "second")
    repo = pygit2.Repository(str(tmp_git))
    # Checkout the first commit by hash — detaches HEAD.
    first_commit_oid = list(
        repo.walk(repo.head.target, pygit2.GIT_SORT_TOPOLOGICAL)
    )[-1].id
    repo.checkout_tree(repo[first_commit_oid].tree)
    repo.set_head(first_commit_oid)
    branch, short = head_summary(repo)
    assert branch is None
    assert short is not None


def test_dirty_count_clean_and_dirty(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    assert dirty_count(repo) == 0

    _write(tmp_git, "a.txt", "y")       # modify
    _write(tmp_git, "b.txt", "new")     # untracked
    assert dirty_count(repo) == 2


def test_dirty_count_skips_editor_side_files(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "x")
    commit(tmp_git, "seed")
    _write(tmp_git, "DOC/figures/.$DOC-1.svg.bkp", "backup")
    _write(tmp_git, "DOC/figures/.$DOC-1.svg.dtmp", "draft")
    _write(tmp_git, "DOC/~$notes.docx", "lock")
    repo = pygit2.Repository(str(tmp_git))
    assert dirty_count(repo) == 0


def test_dirty_paths_under_skips_editor_side_files(tmp_git: Path) -> None:
    """draw.io leaves `.$<name>.bkp` next to every figure it saves; a
    document commit must never sweep it in."""
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "seed")
    _write(tmp_git, "DOC/figures/DOC-1.svg", "<svg/>")
    _write(tmp_git, "DOC/figures/.$DOC-1.svg.bkp", "<svg/>")
    _write(tmp_git, "DOC/~$DOC.xml", "lock")
    repo = pygit2.Repository(str(tmp_git))
    assert dirty_paths_under(repo, "DOC") == ["DOC/figures/DOC-1.svg"]


def test_dirty_paths_under_filters_by_folder(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    _write(tmp_git, "DOC/objects/DOC-1.xml", "<object/>")
    _write(tmp_git, "OTHER/OTHER.xml", "<document/>")
    commit(tmp_git, "seed")

    # Modify one file in DOC, one in OTHER, and add a new file in DOC.
    _write(tmp_git, "DOC/DOC.xml", "<document><x/></document>")
    _write(tmp_git, "DOC/links/DOC-1.xml", "<links/>")
    _write(tmp_git, "OTHER/OTHER.xml", "<document><x/></document>")

    repo = pygit2.Repository(str(tmp_git))
    assert dirty_paths_under(repo, "DOC") == [
        "DOC/DOC.xml",
        "DOC/links/DOC-1.xml",
    ]
    assert dirty_paths_under(repo, "OTHER") == ["OTHER/OTHER.xml"]


def test_dirty_paths_under_does_not_match_prefix_collisions(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    _write(tmp_git, "DOCX/DOCX.xml", "<document/>")
    commit(tmp_git, "seed")
    _write(tmp_git, "DOC/DOC.xml", "<document><x/></document>")
    _write(tmp_git, "DOCX/DOCX.xml", "<document><y/></document>")

    repo = pygit2.Repository(str(tmp_git))
    assert dirty_paths_under(repo, "DOC") == ["DOC/DOC.xml"]


def test_dirty_paths_under_includes_deletes(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/objects/DOC-1.xml", "<object/>")
    commit(tmp_git, "seed")
    (tmp_git / "DOC" / "objects" / "DOC-1.xml").unlink()

    repo = pygit2.Repository(str(tmp_git))
    assert dirty_paths_under(repo, "DOC") == ["DOC/objects/DOC-1.xml"]


def test_dirty_paths_under_includes_staged_only_changes(tmp_git: Path) -> None:
    """Files another git client staged (work tree == index) are uncommitted
    too: commit() builds the tree from HEAD plus the listed paths, so an
    unlisted one would be left out of a document's commit."""
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    _write(tmp_git, "DOC/objects/DOC-1.xml", "<object/>")
    _write(tmp_git, "DOC/objects/DOC-2.xml", "<object/>")
    commit(tmp_git, "seed")
    _write(tmp_git, "DOC/DOC.xml", "<document><x/></document>")
    _write(tmp_git, "DOC/objects/DOC-3.xml", "<object/>")
    (tmp_git / "DOC/objects/DOC-2.xml").unlink()
    repo = pygit2.Repository(str(tmp_git))
    repo.index.add("DOC/DOC.xml")             # staged modify
    repo.index.add("DOC/objects/DOC-3.xml")   # staged new
    repo.index.remove("DOC/objects/DOC-2.xml")  # staged delete
    repo.index.write()

    assert dirty_paths_under(repo, "DOC") == [
        "DOC/DOC.xml",
        "DOC/objects/DOC-2.xml",
        "DOC/objects/DOC-3.xml",
    ]
    assert dirty_count(repo) == 3

    commit(tmp_git, "doc", paths=dirty_paths_under(repo, "DOC"))
    repo = pygit2.Repository(str(tmp_git))
    tree = repo.head.peel(pygit2.Commit).tree
    assert "DOC/objects/DOC-3.xml" in tree
    assert "DOC/objects/DOC-2.xml" not in tree
    assert repo.status() == {}


def test_head_doc_folder_names_reads_the_last_commit(tmp_git: Path) -> None:
    from mellon.git_ops import head_doc_folder_names

    repo = pygit2.Repository(str(tmp_git))
    assert head_doc_folder_names(repo) == frozenset()  # unborn HEAD
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    _write(tmp_git, "NOTDOC/other.xml", "<x/>")
    _write(tmp_git, ".hidden/.hidden.xml", "<x/>")
    _write(tmp_git, "sub/INNER/INNER.xml", "<document/>")
    commit(tmp_git, "seed")
    import shutil

    shutil.rmtree(tmp_git / "DOC")  # gone from the work tree, still in HEAD

    assert head_doc_folder_names(repo) == frozenset({"DOC"})
    assert head_doc_folder_names(repo, ".") == frozenset({"DOC"})
    assert head_doc_folder_names(repo, "sub") == frozenset({"INNER"})
    assert head_doc_folder_names(repo, "missing") == frozenset()


# --- discard_paths() ----------------------------------------------------


def test_discard_paths_restores_folder_to_head(tmp_git: Path) -> None:
    for rel in ("DOC/DOC.xml", "DOC/objects/DOC-0.xml", "DOC/objects/DOC-1.xml",
                "DOC/objects/DOC-2.xml", "DOCX/DOCX.xml", "OTHER/OTHER.xml"):
        _write(tmp_git, rel, f"<seed>{rel}</seed>\n")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))

    _write(tmp_git, "DOC/DOC.xml", "<modified/>\n")
    (tmp_git / "DOC/objects/DOC-1.xml").unlink()            # deleted
    _write(tmp_git, "DOC/objects/DOC-3.xml", "<new/>\n")    # untracked object
    _write(tmp_git, "DOC/links/DOC-0.xml", "<new/>\n")      # untracked, new dir
    _write(tmp_git, "DOC/objects/DOC-0.xml", "<staged/>\n")
    _write(tmp_git, "DOC/objects/DOC-4.xml", "<staged-new/>\n")
    repo.index.add("DOC/objects/DOC-0.xml")                 # staged modify
    repo.index.add("DOC/objects/DOC-4.xml")                 # staged new
    repo.index.remove("DOC/objects/DOC-2.xml")              # staged delete
    (tmp_git / "DOC/objects/DOC-2.xml").unlink()
    repo.index.write()
    # Changes in other folders, one sharing the DOC prefix, must survive.
    _write(tmp_git, "DOCX/DOCX.xml", "<other/>\n")
    _write(tmp_git, "OTHER/OTHER.xml", "<other/>\n")
    _write(tmp_git, "OTHER/new.xml", "<other/>\n")
    repo.index.add("OTHER/OTHER.xml")
    repo.index.write()
    others_before = {p: f for p, f in repo.status().items() if not p.startswith("DOC/")}

    changed = discard_paths(repo, ["DOC"])

    assert changed == [
        "DOC/DOC.xml",
        "DOC/links/DOC-0.xml",
        "DOC/objects/DOC-0.xml",
        "DOC/objects/DOC-1.xml",
        "DOC/objects/DOC-2.xml",
        "DOC/objects/DOC-3.xml",
        "DOC/objects/DOC-4.xml",
    ]
    repo = pygit2.Repository(str(tmp_git))
    status = repo.status()
    assert not [p for p in status if p.startswith("DOC/")]
    assert status == others_before
    for rel in ("DOC/DOC.xml", "DOC/objects/DOC-0.xml", "DOC/objects/DOC-1.xml",
                "DOC/objects/DOC-2.xml"):
        assert (tmp_git / rel).read_text(encoding="utf-8") == f"<seed>{rel}</seed>\n"
    for rel in ("DOC/objects/DOC-3.xml", "DOC/objects/DOC-4.xml", "DOC/links/DOC-0.xml"):
        assert not (tmp_git / rel).exists()
    assert (tmp_git / "OTHER/new.xml").read_text(encoding="utf-8") == "<other/>\n"
    assert (tmp_git / "DOCX/DOCX.xml").read_text(encoding="utf-8") == "<other/>\n"
    # Nothing left to discard the second time.
    assert discard_paths(repo, ["DOC"]) == []


def test_discard_paths_refuses_a_folder_never_committed(tmp_git: Path) -> None:
    repo = pygit2.Repository(str(tmp_git))
    _write(tmp_git, "NEW/NEW.xml", "<document/>\n")
    with pytest.raises(GitError, match="nothing to restore"):
        discard_paths(repo, ["NEW"])  # unborn HEAD
    _write(tmp_git, "DOC/DOC.xml", "<document/>\n")
    commit(tmp_git, "seed", paths=["DOC/DOC.xml"])
    with pytest.raises(GitError, match="nothing to restore"):
        discard_paths(repo, ["NEW"])
    assert (tmp_git / "NEW/NEW.xml").is_file()


def _seed_doc_with_pending_changes(tmp_git: Path) -> pygit2.Repository:
    """DOC committed, then DOC.xml modified + staged and a new object added."""
    _write(tmp_git, "DOC/DOC.xml", "<seed/>\n")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    _write(tmp_git, "DOC/DOC.xml", "<modified/>\n")
    repo.index.add("DOC/DOC.xml")
    repo.index.write()
    _write(tmp_git, "DOC/objects/DOC-1.xml", "<new/>\n")
    return repo


def _assert_doc_untouched(tmp_git: Path) -> None:
    assert (tmp_git / "DOC/DOC.xml").read_text(encoding="utf-8") == "<modified/>\n"
    assert (tmp_git / "DOC/objects/DOC-1.xml").is_file()
    status = pygit2.Repository(str(tmp_git)).status()
    assert status["DOC/DOC.xml"] == pygit2.GIT_STATUS_INDEX_MODIFIED


def test_discard_paths_refuses_while_the_index_is_locked(tmp_git: Path) -> None:
    # A git GUI refreshing its status holds index.lock for a moment; the
    # discard must not restore half the folder and then fail on the index.
    repo = _seed_doc_with_pending_changes(tmp_git)
    (Path(repo.path) / "index.lock").write_bytes(b"")

    with pytest.raises(GitError, match="index is locked"):
        discard_paths(repo, ["DOC"])
    _assert_doc_untouched(tmp_git)


def test_discard_paths_refuses_a_nested_repo_before_touching_files(
    tmp_git: Path,
) -> None:
    repo = _seed_doc_with_pending_changes(tmp_git)
    pygit2.init_repository(str(tmp_git / "DOC" / "sub"), bare=False)
    _write(tmp_git, "DOC/sub/x.txt", "nested")
    assert "DOC/sub/" in repo.status()

    with pytest.raises(GitError, match="nested git repository"):
        discard_paths(repo, ["DOC"])
    _assert_doc_untouched(tmp_git)
    assert (tmp_git / "DOC/sub/x.txt").is_file()


def test_discard_paths_refuses_a_symlink_in_head(tmp_git: Path) -> None:
    # Built in the index, so no symlink privilege is needed on Windows.
    repo = pygit2.Repository(str(tmp_git))
    _write(tmp_git, "DOC/DOC.xml", "<seed/>\n")
    index = repo.index
    index.add("DOC/DOC.xml")
    link_blob = repo.create_blob(b"../outside.xml")
    index.add(pygit2.IndexEntry("DOC/link.xml", link_blob, pygit2.GIT_FILEMODE_LINK))
    index.write()
    sig = pygit2.Signature("t", "t@example.com")
    repo.create_commit("HEAD", sig, sig, "seed", index.write_tree(), [])
    _write(tmp_git, "DOC/DOC.xml", "<modified/>\n")  # DOC/link.xml is missing

    with pytest.raises(GitError, match="symlink or submodule"):
        discard_paths(repo, ["DOC"])
    assert (tmp_git / "DOC/DOC.xml").read_text(encoding="utf-8") == "<modified/>\n"
    assert not (tmp_git / "DOC/link.xml").exists()


def test_discard_paths_replaces_a_linked_file_instead_of_writing_through(
    tmp_git: Path,
) -> None:
    # A hard link stands in for a symlink (no privilege needed): writing
    # HEAD's bytes through it would change the file in the other folder.
    _write(tmp_git, "DOC/DOC.xml", "<seed/>\n")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    _write(tmp_git, "OTHER/notes.xml", "<other/>\n")
    (tmp_git / "DOC/DOC.xml").unlink()
    os.link(tmp_git / "OTHER/notes.xml", tmp_git / "DOC/DOC.xml")

    assert discard_paths(repo, ["DOC"]) == ["DOC/DOC.xml"]
    assert (tmp_git / "DOC/DOC.xml").read_text(encoding="utf-8") == "<seed/>\n"
    assert (tmp_git / "OTHER/notes.xml").read_text(encoding="utf-8") == "<other/>\n"
    assert not list((tmp_git / "DOC").glob(".*.tmp"))


def test_discard_paths_refuses_during_merge(tmp_git: Path) -> None:
    _write(tmp_git, "a.txt", "base")
    commit(tmp_git, "base")
    repo = pygit2.Repository(str(tmp_git))
    base = repo.head.target
    _write(tmp_git, "a.txt", "theirs")
    theirs = repo.revparse_single(commit(tmp_git, "theirs")).id
    repo.reset(base, pygit2.GIT_RESET_HARD)
    _write(tmp_git, "a.txt", "ours")
    commit(tmp_git, "ours")
    repo = pygit2.Repository(str(tmp_git))
    repo.merge(theirs)  # conflicts on a.txt
    _write(tmp_git, "a.txt", "resolved")

    with pytest.raises(GitError, match="finish the merge in your git client"):
        discard_paths(repo, ["a.txt"])
    assert (tmp_git / "a.txt").read_text(encoding="utf-8") == "resolved"


def test_log_for_path_since_commit_truncates(tmp_git: Path) -> None:
    _write(tmp_git, "file.txt", "v1")
    c1 = commit(tmp_git, "v1")
    _write(tmp_git, "file.txt", "v2")
    commit(tmp_git, "v2")
    _write(tmp_git, "file.txt", "v3")
    commit(tmp_git, "v3")

    repo = pygit2.Repository(str(tmp_git))
    entries = log_for_path(repo, "file.txt", since_commit=c1)
    # Walk stops at (exclusive) c1, so we see v3 and v2 only.
    assert [e["message"] for e in entries] == ["v3", "v2"]


# --- issue helpers + tags -----------------------------------------------


@pytest.mark.parametrize(
    "current,expected",
    [
        ("", "1"),
        ("1", "2"),
        ("9", "10"),
        ("12.23.54", "12.23.55"),
        ("0.0.9", "0.0.10"),
        ("draft", ""),
        ("1.2.x", ""),
        ("1.", ""),
        (".5", ""),
    ],
)
def test_increment_issue(current: str, expected: str) -> None:
    assert increment_issue(current) == expected


@pytest.mark.parametrize("good", ["1", "1.2", "12.23.54", "0", "0.0.0"])
def test_validate_issue_accepts(good: str) -> None:
    validate_issue(good)


@pytest.mark.parametrize(
    "bad", ["", "draft", "1.2.x", "1.", ".5", "1..2", "v1", "1 2"]
)
def test_validate_issue_rejects(bad: str) -> None:
    with pytest.raises(GitError, match="dot-separated numeric"):
        validate_issue(bad)


def test_current_issue_empty_when_no_tags(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "seed")
    repo = pygit2.Repository(str(tmp_git))
    assert current_issue(repo, "DOC") == ""


def test_current_issue_returns_highest_reachable(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    create_issue_tag(repo, "DOC", "1", head)
    create_issue_tag(repo, "DOC", "2", head)
    create_issue_tag(repo, "DOC", "12.23.54", head)
    # 12.23.54 should outrank 2 by version-sort.
    assert current_issue(repo, "DOC") == "12.23.54"


def test_current_issue_ignores_other_docs(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    create_issue_tag(repo, "DOC", "5", head)
    create_issue_tag(repo, "OTHER", "9", head)
    assert current_issue(repo, "DOC") == "5"
    assert current_issue(repo, "OTHER") == "9"


def test_current_issue_excludes_unreachable_branch_tag(tmp_git: Path) -> None:
    """A tag on an unrelated branch shouldn't pollute HEAD's current issue."""
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "main commit")
    repo = pygit2.Repository(str(tmp_git))
    main_oid = repo.head.target

    # Create a sibling branch from a *new* root commit (no shared ancestry).
    # The simplest way: tag a fresh detached commit by re-creating from a
    # different blob OID. Easier: create the tag at an unrelated commit OID
    # built from a second working tree state.
    _write(tmp_git, "DOC/DOC.xml", "<document>changed</document>")
    sibling_oid_short = commit(tmp_git, "sibling")
    sibling_oid = repo.revparse_single(sibling_oid_short).id
    create_issue_tag(repo, "DOC", "99", sibling_oid)

    # Reset HEAD back to the original commit so the sibling tag isn't reachable.
    repo.reset(main_oid, pygit2.GIT_RESET_HARD)
    create_issue_tag(repo, "DOC", "1", main_oid)

    assert current_issue(repo, "DOC") == "1"


def test_create_issue_tag_refuses_duplicate(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    create_issue_tag(repo, "DOC", "1", head)
    with pytest.raises(GitError, match="already exists"):
        create_issue_tag(repo, "DOC", "1", head)


def test_create_issue_tag_refuses_an_issue_that_goes_backwards(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    create_issue_tag(repo, "DOC", "3", repo.head.target)
    _write(tmp_git, "DOC/DOC.xml", "<document>v2</document>")
    commit(tmp_git, "v2")
    for lower in ("2", "03", "2.9"):
        with pytest.raises(GitError, match="must be higher than the current issue"):
            create_issue_tag(repo, "DOC", lower, repo.head.target)
    assert create_issue_tag(repo, "DOC", "3.1", repo.head.target) == "DOC-issue-3.1"


def test_create_issue_tag_validates_format(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    with pytest.raises(GitError, match="dot-separated numeric"):
        create_issue_tag(repo, "DOC", "draft", repo.head.target)


def test_issue_tags_by_commit_returns_oid_to_issue_map(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    oid_a = repo.head.target
    create_issue_tag(repo, "DOC", "1", oid_a)

    _write(tmp_git, "DOC/DOC.xml", "<document>v2</document>")
    commit(tmp_git, "v2")
    oid_b = repo.head.target
    create_issue_tag(repo, "DOC", "1.1", oid_b)

    by_commit = issue_tags_by_commit(repo, "DOC")
    assert by_commit == {str(oid_a): "1", str(oid_b): "1.1"}


def test_issue_tags_by_commit_picks_highest_when_collision(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    create_issue_tag(repo, "DOC", "1", head)
    create_issue_tag(repo, "DOC", "1.1", head)

    by_commit = issue_tags_by_commit(repo, "DOC")
    assert by_commit == {str(head): "1.1"}


def test_issue_tags_by_commit_ignores_unrelated_tags(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    create_issue_tag(repo, "DOC", "1", head)
    create_issue_tag(repo, "OTHER", "9", head)
    repo.references.create("refs/tags/random", head)

    by_commit = issue_tags_by_commit(repo, "DOC")
    assert by_commit == {str(head): "1"}


# --- free-named tags -------------------------------------------------------


@pytest.mark.parametrize("good", ["CDR-1.0", "PDR", "release/2026-10", "v1.0-rc1"])
def test_tag_name_error_accepts(good: str) -> None:
    assert tag_name_error(good, ["OTHER"]) == ""


@pytest.mark.parametrize(
    "bad,reason",
    [
        ("", "must not be empty"),
        ("CDR 1.0", "not a valid tag name"),
        ("a..b", "not a valid tag name"),
        ("x~1", "not a valid tag name"),
        ("x.lock", "not a valid tag name"),
        ("-x", "not a valid tag name"),
        ("TAKEN", "already exists"),
        ("VCU3-SRS-issue-3", "form of an issue tag"),
        ("X-issue-1.2", "form of an issue tag"),
    ],
)
def test_tag_name_error_refuses(bad: str, reason: str) -> None:
    assert reason in tag_name_error(bad, ["TAKEN"])


def test_tag_name_error_allows_issue_words_that_are_not_the_issue_form() -> None:
    assert tag_name_error("issue-review", []) == ""
    assert tag_name_error("X-issue-draft", []) == ""


def test_create_tag_makes_an_annotated_tag(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target

    assert create_tag(repo, "CDR-1.0", head, "Critical design review") == "CDR-1.0"

    tag = repo.revparse_single("refs/tags/CDR-1.0")
    assert isinstance(tag, pygit2.Tag)
    assert tag.target == head
    assert tag.message.strip() == "Critical design review"
    assert tag.tagger.name == repo.config["user.name"]
    assert tag.tagger.email == repo.config["user.email"]
    assert tag_names(repo) == ("CDR-1.0",)


def test_create_tag_message_defaults_to_the_name(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    create_tag(repo, "PDR", repo.head.target)
    assert repo.revparse_single("refs/tags/PDR").message.strip() == "PDR"


def test_create_tag_refuses_what_tag_name_error_refuses(tmp_git: Path) -> None:
    _write(tmp_git, "DOC/DOC.xml", "<document/>")
    commit(tmp_git, "v1")
    repo = pygit2.Repository(str(tmp_git))
    head = repo.head.target
    create_issue_tag(repo, "DOC", "1", head)
    create_tag(repo, "CDR-1.0", head)
    for name, reason in (
        ("CDR-1.0", "already exists"),
        ("DOC-issue-1", "already exists"),
        ("DOC-issue-2", "form of an issue tag"),
        ("bad name", "not a valid tag name"),
    ):
        with pytest.raises(GitError, match=reason):
            create_tag(repo, name, head)
    assert tag_names(repo) == ("CDR-1.0", "DOC-issue-1")


def test_log_for_path_includes_commit_oid(tmp_git: Path) -> None:
    _write(tmp_git, "file.txt", "v1")
    commit(tmp_git, "v1")
    _write(tmp_git, "file.txt", "v2")
    commit(tmp_git, "v2")

    repo = pygit2.Repository(str(tmp_git))
    entries = log_for_path(repo, "file.txt")
    for entry in entries:
        assert "commit_oid" in entry
        assert len(entry["commit_oid"]) == 40
        assert entry["commit_oid"].startswith(entry["short_hash"])
