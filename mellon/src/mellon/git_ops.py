"""Thin pygit2 wrapper for the operations mellon needs.

Scope:
    - Discover the repo that contains a document folder.
    - For a given repo-relative path, find the short hash of the most recent
      commit that changed it (equivalent of `git log -1 --format=%h -- <path>`).
    - Stage + commit changes (used by the MCP `commit_changes` tool and the
      GUI's save/commit button).
    - Restore a document folder to HEAD (the GUI's discard action).
    - Fetch file content at a historical commit and list commits that changed
      a given path (used by the MCP `review_suspect_link` tool).
    - Manage per-document issue tags (`<doc-id>-issue-<n>`) — the source of
      truth for a doc's release version.
    - Create the free-named tags of the commit dialog's Tag field (annotated,
      repo-wide).
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from pathlib import Path
from typing import Iterable

import pygit2


SHORT_HASH_LEN = 7
_ISSUE_RE = re.compile(r"^\d+(\.\d+)*$")
_ISSUE_TAG_INFIX = "-issue-"

# libgit2 only caches trees up to 4 KB by default. A document's objects/ tree
# soon outgrows that, and suspect scans re-inflated it at every history step
# (measured 33x slower on a large repo).
pygit2.settings.cache_object_limit(pygit2.GIT_OBJECT_TREE, 1 << 20)


class GitError(Exception):
    """Raised for git-related failures (no repo, no commits, etc.)."""


def discover_repo(path: str | Path) -> tuple[pygit2.Repository, Path]:
    """Return (repo, repo_root) for the repository containing `path`.

    repo_root is the working-tree root (parent of `.git`).
    """
    found = pygit2.discover_repository(str(path))
    if found is None:
        raise GitError(f"No git repository found at or above {path}")
    repo = pygit2.Repository(found)
    # `found` is the path to the `.git` directory (or file for worktrees);
    # the working tree root is one level up.
    root = Path(found).resolve().parent
    # When opened from inside `.git/`, workdir is more reliable:
    if repo.workdir:
        root = Path(repo.workdir).resolve()
    return repo, root


def last_commit_for_path(repo: pygit2.Repository, rel_path: str) -> str | None:
    """Short hash of the last commit on HEAD's history that changed `rel_path`.

    Returns None if the path doesn't exist at HEAD or the repo has no commits.
    Same rule as `last_commit_for_paths`.
    """
    try:
        head = repo.head.peel(pygit2.Commit)
    except (pygit2.GitError, KeyError):
        return None
    if _entry_id(head.tree, rel_path) is None:
        return None
    return last_commit_for_paths(repo, [rel_path])


def last_commit_for_paths(
    repo: pygit2.Repository, paths: Iterable[str]
) -> str | None:
    """Short hash of the most recent commit on HEAD that changed any of `paths`.

    Matches `git log -1 --format=%h -- <paths>`: starting at HEAD, a commit
    whose `paths` are identical in some parent (TREESAME) didn't change them,
    so the walk moves to the first such parent. The first commit that differs
    from every parent is the answer, so a merge counts as a change only when
    its result is new text. A root commit counts when any path exists in it.
    Used by suspect-link detection (and `add_link`'s ack) to treat an
    object's XML and its figure SVG as one logical unit: editing either
    bumps the target's hash.

    Returns None if none of the paths exist anywhere in HEAD's history (or
    the repo has no commits).
    """
    paths_list = list(paths)
    if not paths_list:
        return None
    try:
        commit = repo.head.peel(pygit2.Commit)
    except (pygit2.GitError, KeyError):
        return None

    ids = [_entry_id(commit.tree, rel) for rel in paths_list]
    while True:
        if not commit.parents:
            present = any(i is not None for i in ids)
            return _short(commit.id) if present else None
        for parent in commit.parents:
            if ids == [_entry_id(parent.tree, rel) for rel in paths_list]:
                commit = parent
                break
        else:
            return _short(commit.id)


def _entry_id(tree: pygit2.Tree, rel: str) -> pygit2.Oid | None:
    try:
        return tree[rel].id
    except KeyError:
        return None


def _short(oid: pygit2.Oid) -> str:
    return str(oid)[:SHORT_HASH_LEN]


def git_user_name(repo: pygit2.Repository) -> str | None:
    """`user.name` from the repo's git config, or None if not set."""
    try:
        return repo.config["user.name"]
    except KeyError:
        return None


