"""Repository-wide operations across multiple documents.

A mellon repository is a git repo whose top-level folders are documents. This
module discovers and loads all of them so queries like "everything that links
to <doc>/<id>" can work across document boundaries.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .git_ops import GitError, discover_repo
from .loader import LoaderError, entry_problem, has_manifest, load_document
from .schema import Document, Link


@dataclass(frozen=True, slots=True)
class LoadedDoc:
    """A loaded document together with its on-disk folder."""

    folder: Path
    document: Document


# Requirements: MELLON-STORE-SRS-29
@dataclass(frozen=True, slots=True)
class DocLoadError:
    """A document folder that failed to load.

    `file` and `line` point at the offending file when the loader names one.
    `message` is the loader's text, with "(unresolved merge conflict?)"
    appended when that file contains git conflict markers.
    """

    doc_id: str
    folder: Path
    file: Path | None
    line: int | None
    message: str


# Requirements: MELLON-STORE-SRS-30
_CONFLICT_MARKER = re.compile(rb"^<{7}", re.MULTILINE)


# Requirements: MELLON-STORE-SRS-30, MELLON-STORE-SRS-229, MELLON-STORE-SRS-241
def _error_file(err: LoaderError) -> Path | None:
    # Most loader messages start with "<path>: ". Others start with other text
    # ("Not a directory", or an attribute name from the file, which can hold
    # ": ", "..", an absolute path or a share). So the start counts only when,
    # as text, it names something inside the document folder the load error
    # comes from, with no ".." part, and no folder or file on the way is a
    # link: the message can't make Mellon look at a path outside the document.
    folder = getattr(err, "folder", None)
    head, sep, _rest = str(err).partition(": ")
    if folder is None or not sep:
        return None
    try:
        rel = Path(head).relative_to(folder)
    except ValueError:
        return None
    if not rel.parts or ".." in rel.parts:
        return None
    path = folder
    for part in rel.parts:
        path = path / part
        if entry_problem(path) is not None:
            return None
    return path if path.is_file() else None


# Requirements: MELLON-STORE-SRS-30
def _with_conflict_note(err: LoaderError, file: Path | None) -> str:
    message = str(err)
    if file is not None:
        try:
            if _CONFLICT_MARKER.search(file.read_bytes()):
                message += " (unresolved merge conflict?)"
        except OSError:
            pass
    return message


def load_error_message(err: LoaderError) -> str:
    """The loader's message as a `DocLoadError` gives it: with "(unresolved
    merge conflict?)" appended when the file it names holds git conflict
    markers (MELLON-STORE-SRS-30)."""
    return _with_conflict_note(err, _error_file(err))


# Requirements: MELLON-CHECK-SRS-90, MELLON-STORE-SRS-29
def _doc_load_error(folder: Path, err: LoaderError) -> DocLoadError:
    file = _error_file(err)
    # lxml's XMLSyntaxError (the cause of a parse failure) carries the line.
    line = getattr(err.__cause__, "lineno", None)
    return DocLoadError(
        doc_id=folder.name,
        folder=folder,
        file=file,
        line=line,
        message=_with_conflict_note(err, file),
    )


# Requirements: MELLON-MCP-SRS-9, MELLON-STORE-SRS-13, MELLON-STORE-SRS-19
def discover_documents(repo_root: str | Path) -> list[Path]:
    """Return sorted paths of document folders directly under `repo_root`.

    A folder qualifies when `<folder>/<folder_name>.xml` exists, letter case
    included (see `loader.has_manifest`). Nothing is
    parsed here, so a broken document is still discovered and its error
    surfaces when it is loaded (see `load_all`). Folders starting with `.`
    are skipped. Deeper nesting is not supported.
    """
    repo_root = Path(repo_root)
    try:
        children = sorted(repo_root.iterdir())
    except OSError as e:
        raise LoaderError(f"cannot read repo root {repo_root}: {e}") from e
    out: list[Path] = []
    for child in children:
        # MELLON-STORE-SRS-229: a link or junction isn't followed, so it isn't
        # found to be a document.
        if child.name.startswith(".") or entry_problem(child) or not child.is_dir():
            continue
        # MELLON-STORE-SRS-18: <folder name>.xml, letter case included.
        if has_manifest(child):
            out.append(child)
    return out


def doc_folder_names(repo_root: str | Path) -> frozenset[str]:
    """Names of top-level folders that look like documents, without parsing.

    A folder qualifies when `<folder>/<folder_name>.xml` exists (the same
    rule as `discover_documents`); the manifest is not loaded, so this is
    cheap enough to call on every window activation. Dot-folders (including
    `.git`) are skipped. Returns an empty set when `repo_root` can't be read.
    """
    root = Path(repo_root)
    out: set[str] = set()
    try:
        children = list(root.iterdir())
    except OSError:
        return frozenset()
    for child in children:
        if child.name.startswith(".") or entry_problem(child) or not child.is_dir():
            continue
        if has_manifest(child):
            out.add(child.name)
    return frozenset(out)


def folders_fingerprint(
    folders: Iterable[Path],
) -> frozenset[tuple[str, int, int]]:
    """Cheap change-detection fingerprint over a set of folders.

    One `(key, st_mtime_ns, st_size)` triple per file, where `key` is
    `<folder_name>/<relative_path>`. Comparing two fingerprints catches
    edits, additions, and deletions — committed or not — which is what the
    GUI needs to notice writes made by another process (e.g. the MCP
    server). Folders or files that vanish mid-walk are skipped.
    """
    out: set[tuple[str, int, int]] = set()
    for folder in folders:
        folder = Path(folder)
        stack = [(str(folder), f"{folder.name}/")]
        while stack:
            path, prefix = stack.pop()
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        key = f"{prefix}{entry.name}"
                        try:
                            # Links aren't followed (MELLON-STORE-SRS-229).
                            if entry.is_symlink() or entry.is_junction():
                                st = entry.stat(follow_symlinks=False)
                                out.add((key, st.st_mtime_ns, st.st_size))
                            elif entry.is_dir(follow_symlinks=False):
                                stack.append((entry.path, f"{key}/"))
                            elif entry.is_file(follow_symlinks=False):
                                st = entry.stat(follow_symlinks=False)
                                out.add((key, st.st_mtime_ns, st.st_size))
                        except OSError:
                            continue
            except OSError:
                continue
    return frozenset(out)


# Requirements: MELLON-STORE-SRS-27, MELLON-STORE-SRS-28
def load_all(
    repo_root: str | Path, *, load_errors: list[DocLoadError] | None = None
) -> dict[str, LoadedDoc]:
    """Load every document under `repo_root`, keyed by doc id.

    A document that fails to load is left out of the result. Pass a list as
    `load_errors` to have a `DocLoadError` appended for each such document.
    """
    repo_root = Path(repo_root)
    out: dict[str, LoadedDoc] = {}
    for folder in discover_documents(repo_root):
        try:
            doc = load_document(folder)
        except LoaderError as e:
            if load_errors is not None:
                load_errors.append(_doc_load_error(folder, e))
            continue
        if doc.id in out:
            raise LoaderError(
                f"Duplicate document id {doc.id!r}: folders "
                f"{out[doc.id].folder} and {folder}"
            )
        out[doc.id] = LoadedDoc(folder=folder, document=doc)
    return out


def load_sibling_docs(
    folder: Path,
    doc: Document,
    *,
    load_errors: list[DocLoadError] | None = None,
) -> dict[str, LoadedDoc]:
    """Find every doc the tool can see from `folder`, keyed by doc id.

    Tries (in order) the enclosing git repo, then `folder.parent`, and finally
    falls back to a dict containing just the given document. Useful for cross-
    doc operations (incoming links, trace, cascading deletes) that should work
    both inside and outside a git repo. `load_errors` collects the documents
    that failed to load under the root that was used (see `load_all`).
    """
    for root in _candidate_roots(folder):
        errors: list[DocLoadError] = []
        try:
            docs = load_all(root, load_errors=errors)
        except LoaderError:
            continue
        if doc.id in docs:
            if load_errors is not None:
                load_errors.extend(errors)
            return docs
    return {doc.id: LoadedDoc(folder=folder, document=doc)}


def _candidate_roots(folder: Path):
    try:
        _repo, repo_root = discover_repo(folder)
        yield repo_root
    except GitError:
        pass
    yield folder.resolve().parent


def all_objects(
    docs: dict[str, LoadedDoc],
) -> list[tuple[str, str, str]]:
    """Every object across every document, in outline pre-order.

    Returns `(doc_id, obj_id, title)` triples where `title` is the first line
    of the object's description (truncated to 200 characters). Documents are
    iterated in sorted id order; within each doc the outline order is used so
    headings precede their leaves — handy for list-picker UX.
    """
    out: list[tuple[str, str, str]] = []
    for doc_id in sorted(docs.keys()):
        doc = docs[doc_id].document
        for obj_id in doc.iter_outline_ids():
            obj = doc.objects.get(obj_id)
            title = ""
            if obj and obj.description:
                title = obj.description.splitlines()[0][:200]
            out.append((doc_id, obj_id, title))
    return out


def parent_doc_ids(docs: dict[str, LoadedDoc], doc_id: str) -> set[str]:
    """Distinct target documents reached by outgoing links from `doc_id`.

    A "parent" is any other document that any object in `doc_id` links up to.
    Returns an empty set when the doc is unknown or has no outgoing links.
    `doc_id` itself is excluded even if there are intra-document links.
    """
    loaded = docs.get(doc_id)
    if loaded is None:
        return set()
    out: set[str] = set()
    for link_list in loaded.document.links.values():
        for link in link_list:
            if link.target_doc and link.target_doc != doc_id:
                out.add(link.target_doc)
    return out


# Requirements: MELLON-TRACE-SRS-33
def incoming_links(
    docs: dict[str, LoadedDoc], target_doc: str, target_id: str
) -> list[tuple[str, str, Link]]:
    """All incoming links to (target_doc, target_id) across `docs`.

    Returns (source_doc_id, source_object_id, link) triples.
    """
    out: list[tuple[str, str, Link]] = []
    for src_doc_id, loaded in docs.items():
        for source_id, link_list in loaded.document.links.items():
            for link in link_list:
                if link.target_doc == target_doc and link.target_id == target_id:
                    out.append((src_doc_id, source_id, link))
    return out