def _resolve_signature(repo: pygit2.Repository) -> pygit2.Signature:
    """Signature from `user.name` / `user.email` in the git config.

    Raises GitError when either is unset: like the git CLI, mellon won't
    commit under a made-up identity (it would end up in the audit trail).
    """
    name = git_user_name(repo)
    try:
        email = repo.config["user.email"]
    except KeyError:
        email = None
    if not name or not email:
        raise GitError(
            "git user.name and user.email are not configured; set them with "
            "`git config --global user.name \"Your Name\"` and "
            "`git config --global user.email you@example.com`"
        )
    return pygit2.Signature(name, email)


def _refuse_during_merge(repo: pygit2.Repository, then: str) -> None:
    """Raise GitError while a merge (or rebase, cherry-pick, ...) is unfinished."""
    if (
        repo.state() != pygit2.GIT_REPOSITORY_STATE_NONE
        or repo.index.conflicts is not None
    ):
        raise GitError(
            "a merge (or another git operation) is in progress; "
            f"finish the merge in your git client, then {then}"
        )


def _refuse_while_index_locked(repo: pygit2.Repository, then: str) -> None:
    """Raise GitError while another git program holds the index lock."""
    if (Path(repo.path) / "index.lock").exists():
        raise GitError(
            "the git index is locked by another git program (index.lock); "
            f"wait for it to finish or close it, then {then}"
        )


def commit(
    repo_root: str | Path,
    message: str,
    paths: Iterable[str] | None = None,
    *,
    signature: pygit2.Signature | None = None,
) -> str:
    """Commit working-tree changes on HEAD. Returns the short hash.

    The commit's tree is HEAD's tree plus only the selected paths, never the
    whole on-disk index, so changes someone else staged stay out of it. The
    real index is updated for just those paths (before the commit), so
    `git status` is clean for them.

    Args:
        repo_root: any path inside the target repo (resolved via discover_repo).
        message: full commit message.
        paths: optional list of repo-relative paths to commit. If None, every
            uncommitted change (`dirty_paths_under`) is committed. Each path
            is committed with its work-tree content, or as a delete when it
            is gone. Paths use forward slashes regardless of OS.
        signature: author + committer. If None, built from the repo's
            `user.name` / `user.email`.

    Raises GitError if there's nothing to commit (no-op commits are
    rejected), a merge or other git operation is in progress, another git
    program holds the index lock or commits meanwhile, or no git identity
    is configured. A GitError always means nothing was committed.
    """
    repo, root = discover_repo(repo_root)
    try:
        _refuse_during_merge(repo, "commit again")
        _refuse_while_index_locked(repo, "commit again")
        sig = signature if signature is not None else _resolve_signature(repo)

        if paths is None:
            paths = dirty_paths_under(repo, "")

        # HEAD is read once: the tree is built on it and it is the parent, so
        # a commit landing meanwhile (the MCP server and the GUI on one repo)
        # makes create_commit refuse instead of this commit reverting it.
        head = None if repo.head_is_unborn else repo.head.peel(pygit2.Commit)
        index = repo.index
        tree_index = pygit2.Index()
        if head is not None:
            tree_index.read_tree(head.tree)
        for p in paths:
            p_posix = str(p).replace("\\", "/")
            if (root / p_posix).exists():
                index.add(p_posix)
                tree_index.add(index[p_posix])
            else:
                # Treated as a delete; ignore if it wasn't tracked.
                for idx in (index, tree_index):
                    try:
                        idx.remove(p_posix)
                    except (KeyError, OSError, pygit2.GitError):
                        pass
        tree_oid = tree_index.write_tree(repo)
        if head is not None and tree_oid == head.tree.id:
            raise GitError("nothing to commit (no changes staged)")
        parents = [head.id] if head is not None else []

        # The index goes first: when another git program holds its lock this
        # fails before anything is committed. If the commit then fails, the
        # paths are only left staged (with their work-tree content).
        index.write()
        try:
            oid = repo.create_commit("HEAD", sig, sig, message, tree_oid, parents)
        except (pygit2.GitError, OSError) as e:
            # The tip moved since HEAD was read ("current tip is not the first
            # parent"), or another program holds the branch's lock (OSError).
            raise GitError(
                f"{e}. Another git program changed the branch while "
                "committing, so nothing was committed: commit again"
            ) from e
    except pygit2.GitError as e:
        raise GitError(str(e)) from e
    return _short(oid)


_REGULAR_FILEMODES = (pygit2.GIT_FILEMODE_BLOB, pygit2.GIT_FILEMODE_BLOB_EXECUTABLE)


def discard_paths(repo: pygit2.Repository, paths: Iterable[str]) -> list[str]:
    """Put everything under `paths` back to HEAD, in the work tree and index.

    Like `git restore --source=HEAD --staged --worktree -- <paths>` followed
    by `git clean -f -- <paths>`: modified and deleted files get HEAD's
    content back, staged changes are unstaged, and files HEAD doesn't have
    (untracked or staged-new) are removed. Ignored files and everything
    outside `paths` are left alone. `paths` are repo-relative files or
    folders (forward slashes); each must exist in HEAD. Restored files get
    the committed bytes as they are (no CRLF conversion), like mellon's own
    LF writes, through a temp file, so a symlink in the way is replaced, not
    written through.

    Everything is checked before any file is touched, and the index is
    written before the work tree: if a file write fails after that, running
    the discard again finishes the job.

    Returns the repo-relative paths that changed, sorted. Raises GitError
    when a merge is in progress, another git program holds the index lock,
    a path isn't in HEAD (never committed, so there's nothing to restore it
    to), or something under `paths` isn't a plain file (a nested git
    repository, a folder where HEAD has a file, a symlink or submodule in
    HEAD): those are left for the user to sort out with git.
    """
    rels = [str(p).replace("\\", "/").strip("/") for p in paths]
    try:
        _refuse_during_merge(repo, "discard again")
        _refuse_while_index_locked(repo, "discard again")
        head_tree = (
            None if repo.head_is_unborn else repo.head.peel(pygit2.Commit).tree
        )
        for rel in rels:
            if head_tree is None or not rel or _entry_id(head_tree, rel) is None:
                raise GitError(
                    f"{rel!r} is not in the last commit, so there is nothing "
                    "to restore it to"
                )
        root = Path(repo.workdir)
        prefixes = tuple(f"{rel}/" for rel in rels)
        changed = sorted(
            p
            for p in (k.replace("\\", "/") for k in repo.status())
            if p in rels or p.startswith(prefixes)
        )
        # (rel, HEAD entry to restore, or None to delete)
        plan: list[tuple[str, pygit2.Object | None]] = []
        for rel in changed:
            target = root / rel
            if rel.endswith("/") or (target.is_dir() and not target.is_symlink()):
                raise GitError(
                    f"{rel.rstrip('/')!r} is a folder git does not track as "
                    "files here (for example a nested git repository); move "
                    "or delete it yourself, then discard again"
                )
            try:
                entry = head_tree[rel]
            except KeyError:
                entry = None
            if entry is not None and entry.filemode == pygit2.GIT_FILEMODE_TREE:
                entry = None  # a file where HEAD has a folder: delete it
            if entry is not None and entry.filemode not in _REGULAR_FILEMODES:
                raise GitError(
                    f"{rel!r} is a symlink or submodule in the last commit; "
                    "restore it with git"
                )
            plan.append((rel, entry))

        index = repo.index
        for rel, entry in plan:
            if entry is None:
                if rel in index:
                    index.remove(rel)
            else:
                index.add(pygit2.IndexEntry(rel, entry.id, entry.filemode))
        index.write()
        for rel, entry in plan:
            target = root / rel
            if entry is None:
                # New since HEAD (unlink removes a symlink, not its target).
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace_file(
                target,
                repo[entry.id].data,
                executable=entry.filemode == pygit2.GIT_FILEMODE_BLOB_EXECUTABLE,
            )
    except pygit2.GitError as e:
        raise GitError(str(e)) from e
    return changed


_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_DELAY = 0.05  # seconds


def _replace_file(path: Path, data: bytes, *, executable: bool = False) -> None:
    """Write `data` to a temp sibling and `os.replace` it over `path`.

    Same pattern as `editor._write_tree` (which imports this module, so it
    can't be shared from there): no truncated file on failure, and a
    symlink at `path` is replaced rather than followed.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        if executable:
            os.chmod(tmp, os.stat(tmp).st_mode | 0o111)
        # Windows refuses to replace a file another process holds open
        # without FILE_SHARE_DELETE; those holds are brief, so retry.
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def check_repo_root(root: str | Path) -> str | None:
    """Why `root` can't be used as a mellon repo root, or None when it can.

    Git paths are built as `<doc-id>/...` from the git work-tree root, so the
    documents must sit directly in it; and suspect detection needs full
    history (in a shallow clone every link would read SUSPECT).
    """
    try:
        repo, git_root = discover_repo(root)
    except GitError:
        return f"{root} is not inside a git repository (run `git init` there)"
    except pygit2.GitError as e:
        # Found but unopenable: corrupt, unsupported format, or not owned
        # by the current user.
        return f"{root}: cannot open git repository: {e}"
    if Path(root).resolve() != git_root:
        return (
            f"{root} is not the root of its git work tree ({git_root}); "
            f"mellon documents must sit directly under the git root"
        )
    if repo.is_shallow:
        return (
            f"{git_root} is a shallow clone; suspect-link detection needs "
            f"full history (run `git fetch --unshallow`)"
        )
    return None


def show_blob_at(
    repo: pygit2.Repository, rel_path: str, commit_ref: str
) -> str:
    """Return the UTF-8 content of `rel_path` as it was at `commit_ref`.

    commit_ref may be a short or full hash, a branch name, or any other ref
    that `git rev-parse` would accept.

    Raises GitError if the ref can't be resolved or the path isn't present
    in that commit's tree.
    """
    try:
        obj = repo.revparse_single(commit_ref)
    except (pygit2.GitError, KeyError) as e:
        raise GitError(f"cannot resolve {commit_ref!r}: {e}") from e
    commit_obj = obj.peel(pygit2.Commit)
    try:
        entry = commit_obj.tree[rel_path]
    except KeyError as e:
        raise GitError(
            f"{rel_path!r} not present at {commit_ref}"
        ) from e
    blob = repo[entry.id]
    return blob.data.decode("utf-8")


def list_tree_filenames(
    repo: pygit2.Repository, rel_dir: str, commit_ref: str
) -> list[str]:
    """Names of blobs directly under `rel_dir` at `commit_ref`, sorted.

    Non-recursive: subdirectories are skipped, only file blobs at the immediate
    level of `rel_dir` are returned. Empty list when `rel_dir` doesn't exist
    at that ref. Pass `rel_dir=""` to list the top of the tree.

    Raises GitError if the ref itself can't be resolved.
    """
    try:
        obj = repo.revparse_single(commit_ref)
    except (pygit2.GitError, KeyError) as e:
        raise GitError(f"cannot resolve {commit_ref!r}: {e}") from e
    commit_obj = obj.peel(pygit2.Commit)
    rel_dir = rel_dir.replace("\\", "/").strip("/")
    if rel_dir == "":
        tree = commit_obj.tree
    else:
        try:
            entry = commit_obj.tree[rel_dir]
        except KeyError:
            return []
        sub = repo[entry.id]
        if not isinstance(sub, pygit2.Tree):
            return []
        tree = sub
    return sorted(
        e.name for e in tree if e.filemode != pygit2.GIT_FILEMODE_TREE
    )


def head_doc_folder_names(
    repo: pygit2.Repository, rel_dir: str = ""
) -> frozenset[str]:
    """Names of the document folders directly under `rel_dir` in HEAD.

    The same rule as `repo.doc_folder_names` on the work tree: a folder
    qualifies when HEAD has `<folder>/<folder_name>.xml`; dot-folders are
    skipped. Empty for an unborn HEAD or a `rel_dir` HEAD doesn't have.
    """
    if repo.head_is_unborn:
        return frozenset()
    tree = repo.head.peel(pygit2.Commit).tree
    rel_dir = rel_dir.replace("\\", "/").strip("/")
    if rel_dir not in ("", "."):
        try:
            tree = repo[tree[rel_dir].id]
        except KeyError:
            return frozenset()
        if not isinstance(tree, pygit2.Tree):
            return frozenset()
    out: set[str] = set()
    for entry in tree:
        if entry.filemode != pygit2.GIT_FILEMODE_TREE or entry.name.startswith("."):
            continue
        sub = repo[entry.id]
        try:
            manifest = sub[f"{entry.name}.xml"]
        except KeyError:
            continue
        if manifest.filemode in _REGULAR_FILEMODES:
            out.add(entry.name)
    return frozenset(out)


def head_summary(repo: pygit2.Repository) -> tuple[str | None, str | None]:
    """Return `(branch_name, short_hash)` for the repository's current HEAD.

    `branch_name` is None when HEAD is detached; `short_hash` is None when the
    repo has no commits yet. Both are strings otherwise.
    """
    try:
        head_ref = repo.head
    except (pygit2.GitError, KeyError):
        return None, None

    try:
        short = _short(head_ref.target)
    except (pygit2.GitError, KeyError):
        short = None

    branch: str | None = None
    if not repo.head_is_detached:
        name = head_ref.shorthand
        if name:
            branch = name
    return branch, short


# A path differs from HEAD in the work tree or in the index (staged, e.g. by
# another git client): either way its change is not committed yet.
_UNCOMMITTED_MASK = (
    pygit2.GIT_STATUS_WT_NEW
    | pygit2.GIT_STATUS_WT_MODIFIED
    | pygit2.GIT_STATUS_WT_DELETED
    | pygit2.GIT_STATUS_WT_TYPECHANGE
    | pygit2.GIT_STATUS_WT_RENAMED
    | pygit2.GIT_STATUS_INDEX_NEW
    | pygit2.GIT_STATUS_INDEX_MODIFIED
    | pygit2.GIT_STATUS_INDEX_DELETED
    | pygit2.GIT_STATUS_INDEX_TYPECHANGE
    | pygit2.GIT_STATUS_INDEX_RENAMED
)


# Editor side files written next to the file being edited: draw.io keeps a
# `.$<name>.bkp` backup (and `.$<name>.dtmp` drafts) beside every figure it
# saves, Office a `~$<name>` lock file. They are never Mellon content.
_SIDE_FILE_PREFIXES = (".$", "~$")


def is_side_file(path: str) -> bool:
    """True for an editor side file (`.$*` / `~$*`), judged by its name."""
    return path.replace("\\", "/").rsplit("/", 1)[-1].startswith(_SIDE_FILE_PREFIXES)


def dirty_count(repo: pygit2.Repository) -> int:
    """Number of paths with uncommitted changes (new, modified, or deleted).

    Matches what `git status --short` would list: work-tree changes and
    staged ones (ignored files are not counted). Editor side files
    (`.$*`, `~$*`) are not counted either.
    """
    return sum(
        1
        for path, flags in repo.status().items()
        if flags & _UNCOMMITTED_MASK and not is_side_file(path)
    )


def dirty_paths_under(
    repo: pygit2.Repository, folder_rel: str
) -> list[str]:
    """Uncommitted paths under `folder_rel`, posix-style, sorted.

    Filters `repo.status()` to entries whose path starts with `folder_rel/`
    (or equals it). Returns repo-relative posix paths suitable for passing
    straight to `commit(paths=...)`. Honors the same mask as `dirty_count`:
    new, modified, deleted, typechange, renamed, in the work tree or staged.
    A staged file must be listed: `commit` builds the tree from HEAD plus
    the listed paths, so a file left out would be left out of the commit.
    Editor side files (`.$*`, `~$*`, e.g. draw.io's `.$<fig>.svg.bkp`) are
    skipped so a commit never sweeps them in.
    """
    folder_rel = folder_rel.replace("\\", "/").rstrip("/")
    prefix = folder_rel + "/" if folder_rel else ""
    out: list[str] = []
    for path, flags in repo.status().items():
        if not (flags & _UNCOMMITTED_MASK):
            continue
        norm = path.replace("\\", "/")
        if prefix and not norm.startswith(prefix):
            continue
        if is_side_file(norm):
            continue
        out.append(norm)
    out.sort()
    return out


def log_for_path(
    repo: pygit2.Repository,
    rel_path: str | Iterable[str],
    max_entries: int = 10,
    since_commit: str | None = None,
) -> list[dict]:
    """Commits on HEAD's history that changed `rel_path`, newest first.

    Matches `git log -- <rel_path>` for linear histories: emits a commit iff
    the blob at `rel_path` differs from every parent's blob at the same path
    (so unrelated commits, and merges that only bring in one side's change,
    are filtered out; the commit that made the change is listed instead).

    `rel_path` may be several paths taken as one unit — an object's XML plus
    its figure, as `last_commit_for_paths` does: a commit is listed when the
    set of blobs differs from every parent's, like `git log -- <p1> <p2>`.

    Args:
        max_entries: stop after this many matches.
        since_commit: if given, stop walking once we reach this commit
            (exclusive). Useful for "what changed on this file since ack X?"

    Returns a list of `{commit_oid, short_hash, message, author, email,
    timestamp}` dicts. `commit_oid` is the full hex; `timestamp` is the
    author's unix timestamp (seconds since epoch).
    """
    stop_oid = None
    if since_commit is not None:
        try:
            stop_oid = repo.revparse_single(since_commit).peel(pygit2.Commit).id
        except (pygit2.GitError, KeyError) as e:
            raise GitError(f"cannot resolve since_commit={since_commit!r}: {e}") from e

    try:
        head_oid = repo.head.target
    except (pygit2.GitError, KeyError):
        return []

    paths = [rel_path] if isinstance(rel_path, str) else list(rel_path)
    results: list[dict] = []
    for commit_obj in repo.walk(head_oid, pygit2.GIT_SORT_TOPOLOGICAL):
        if stop_oid is not None and commit_obj.id == stop_oid:
            break
        cur_blobs = tuple(_entry_id(commit_obj.tree, p) for p in paths)
        parent_blobs = [
            tuple(_entry_id(parent.tree, p) for p in paths)
            for parent in commit_obj.parents
        ] or [(None,) * len(paths)]

        if cur_blobs not in parent_blobs:
            results.append(
                {
                    "commit_oid": str(commit_obj.id),
                    "short_hash": _short(commit_obj.id),
                    "message": (commit_obj.message or "").splitlines()[0],
                    "author": commit_obj.author.name,
                    "email": commit_obj.author.email,
                    "timestamp": commit_obj.author.time,
                }
            )
            if len(results) >= max_entries:
                break

    return results


# --- issue tags -----------------------------------------------------------


def validate_issue(value: str) -> None:
    """Raise `GitError` if `value` isn't a non-empty dot-separated numeric
    sequence. Accepts '1', '1.2', '12.23.54'; rejects '', 'draft', '1.2.x'."""
    if not _ISSUE_RE.match(value):
        raise GitError(
            f"issue {value!r} must be a dot-separated numeric sequence "
            f"(e.g. '1', '1.2', '12.23.54')"
        )


def increment_issue(current: str) -> str:
    """'1' -> '2'; '12.23.54' -> '12.23.55'; '' -> '1'.

    Returns '' if `current` is non-empty and doesn't match the numeric-dot
    pattern — callers can detect that and prompt for a manual value.
    """
    if not current:
        return "1"
    if not _ISSUE_RE.match(current):
        return ""
    head, _, tail = current.rpartition(".")
    bumped = str(int(tail) + 1)
    return f"{head}.{bumped}" if head else bumped


def _issue_key(issue: str) -> tuple[int, ...]:
    return tuple(int(p) for p in issue.split("."))


def _issue_tag_ref(doc_id: str, issue: str) -> str:
    return f"refs/tags/{doc_id}{_ISSUE_TAG_INFIX}{issue}"


def _is_ancestor(
    repo: pygit2.Repository,
    ancestor_oid: pygit2.Oid,
    descendant_oid: pygit2.Oid,
) -> bool:
    """True if `ancestor_oid` is reachable from `descendant_oid`."""
    if ancestor_oid == descendant_oid:
        return True
    try:
        base = repo.merge_base(ancestor_oid, descendant_oid)
    except (pygit2.GitError, KeyError):
        return False
    return base == ancestor_oid


def current_issue(repo: pygit2.Repository, doc_id: str) -> str:
    """Highest issue tag for `doc_id` reachable from HEAD.

    Scans `refs/tags/<doc_id>-issue-*`, filters to those whose target commit
    is an ancestor of HEAD (so cross-branch tags don't pollute the value),
    and returns the highest version-sorted issue. Empty string when no such
    tag exists or HEAD is unborn.
    """
    try:
        head_oid = repo.head.target
    except (pygit2.GitError, KeyError):
        return ""
    prefix = f"refs/tags/{doc_id}{_ISSUE_TAG_INFIX}"
    candidates: list[tuple[tuple[int, ...], str]] = []
    for ref_name in repo.references:
        if not ref_name.startswith(prefix):
            continue
        issue_str = ref_name[len(prefix):]
        if not _ISSUE_RE.match(issue_str):
            continue
        try:
            target_commit = repo.references[ref_name].peel(pygit2.Commit)
        except (pygit2.GitError, KeyError):
            continue
        if not _is_ancestor(repo, target_commit.id, head_oid):
            continue
        candidates.append((_issue_key(issue_str), issue_str))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def issue_tags_by_commit(repo: pygit2.Repository, doc_id: str) -> dict[str, str]:
    """Map full commit-oid hex -> issue string for every `<doc_id>-issue-*` tag.

    When multiple tags point to the same commit, the highest version wins.
    No reachability filtering: tags from other branches are included, since
    the caller looks up specific commits by oid.
    """
    prefix = f"refs/tags/{doc_id}{_ISSUE_TAG_INFIX}"
    by_commit: dict[str, tuple[tuple[int, ...], str]] = {}
    for ref_name in repo.references:
        if not ref_name.startswith(prefix):
            continue
        issue_str = ref_name[len(prefix):]
        if not _ISSUE_RE.match(issue_str):
            continue
        try:
            target_commit = repo.references[ref_name].peel(pygit2.Commit)
        except (pygit2.GitError, KeyError):
            continue
        key = _issue_key(issue_str)
        oid_hex = str(target_commit.id)
        existing = by_commit.get(oid_hex)
        if existing is None or key > existing[0]:
            by_commit[oid_hex] = (key, issue_str)
    return {oid: pair[1] for oid, pair in by_commit.items()}


def create_issue_tag(
    repo: pygit2.Repository,
    doc_id: str,
    issue: str,
    oid: pygit2.Oid,
) -> str:
    """Create a lightweight `refs/tags/<doc_id>-issue-<issue>` ref at `oid`.

    Validates the issue format, refuses an issue that isn't higher than the
    document's current one (so '2' after '3', or '03' after '3', can't
    create a confusing second tag), and refuses to overwrite an existing tag.
    Returns the bare tag name (without the `refs/tags/` prefix).
    """
    validate_issue(issue)
    ref_name = _issue_tag_ref(doc_id, issue)
    if ref_name in repo.references:
        raise GitError(f"tag {ref_name[len('refs/tags/'):]!r} already exists")
    current = current_issue(repo, doc_id)
    if current and _issue_key(issue) <= _issue_key(current):
        raise GitError(
            f"issue {issue!r} must be higher than the current issue {current!r}"
        )
    try:
        repo.references.create(ref_name, oid)
    except (pygit2.GitError, ValueError) as e:
        raise GitError(f"failed to create tag {ref_name!r}: {e}") from e
    return f"{doc_id}{_ISSUE_TAG_INFIX}{issue}"


# --- free-named tags ------------------------------------------------------

# `<anything>-issue-<n>` belongs to the issue tags: `current_issue` would read
# such a tag as an issue of a document with that id.
_ISSUE_FORM_RE = re.compile(
    r"^.+" + re.escape(_ISSUE_TAG_INFIX) + r"\d+(\.\d+)*$"
)


def tag_names(repo: pygit2.Repository) -> tuple[str, ...]:
    """Every tag's name (without `refs/tags/`), sorted."""
    prefix = "refs/tags/"
    return tuple(
        sorted(r[len(prefix):] for r in repo.references if r.startswith(prefix))
    )


def tag_name_error(name: str, existing: Iterable[str]) -> str:
    """Why `name` can't be a new tag, or "" when it can.

    Pure (no repo access), so the commit dialog checks as the user types:
    `existing` is the repo's tag names (`tag_names`). Refuses names git
    won't take (spaces, `~^:?*[\\`, `..`, a leading `-`, ...), names already
    taken, and the `<doc-id>-issue-<n>` form reserved for issue tags.
    """
    if not name:
        return "Tag name must not be empty."
    if name.startswith("-") or not pygit2.reference_is_valid_name(
        f"refs/tags/{name}"
    ):
        return (
            f"{name!r} is not a valid tag name (no spaces or any of "
            "~ ^ : ? * [ \\, no '..', and it must not start with '-')."
        )
    if name in set(existing):
        return f"Tag {name!r} already exists."
    if _ISSUE_FORM_RE.match(name):
        return (
            f"{name!r} has the form of an issue tag (<doc-id>-issue-<n>); "
            "raise the issue for that instead."
        )
    return ""


def create_tag(
    repo: pygit2.Repository,
    name: str,
    oid: pygit2.Oid,
    message: str = "",
) -> str:
    """Create an annotated tag `name` at commit `oid`. Returns `name`.

    Annotated, unlike the issue tags: it records who tagged and when, and
    `git push --follow-tags` publishes it. The tagger is the git identity
    (as for commits); an empty `message` becomes the tag name. Raises
    `GitError` for any name `tag_name_error` refuses.
    """
    error = tag_name_error(name, tag_names(repo))
    if error:
        raise GitError(error)
    tagger = _resolve_signature(repo)
    try:
        repo.create_tag(
            name, oid, pygit2.GIT_OBJECT_COMMIT, tagger, message or name
        )
    except (pygit2.GitError, ValueError) as e:
        raise GitError(f"failed to create tag {name!r}: {e}") from e
    return name
