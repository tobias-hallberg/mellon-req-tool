"""`DocumentService` — the single façade between adapters (GUI/MCP) and core.

Design goals:
    - One class, one error type (`ServiceError`), one result shape (`MutationResult`).
    - Optional in-memory cache so the GUI can avoid re-parsing on every keystroke.
    - Mutations return the already-reloaded post-state so callers never need to
      remember to call `load_document` again.

The service delegates all on-disk work to the existing core modules:
    `editor`, `loader`, `repo`, `suspects`, `trace`, `validator`, `git_ops`.
None of those modules change; this file just wraps them.
"""

from __future__ import annotations

import contextlib
import functools
from pathlib import Path
from typing import Any, Callable, Iterable

from datetime import date as _date

from .. import editor
from ..editor import EditorError
from ..git_ops import (
    GitError,
    create_issue_tag,
    create_tag,
    current_issue as git_current_issue,
    discard_paths,
    discover_repo,
    dirty_paths_under,
    git_user_name,
    head_doc_folder_names,
    head_summary,
    log_for_path,
    tag_name_error,
    tag_names,
)
from ..git_ops import commit as git_commit
from ..loader import LoaderError, load_document
from ..repo import (
    DocLoadError,
    LoadedDoc,
    all_objects as repo_all_objects,
    doc_folder_names,
    incoming_links as repo_incoming_links,
    load_all,
    parent_doc_ids as repo_parent_doc_ids,
)
from ..schema import Document, FilterRule, Link, RequirementObject, Table
from ..suspects import (
    LinkState,
    LinkStatus,
    ack_all as suspects_ack_all,
    scan_document,
    scan_repo,
)
from ..coverage import CoverageReport, coverage_report as _coverage_report
from ..search import SearchHit, search_objects as _search_objects
from ..trace import TraceEdge, trace_down, trace_up
from ..validator import ValidationError, validate

from .cache import RepoCache
from .errors import ErrorCode, ServiceError
from .results import (
    CommitResult,
    CompositeWriteResult,
    DeleteResult,
    DocCommitPreview,
    MutationResult,
)


def _find_outline_parent(
    nodes, target_id: str, current_parent: str | None = None
) -> tuple[bool, str | None]:
    """Walk an outline tree to find `target_id`.

    Returns `(found, parent_ref_id)` where `parent_ref_id` is None when the
    target is at the top level. The boolean disambiguates "found at top
    level (parent=None)" from "not in the outline at all".
    """
    for n in nodes:
        if n.ref_id == target_id:
            return True, current_parent
        found, parent = _find_outline_parent(n.children, target_id, n.ref_id)
        if found:
            return True, parent
    return False, None


# Editor raises `EditorError` with free-form messages; these patterns let us
# classify into ALREADY_EXISTS / CONFLICT / NOT_FOUND, falling back to
# VALIDATION otherwise.
_ALREADY_EXISTS_SUBSTRINGS = (
    "already exists",
    "already declared",
)
_CONFLICT_SUBSTRINGS = (
    "has children in the outline",
)
_NOT_FOUND_SUBSTRINGS = (
    "no object ",
    "not in outline",
    "not found in outline",
    "not found under target parent",
    "not found in manifest",
)


def _classify_editor_error(err: EditorError) -> ServiceError:
    msg = str(err)
    lower = msg.lower()
    if any(s in lower for s in _ALREADY_EXISTS_SUBSTRINGS):
        return ServiceError(ErrorCode.ALREADY_EXISTS, msg)
    if any(s in lower for s in _CONFLICT_SUBSTRINGS):
        return ServiceError(ErrorCode.CONFLICT, msg)
    if any(s in lower for s in _NOT_FOUND_SUBSTRINGS):
        return ServiceError(ErrorCode.NOT_FOUND, msg)
    return ServiceError(ErrorCode.VALIDATION, msg)


def _classify_write_error(err: ValueError | OSError) -> ServiceError:
    """lxml refuses text it can't serialize with ValueError; disk failures are OSError."""
    if isinstance(err, OSError):
        return ServiceError(ErrorCode.IO, str(err))
    return ServiceError(ErrorCode.VALIDATION, str(err))


# Called as listener(doc_id, label, changes) after a write method changed
# files; see DocumentService.add_change_listener.
ChangeListener = Callable[[str, str, editor.Journal], None]


def _recorded(method):
    """Record the files a write method changes and pass them to the change
    listeners (the GUI's undo). A call made inside another recorded call
    belongs to the outer one."""

    @functools.wraps(method)
    def wrapper(self, doc_id, *args, **kwargs):
        if editor.journal_open():
            return method(self, doc_id, *args, **kwargs)
        with editor.journal(rollback=False) as changes:
            result = method(self, doc_id, *args, **kwargs)
        if changes.paths:
            label = method.__name__.replace("_", " ")
            for listener in tuple(self._change_listeners):
                listener(doc_id, label, changes)
        return result

    return wrapper


class DocumentService:
    """Owns document I/O, mutations, reads, and (optionally) a repo cache.

    Args:
        repo_root: root folder whose immediate children are document folders.
            May be None for transient service instances that only call
            stateless utilities — but every method that needs a repo will
            raise `ServiceError(VALIDATION)`.
        cache: if True, reads populate an in-memory cache and mutations
            invalidate affected docs. GUI turns this on; MCP leaves it off.
        read_only: if True, any mutation raises `ServiceError(READ_ONLY)`.
            Used by `mellon-mcp --read-only`.
    """

    def __init__(
        self,
        repo_root: str | Path | None,
        *,
        cache: bool = False,
        read_only: bool = False,
    ) -> None:
        self._repo_root: Path | None = Path(repo_root).resolve() if repo_root else None
        self._cache: RepoCache | None = RepoCache() if cache else None
        self._read_only = read_only
        # pygit2.Repository for the repo root, opened by the first `_git_head`.
        self._git_repo = None
        self._change_listeners: list[ChangeListener] = []

    # --- properties -----------------------------------------------------

    @property
    def repo_root(self) -> Path | None:
        return self._repo_root

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def cache(self) -> RepoCache | None:
        return self._cache

    # --- internals ------------------------------------------------------

    def _require_repo_root(self) -> Path:
        if self._repo_root is None:
            raise ServiceError(
                ErrorCode.VALIDATION, "no repo_root configured on this service"
            )
        return self._repo_root

    def _require_write(self) -> None:
        if self._read_only:
            raise ServiceError(
                ErrorCode.READ_ONLY, "this DocumentService is read-only"
            )

    def _doc_folder(self, doc_id: str) -> Path:
        root = self._require_repo_root()
        try:
            # A doc_id names a folder directly under the repo; "../X/X" must
            # not reach a manifest that happens to exist outside it.
            folder = editor._child_path(root, doc_id)
        except EditorError:
            folder = None
        if (
            folder is None
            or not folder.is_dir()
            or not (folder / f"{doc_id}.xml").is_file()
        ):
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"document {doc_id!r} not found under {root}"
            )
        return folder

    def _load(self, doc_id: str) -> LoadedDoc:
        """Load or fetch `doc_id` from cache. Raises on disk / parse errors."""
        if self._cache is not None:
            hit = self._cache.get(doc_id)
            if hit is not None:
                return hit
        folder = self._doc_folder(doc_id)
        try:
            doc = load_document(folder)
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        loaded = LoadedDoc(folder=folder, document=doc)
        if self._cache is not None:
            self._cache.put(loaded)
        return loaded

    def _reload(self, doc_id: str) -> LoadedDoc:
        """Force a disk re-read for `doc_id`, refreshing the cache entry."""
        if self._cache is not None:
            self._cache.invalidate([doc_id])
        return self._load(doc_id)

    def _docs_of(self, paths: Iterable[Path]) -> tuple[str, ...]:
        """Ids of the document folders `paths` lie in, sorted."""
        root = self._require_repo_root()
        ids: set[str] = set()
        for p in paths:
            try:
                parts = Path(p).relative_to(root).parts
            except ValueError:
                continue
            if len(parts) > 1:
                ids.add(parts[0])
        return tuple(sorted(ids))

    def _affected_docs_from_delete(
        self, doc_id: str, report: editor.DeleteReport
    ) -> tuple[str, ...]:
        """Target doc + every sibling doc whose link file was touched."""
        if not report.performed:
            return ()
        ids: set[str] = {doc_id}
        for src_doc, _src_id, _ltype in report.incoming:
            ids.add(src_doc)
        return tuple(sorted(ids))

    # --- read surface ---------------------------------------------------

    def list_documents(
        self, *, load_errors: list[DocLoadError] | None = None
    ) -> list[LoadedDoc]:
        """Every document under the repo root, sorted by doc id.

        `load_errors` works as in `get_all_documents`.
        """
        docs = self.get_all_documents(load_errors=load_errors)
        return [docs[k] for k in sorted(docs.keys())]

    def get_all_documents(
        self, *, load_errors: list[DocLoadError] | None = None
    ) -> dict[str, LoadedDoc]:
        """All documents keyed by id (mostly for cross-doc traversal).

        A document that fails to load is left out. Pass a list as
        `load_errors` to have a `DocLoadError` appended for each such
        document (from the same load, or the same cached snapshot).
        """
        cached = self._cache.get_all() if self._cache is not None else None
        if cached is not None:
            if load_errors is not None:
                load_errors.extend(self._cache.load_errors())
            return cached
        root = self._require_repo_root()
        errors: list[DocLoadError] = []
        try:
            docs = load_all(root, load_errors=errors)
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        if self._cache is not None:
            self._cache.put_all(docs, errors)
        if load_errors is not None:
            load_errors.extend(errors)
        return docs

    def get_document(self, doc_id: str) -> Document:
        return self._load(doc_id).document

    def get_loaded_document(self, doc_id: str) -> LoadedDoc:
        return self._load(doc_id)

    def current_issue(self, doc_id: str) -> str:
        """Highest reachable `<doc_id>-issue-*` tag from HEAD, or '' when no
        such tag / not in a git repo. Reads via `git_ops.current_issue`."""
        try:
            folder = self._doc_folder(doc_id)
        except ServiceError:
            return ""
        try:
            repo, _root = discover_repo(folder)
        except GitError:
            return ""
        return git_current_issue(repo, doc_id)

    def parent_documents(self, doc_id: str) -> dict[str, Document]:
        """The declared parent documents of `doc_id` that load, keyed by id:
        what the PDF's Traceability section needs (see `export.pdf`). A
        parent that fails to load is left out; the PDF names it."""
        doc = self._load(doc_id).document
        parents: dict[str, Document] = {}
        for pid in doc.parents:
            try:
                parents[pid] = self._load(pid).document
            except ServiceError:
                continue
        return parents

    def get_object(self, doc_id: str, obj_id: str) -> RequirementObject:
        doc = self._load(doc_id).document
        obj = doc.objects.get(obj_id)
        if obj is None:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"no object {obj_id!r} in document {doc_id!r}"
            )
        return obj

    def validate_document(self, doc_id: str) -> list[ValidationError]:
        return validate(self._load(doc_id).document)

    def validate_repo(
        self, *, load_errors: list[DocLoadError] | None = None
    ) -> dict[str, list[ValidationError]]:
        """Run `validate` on every document; return errors keyed by doc id.

        Pure in-memory work (see `validator.validate`) — cheap to call as a
        repo-wide gate, even on large repos. Empty list per doc means clean.
        Documents that fail to load can't be validated; collect them with
        `load_errors` (as in `get_all_documents`).
        """
        out: dict[str, list[ValidationError]] = {}
        for loaded in self.list_documents(load_errors=load_errors):
            doc_id = loaded.document.id
            out[doc_id] = validate(loaded.document)
        return out

    def repo_health(
        self, *, load_errors: list[DocLoadError] | None = None
    ) -> dict:
        """One-call snapshot of repo state for the agent's first read.

        Aggregates per-doc counts (objects, links, validation errors, suspect
        and broken links, current issue tag, declared parents) plus pairwise
        coverage for every declared parent relationship. Returns a JSON-
        serializable dict; the MCP tool surfaces it with a `load_errors` key
        added. Documents that fail to load are left out; collect them with
        `load_errors` (as in `get_all_documents`).
        """
        docs = self.get_all_documents(load_errors=load_errors)
        # Pull repo-wide suspects (returns dict[doc_id, [LinkStatus]]). Falls
        # back to empty when the repo isn't a git repo — that's fine, we still
        # want validation + counts.
        try:
            suspect_scan = self.scan_suspects(None)
        except ServiceError:
            suspect_scan = {}

        documents: list[dict] = []
        total_validation_errors = 0
        total_suspect_count = 0
        total_broken_count = 0

        for doc_id in sorted(docs.keys()):
            loaded = docs[doc_id]
            doc = loaded.document
            errors = validate(doc)
            statuses = suspect_scan.get(doc_id, [])
            suspect = sum(
                1 for s in statuses if s.state is LinkState.SUSPECT
            )
            broken = sum(1 for s in statuses if s.state is LinkState.BROKEN)
            total_validation_errors += len(errors)
            total_suspect_count += suspect
            total_broken_count += broken
            documents.append(
                {
                    "doc_id": doc_id,
                    "object_count": len(doc.objects),
                    "link_count": sum(len(v) for v in doc.links.values()),
                    "current_issue": self.current_issue(doc_id),
                    "parents": list(doc.parents),
                    "validation_error_count": len(errors),
                    "suspect_count": suspect,
                    "broken_count": broken,
                }
            )

        coverage_rows: list[dict] = []
        for doc_id in sorted(docs.keys()):
            doc = docs[doc_id].document
            for parent_id in doc.parents:
                if parent_id not in docs:
                    continue
                # child -> parent (outgoing): "does each child obj justify itself?"
                try:
                    out_report = _coverage_report(
                        docs, doc_id, parent_id, direction="outgoing"
                    )
                    coverage_rows.append(
                        {
                            "source_doc": doc_id,
                            "target_doc": parent_id,
                            "direction": "outgoing",
                            "total": out_report.total,
                            "covered": out_report.covered,
                            "coverage_percent": out_report.coverage_percent,
                        }
                    )
                except Exception:  # pragma: no cover — defensive
                    pass
                # parent -> child (incoming): "is each parent obj verified by a child?"
                try:
                    in_report = _coverage_report(
                        docs, parent_id, doc_id, direction="incoming"
                    )
                    coverage_rows.append(
                        {
                            "source_doc": parent_id,
                            "target_doc": doc_id,
                            "direction": "incoming",
                            "total": in_report.total,
                            "covered": in_report.covered,
                            "coverage_percent": in_report.coverage_percent,
                        }
                    )
                except Exception:  # pragma: no cover — defensive
                    pass

        return {
            "documents": documents,
            "coverage": coverage_rows,
            "total_validation_errors": total_validation_errors,
            "total_suspect_count": total_suspect_count,
            "total_broken_count": total_broken_count,
        }

    def scan_suspects(self, doc_id: str | None = None) -> dict[str, list[LinkStatus]]:
        """Classify links in one doc (if `doc_id` given) or the whole repo."""
        # Read before scanning: a commit landing mid-scan then costs a
        # re-scan, never a stale hit.
        head = self._git_head() if self._cache is not None else None
        if doc_id is not None:
            if self._cache is not None:
                hit = self._cache.suspect_status(doc_id, head)
                if hit is not None:
                    return {doc_id: list(hit)}
            loaded = self._load(doc_id)
            try:
                statuses = scan_document(loaded.document, loaded.folder)
            except GitError as e:
                raise ServiceError(ErrorCode.GIT, str(e)) from e
            if self._cache is not None:
                self._cache.put_suspect_status(doc_id, statuses, head)
            return {doc_id: statuses}

        root = self._require_repo_root()
        try:
            out = scan_repo(root)
        except (LoaderError, GitError) as e:
            code = ErrorCode.LOAD if isinstance(e, LoaderError) else ErrorCode.GIT
            raise ServiceError(code, str(e)) from e
        if self._cache is not None:
            for did, statuses in out.items():
                self._cache.put_suspect_status(did, statuses, head)
        return out

    def _git_head(self) -> str | None:
        """Short HEAD hash of the repo root's git repo; None without git or
        commits. Keys cached suspect scans, which every commit can change.

        The GUI asks once per grid row, so the repo is opened once and kept:
        reading HEAD from it still sees commits made by any git client.
        """
        if self._git_repo is None:
            try:
                self._git_repo, _root = discover_repo(self._require_repo_root())
            except GitError:
                return None
        return head_summary(self._git_repo)[1]

    def trace_up(self, doc_id: str, obj_id: str) -> tuple[TraceEdge, ...]:
        docs = self.get_all_documents()
        if doc_id not in docs:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"document {doc_id!r} not in repo"
            )
        if obj_id not in docs[doc_id].document.objects:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"no object {obj_id!r} in document {doc_id!r}"
            )
        return trace_up(docs, doc_id, obj_id)

    def trace_down(self, doc_id: str, obj_id: str) -> tuple[TraceEdge, ...]:
        docs = self.get_all_documents()
        if doc_id not in docs:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"document {doc_id!r} not in repo"
            )
        if obj_id not in docs[doc_id].document.objects:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"no object {obj_id!r} in document {doc_id!r}"
            )
        return trace_down(docs, doc_id, obj_id)

    def incoming_links(
        self, target_doc: str, target_id: str
    ) -> list[tuple[str, str, Link]]:
        return repo_incoming_links(
            self.get_all_documents(), target_doc, target_id
        )

    def all_objects(self) -> list[tuple[str, str, str]]:
        """Every (doc_id, obj_id, title) across the repo, in outline pre-order."""
        return repo_all_objects(self.get_all_documents())

    def search_objects(
        self,
        query: str,
        *,
        doc_ids: tuple[str, ...] | None = None,
        attr_filters: dict[str, str] | None = None,
        max_results: int = 50,
    ) -> list[SearchHit]:
        """Substring search across the repo. Cheap; backed by an in-memory scan."""
        return _search_objects(
            self.get_all_documents(),
            query,
            doc_ids=doc_ids,
            attr_filters=attr_filters,
            max_results=max_results,
        )

    def coverage_report(
        self,
        source_doc: str,
        target_doc: str,
        *,
        direction: str = "incoming",
        link_type: str | None = None,
        include_headings: bool = False,
        transitive: bool = False,
    ) -> CoverageReport:
        """Per-object coverage of `source_doc` against `target_doc`."""
        docs = self.get_all_documents()
        if source_doc not in docs:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"source document {source_doc!r} not in repo"
            )
        if target_doc not in docs:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"target document {target_doc!r} not in repo"
            )
        if direction not in ("incoming", "outgoing"):
            raise ServiceError(
                ErrorCode.VALIDATION,
                f"direction must be 'incoming' or 'outgoing', got {direction!r}",
            )
        return _coverage_report(
            docs,
            source_doc,
            target_doc,
            direction=direction,
            link_type=link_type,
            include_headings=include_headings,
            transitive=transitive,
        )

    def git_head(self) -> tuple[str | None, str | None]:
        root = self._require_repo_root()
        try:
            repo, _ = discover_repo(root)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        return head_summary(repo)

    def git_log(
        self,
        doc_id: str,
        obj_id: str | None = None,
        *,
        max_entries: int = 10,
        since_commit: str | None = None,
    ) -> list[dict[str, Any]]:
        """Commits touching an object's XML or figure (or the doc's manifest
        if `obj_id=None`)."""
        folder = self._doc_folder(doc_id)
        try:
            repo, root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        if obj_id is None:
            paths = [f"{doc_id}/{doc_id}.xml"]
        else:
            # Ensure the object exists so we give a useful error up front.
            doc = self._load(doc_id).document
            if obj_id not in doc.objects:
                raise ServiceError(
                    ErrorCode.NOT_FOUND, f"no object {obj_id!r} in document {doc_id!r}"
                )
            paths = [f"{doc_id}/objects/{obj_id}.xml", f"{doc_id}/figures/{obj_id}.svg"]
        try:
            return log_for_path(
                repo, paths, max_entries=max_entries, since_commit=since_commit
            )
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e

    # --- mutation surface ----------------------------------------------

    @_recorded
    def create_object(
        self,
        doc_id: str,
        *,
        parent_id: str | None = None,
        description: str = "",
    ) -> MutationResult[str]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            new_id = editor.create_object(folder, parent_id=parent_id, description=description)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=new_id, refreshed=refreshed, affected_docs=(doc_id,))

    # --- composite writes -----------------------------------------------
    #
    # These methods bundle several editor primitives into one logical call so
    # an agent can author a fully-populated requirement in a single MCP round
    # trip. Atomicity: pre-validate everything up front so the common failure
    # mode (bad attr name, disallowed target doc, etc.) is caught before any
    # file is touched. We don't try to roll back partial I/O failures — the
    # project has no transaction infrastructure and adding it just for these
    # tools would be invasive.

    @staticmethod
    def _validate_value_for_attr(attr, value: str) -> None:
        """Kind-check `value` against `attr`. Empty value is always accepted.

        Mirrors `validator._check_attr_value` so the diagnostics match what
        `validate_document` would emit later. Accepts a not-yet-persisted
        `Attribute` (used by `define_attribute(default_value=…)` to validate
        before adding the schema entry).
        """
        if value == "":
            return
        name = attr.name
        if attr.kind == "enum" and value not in attr.options:
            raise ServiceError(
                ErrorCode.VALIDATION,
                f"attribute {name!r}: {value!r} is not a valid option "
                f"(options: {list(attr.options)})",
            )
        if attr.kind == "integer":
            try:
                int(value, 10)
            except ValueError as e:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"attribute {name!r}: {value!r} is not a base-10 integer",
                ) from e
        elif attr.kind == "real":
            try:
                float(value)
            except ValueError as e:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"attribute {name!r}: {value!r} is not a real number",
                ) from e
        elif attr.kind == "boolean":
            if value not in ("true", "false"):
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"attribute {name!r}: {value!r} is not 'true' or 'false'",
                )
        elif attr.kind == "date":
            try:
                _date.fromisoformat(value)
            except ValueError as e:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"attribute {name!r}: {value!r} is not "
                    f"an ISO 8601 date (YYYY-MM-DD)",
                ) from e

    def _validate_attr_pair(self, doc: Document, name: str, value: str) -> None:
        """Raise `ServiceError(VALIDATION)` if `(name, value)` doesn't fit the schema.

        Shared by every composite write tool. Looks up the attribute by name
        on `doc` and dispatches to `_validate_value_for_attr` for the kind check.
        """
        attr = doc.attribute(name)
        if attr is None:
            raise ServiceError(
                ErrorCode.VALIDATION,
                f"attribute {name!r} is not declared in document {doc.id!r}",
            )
        self._validate_value_for_attr(attr, value)

    def _validate_composite_attrs(
        self, doc: Document, attrs: dict[str, str]
    ) -> None:
        """Raise `ServiceError(VALIDATION)` if any attr name or value is bad."""
        for name, value in attrs.items():
            self._validate_attr_pair(doc, name, value)
            self._validate_xml_text(value, f"attribute {name!r}")

    @staticmethod
    def _validate_xml_text(value: str, what: str) -> None:
        """Raise `ServiceError(VALIDATION)` if `value` holds text XML can't store."""
        try:
            editor._sanitize_text(value, what)
        except EditorError as e:
            raise _classify_editor_error(e) from e

    def _validate_composite_links(
        self, doc: Document, links: list[dict], *, require_type: bool
    ) -> list[tuple[str, str, str | None]]:
        """Normalize and policy-check link entries.

        Returns the list as `(target_doc, target_id, link_type)` tuples; for
        `remove_links`, the `link_type` slot is None (callers don't supply it).
        Links to add (`require_type=True`) must also carry a storable type
        string, name an existing target object and appear only once in the
        request.
        """
        allowed = {doc.id, *doc.parents}
        out: list[tuple[str, str, str | None]] = []
        seen: set[tuple[str, str]] = set()
        target_objects: dict[str, Any] = {doc.id: doc.objects}
        for link in links:
            target_doc = link.get("target_doc")
            target_id = link.get("target_id")
            link_type = link.get("type") if require_type else None
            missing = []
            if not target_doc:
                missing.append("target_doc")
            if not target_id:
                missing.append("target_id")
            if require_type and not link_type:
                missing.append("type")
            if missing:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"link entry is missing {', '.join(missing)}: {link!r}",
                )
            if target_doc not in allowed:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"{doc.id!r} is not allowed to link to {target_doc!r}; "
                    f"declared parents are {list(doc.parents)}",
                )
            if require_type:
                if not isinstance(link_type, str):
                    raise ServiceError(
                        ErrorCode.VALIDATION,
                        f"link type must be a string: {link!r}",
                    )
                self._validate_xml_text(link_type, "link type")
                if (target_doc, target_id) in seen:
                    raise ServiceError(
                        ErrorCode.VALIDATION,
                        f"duplicate link in request: {target_doc}/{target_id}",
                    )
                seen.add((target_doc, target_id))
                if target_doc not in target_objects:
                    try:
                        target = self._load(target_doc).document
                        target_objects[target_doc] = target.objects
                    except ServiceError as e:
                        if e.code is not ErrorCode.NOT_FOUND:
                            raise
                        target_objects[target_doc] = {}
                if target_id not in target_objects[target_doc]:
                    raise ServiceError(
                        ErrorCode.VALIDATION,
                        f"link target {target_doc}/{target_id} does not exist",
                    )
            out.append((target_doc, target_id, link_type))
        return out

    @_recorded
    def create_object_full(
        self,
        doc_id: str,
        *,
        parent_id: str | None = None,
        description: str = "",
        attrs: dict[str, str] | None = None,
        links: list[dict] | None = None,
    ) -> CompositeWriteResult:
        """Create an object and populate attrs + outgoing links in one call.

        Pre-validates every attr and link before touching disk; on success
        returns the populated `RequirementObject` via `refreshed`. A follow-up
        step failing after the object was created (rare — pre-validation
        catches the common cases) raises `ServiceError(IO)` for a disk
        failure, else `ServiceError(VALIDATION)`, with a `partial` block in
        `details`.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        attrs = dict(attrs or {})
        links = list(links or [])

        doc = self._load(doc_id).document
        self._validate_composite_attrs(doc, attrs)
        normalized_links = self._validate_composite_links(
            doc, links, require_type=True
        )

        try:
            new_id = editor.create_object(
                folder, parent_id=parent_id, description=description
            )
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e

        affected_files: list[Path] = [
            folder / "objects" / f"{new_id}.xml",
            folder / f"{doc_id}.xml",
        ]
        attrs_set: list[str] = []
        links_added: list[tuple[str, str, str]] = []
        link_file = folder / "links" / f"{new_id}.xml"
        try:
            for name, value in attrs.items():
                editor.set_attribute(folder, new_id, name, value)
                attrs_set.append(name)
            for target_doc, target_id, link_type in normalized_links:
                editor.add_link(folder, new_id, target_doc, target_id, link_type)
                links_added.append((target_doc, target_id, link_type))
        except (EditorError, ValueError, OSError) as e:
            self._reload(doc_id)
            raise ServiceError(
                ErrorCode.IO if isinstance(e, OSError) else ErrorCode.VALIDATION,
                f"composite create succeeded for object {new_id!r} but "
                f"a follow-up step failed: {e}",
                details={
                    "partial": {
                        "created_id": new_id,
                        "attrs_set": list(attrs_set),
                        "links_added": [list(t) for t in links_added],
                        "failed_detail": str(e),
                    }
                },
            ) from e

        if links_added and link_file not in affected_files:
            affected_files.append(link_file)

        refreshed = self._reload(doc_id).document
        return CompositeWriteResult(
            obj_id=new_id,
            refreshed=refreshed,
            affected_files=tuple(affected_files),
            attrs_set=tuple(attrs_set),
            links_added=tuple(links_added),
            links_removed=(),
            description_changed=bool(description),
        )

    @_recorded
    def update_object(
        self,
        doc_id: str,
        obj_id: str,
        *,
        description: str | None = None,
        attrs: dict[str, str] | None = None,
        add_links: list[dict] | None = None,
        remove_links: list[dict] | None = None,
    ) -> CompositeWriteResult:
        """Update an object's description, attrs, and links in one call.

        Semantics:
          - `description=None` → don't touch; `description=""` → blank it.
          - `attrs={"foo": ""}` writes an empty value (matches the existing
            `remove_attr_option` convention). To remove an attribute entirely
            use `edit_attribute` workflows; this tool only sets values.
          - `remove_links` is processed before `add_links`, so the same target
            pair appearing in both nets to "add".
          - All-None call is a no-op (returns ok with `affected_files=[]`).
          - Attrs and links are validated before any write (an added link
            must not already exist unless also removed). A step failing
            mid-batch raises `ServiceError(IO)` for a disk failure, else
            `ServiceError(VALIDATION)`, with a `partial` block in `details`.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        attrs = dict(attrs or {})
        add_links = list(add_links or [])
        remove_links = list(remove_links or [])

        doc = self._load(doc_id).document
        if obj_id not in doc.objects:
            raise ServiceError(
                ErrorCode.NOT_FOUND,
                f"no object {obj_id!r} in document {doc_id!r}",
            )
        self._validate_composite_attrs(doc, attrs)
        normalized_add = self._validate_composite_links(
            doc, add_links, require_type=True
        )
        normalized_remove = self._validate_composite_links(
            doc, remove_links, require_type=False
        )
        existing = {(l.target_doc, l.target_id) for l in doc.links.get(obj_id, ())}
        existing -= {(td, ti) for td, ti, _ in normalized_remove}
        for target_doc, target_id, _ in normalized_add:
            if (target_doc, target_id) in existing:
                raise ServiceError(
                    ErrorCode.VALIDATION,
                    f"link already exists: {obj_id} -> {target_doc}/{target_id}",
                )

        affected_files: list[Path] = []
        obj_path = folder / "objects" / f"{obj_id}.xml"
        link_file = folder / "links" / f"{obj_id}.xml"
        description_changed = False
        attrs_set: list[str] = []
        links_added: list[tuple[str, str, str]] = []
        links_removed: list[tuple[str, str]] = []
        try:
            if description is not None:
                editor.set_description(folder, obj_id, description)
                description_changed = True
                if obj_path not in affected_files:
                    affected_files.append(obj_path)
            for target_doc, target_id, _ in normalized_remove:
                removed = editor.remove_link(
                    folder, obj_id, target_doc, target_id
                )
                if removed:
                    links_removed.append((target_doc, target_id))
                    if link_file not in affected_files:
                        affected_files.append(link_file)
            for target_doc, target_id, link_type in normalized_add:
                editor.add_link(folder, obj_id, target_doc, target_id, link_type)
                links_added.append((target_doc, target_id, link_type))
                if link_file not in affected_files:
                    affected_files.append(link_file)
            for name, value in attrs.items():
                editor.set_attribute(folder, obj_id, name, value)
                attrs_set.append(name)
                if obj_path not in affected_files:
                    affected_files.append(obj_path)
        except (EditorError, ValueError, OSError) as e:
            self._reload(doc_id)
            raise ServiceError(
                ErrorCode.IO if isinstance(e, OSError) else ErrorCode.VALIDATION,
                f"composite update failed mid-batch: {e}",
                details={
                    "partial": {
                        "description_changed": description_changed,
                        "attrs_set": list(attrs_set),
                        "links_added": [list(t) for t in links_added],
                        "links_removed": [list(t) for t in links_removed],
                        "failed_detail": str(e),
                    }
                },
            ) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e

        refreshed = self._reload(doc_id).document
        return CompositeWriteResult(
            obj_id=obj_id,
            refreshed=refreshed,
            affected_files=tuple(affected_files),
            attrs_set=tuple(attrs_set),
            links_added=tuple(links_added),
            links_removed=tuple(links_removed),
            description_changed=description_changed,
        )

    @_recorded
    def copy_object(
        self,
        doc_id: str,
        obj_id: str,
        *,
        parent_id: str | None = None,
        count: int = 1,
        copy_links: bool = True,
        copy_figure: bool = True,
    ) -> MutationResult[list[str]]:
        """Duplicate an object N times.

        Each copy gets a freshly-allocated id and lands as a sibling of the
        source (or under `parent_id` if given). Attributes are always copied;
        outgoing links and the attached figure (if any) are copied based on
        the flags. Returns the list of new ids in creation order.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        if count < 1:
            raise ServiceError(
                ErrorCode.VALIDATION, f"count must be >= 1, got {count}"
            )

        doc = self._load(doc_id).document
        if obj_id not in doc.objects:
            raise ServiceError(
                ErrorCode.NOT_FOUND,
                f"no object {obj_id!r} in document {doc_id!r}",
            )
        if parent_id is not None and parent_id not in doc.objects:
            raise ServiceError(
                ErrorCode.NOT_FOUND,
                f"parent {parent_id!r} not in document {doc_id!r}",
            )

        source = doc.objects[obj_id]
        source_links = doc.links.get(obj_id, ())
        source_figure_path = folder / "figures" / f"{obj_id}.svg"
        has_figure = source_figure_path.is_file()

        effective_parent = parent_id
        if effective_parent is None:
            _, effective_parent = _find_outline_parent(doc.outline, obj_id)

        new_ids: list[str] = []
        try:
            import shutil
            for _ in range(count):
                new_id = editor.create_object(
                    folder, parent_id=effective_parent, description=source.description
                )
                for name, value in source.attrs.items():
                    editor.set_attribute(folder, new_id, name, value)
                if copy_links:
                    for link in source_links:
                        editor.add_link(
                            folder, new_id,
                            link.target_doc, link.target_id, link.type,
                        )
                if copy_figure and has_figure:
                    dst = folder / "figures" / f"{new_id}.svg"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_figure_path, dst)
                new_ids.append(new_id)
        except EditorError as e:
            self._reload(doc_id)
            raise ServiceError(
                ErrorCode.IO,
                f"copy_object stopped after {len(new_ids)} successful copies: {e}",
                details={
                    "partial": {
                        "new_ids": list(new_ids),
                        "failed_detail": str(e),
                    }
                },
            ) from e
        except OSError as e:
            self._reload(doc_id)
            raise ServiceError(ErrorCode.IO, str(e)) from e

        refreshed = self._reload(doc_id).document
        return MutationResult(
            value=new_ids, refreshed=refreshed, affected_docs=(doc_id,)
        )

    @_recorded
    def bulk_set_attribute(
        self,
        doc_id: str,
        obj_ids: list[str],
        name: str,
        value: str,
    ) -> MutationResult[list[str]]:
        """Set the same attribute value on N objects in one call.

        Pre-validates the attr name + value (once, against the schema) and
        confirms every `obj_id` exists in the doc; failures raise before any
        file is written. Empty `obj_ids` is a no-op. Mid-batch I/O failure
        raises `ServiceError(IO)` with the partial progress in
        `details["partial"]`.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        ids = list(obj_ids)
        if not ids:
            refreshed = self._load(doc_id).document
            return MutationResult(value=[], refreshed=refreshed, affected_docs=())

        doc = self._load(doc_id).document
        self._validate_attr_pair(doc, name, value)
        missing = [oid for oid in ids if oid not in doc.objects]
        if missing:
            raise ServiceError(
                ErrorCode.NOT_FOUND,
                f"object(s) not in document {doc_id!r}: {missing}",
            )

        set_on: list[str] = []
        try:
            for obj_id in ids:
                editor.set_attribute(folder, obj_id, name, value)
                set_on.append(obj_id)
        except EditorError as e:
            self._reload(doc_id)
            raise ServiceError(
                ErrorCode.IO,
                f"bulk_set_attribute failed after {len(set_on)} write(s): {e}",
                details={
                    "partial": {
                        "set_on": list(set_on),
                        "failed_detail": str(e),
                    }
                },
            ) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e

        refreshed = self._reload(doc_id).document
        return MutationResult(
            value=set_on, refreshed=refreshed, affected_docs=(doc_id,)
        )

    def create_document(
        self,
        doc_id: str,
        *,
        title: str = "",
        parents: list[str] | None = None,
        attr_schema: list[dict] | None = None,
    ) -> MutationResult[str]:
        self._require_write()
        root = self._require_repo_root()
        try:
            editor.create_document(
                root, doc_id, title=title, parents=parents, attr_schema=attr_schema
            )
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=doc_id, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def set_attribute(
        self, doc_id: str, obj_id: str, name: str, value: str
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_attribute(folder, obj_id, name, value)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def set_description(
        self, doc_id: str, obj_id: str, text: str
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_description(folder, obj_id, text)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def set_figure(
        self, doc_id: str, obj_id: str, src_path: str | Path
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_figure(folder, obj_id, Path(src_path))
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def clear_figure(
        self, doc_id: str, obj_id: str
    ) -> MutationResult[bool]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            removed = editor.clear_figure(folder, obj_id)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        affected = (doc_id,) if removed else ()
        return MutationResult(value=removed, refreshed=refreshed, affected_docs=affected)

    @_recorded
    def set_table(
        self, doc_id: str, obj_id: str, table: Table
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_table(folder, obj_id, table)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def clear_table(
        self, doc_id: str, obj_id: str
    ) -> MutationResult[bool]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            removed = editor.clear_table(folder, obj_id)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        affected = (doc_id,) if removed else ()
        return MutationResult(value=removed, refreshed=refreshed, affected_docs=affected)

    @_recorded
    def set_metadata(
        self, doc_id: str, field: str, value: str
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_metadata(folder, field, value)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def add_parent(self, doc_id: str, parent_doc: str) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.add_parent(folder, parent_doc)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def remove_parent(self, doc_id: str, parent_doc: str) -> MutationResult[bool]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            removed = editor.remove_parent(folder, parent_doc)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        affected = (doc_id,) if removed else ()
        return MutationResult(value=removed, refreshed=refreshed, affected_docs=affected)

    @_recorded
    def add_attr_option(
        self, doc_id: str, name: str, option: str
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.add_attr_option(folder, name, option)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def add_attribute(
        self,
        doc_id: str,
        name: str,
        kind: str,
        required: bool,
        options: tuple[str, ...] = (),
        label: str = "",
        default_value: str | None = None,
    ) -> MutationResult[list[str]]:
        """Declare a new attribute in the document schema.

        Returns a `MutationResult` whose `value` is the list of object ids
        that were backfilled with `default_value` (empty when
        `default_value` is None). Pre-validates `default_value` against the
        proposed attr's kind so a typo doesn't leave the schema in a half-
        baked state.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)

        if default_value is not None:
            from ..schema import Attribute as _Attribute
            proposed = _Attribute(
                name=name,
                label=label,
                kind=kind,
                required=required,
                options=options,
            )
            self._validate_value_for_attr(proposed, default_value)

        try:
            editor.add_attribute(folder, name, kind, required, options, label)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e

        backfilled: list[str] = []
        if default_value is not None:
            fresh = self._reload(doc_id).document
            for obj_id in list(fresh.objects.keys()):
                if fresh.objects[obj_id].attrs.get(name, "") != "":
                    continue
                try:
                    editor.set_attribute(folder, obj_id, name, default_value)
                except EditorError as e:
                    self._reload(doc_id)
                    raise ServiceError(
                        ErrorCode.IO,
                        f"backfill of {name!r} failed on {obj_id!r}: {e}",
                        details={
                            "partial": {
                                "backfilled": list(backfilled),
                                "failed_at": obj_id,
                                "failed_detail": str(e),
                            }
                        },
                    ) from e
                backfilled.append(obj_id)

        refreshed = self._reload(doc_id).document
        return MutationResult(
            value=backfilled, refreshed=refreshed, affected_docs=(doc_id,)
        )

    @_recorded
    def delete_attribute(
        self, doc_id: str, name: str
    ) -> MutationResult[list[Path]]:
        """`value` lists every file the cascade wrote (manifest first)."""
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            written = editor.delete_attribute(folder, name)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=written, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def change_attribute_required(
        self, doc_id: str, name: str, required: bool
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.change_attribute_required(folder, name, required)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def create_view(
        self, doc_id: str, name: str, visible_attrs: tuple[str, ...] | list[str]
    ) -> MutationResult[str]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            slug = editor.create_view(folder, name, visible_attrs)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=slug, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def delete_view(self, doc_id: str, slug: str) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.delete_view(folder, slug)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def rename_view(
        self, doc_id: str, slug: str, new_name: str
    ) -> MutationResult[str]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            new_slug = editor.rename_view(folder, slug, new_name)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=new_slug, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def set_view_visible(
        self,
        doc_id: str,
        slug: str,
        visible_attrs: tuple[str, ...] | list[str],
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_view_visible(folder, slug, visible_attrs)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    # --- filters --------------------------------------------------------

    @_recorded
    def create_filter(
        self,
        doc_id: str,
        name: str,
        combinator: str = "all",
        keep_ancestors: bool = True,
        rules: tuple[FilterRule, ...] | list[FilterRule] = (),
    ) -> MutationResult[str]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            slug = editor.create_filter(
                folder, name, combinator, keep_ancestors, rules
            )
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=slug, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def delete_filter(self, doc_id: str, slug: str) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.delete_filter(folder, slug)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def rename_filter(
        self, doc_id: str, slug: str, new_name: str
    ) -> MutationResult[str]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            new_slug = editor.rename_filter(folder, slug, new_name)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(
            value=new_slug, refreshed=refreshed, affected_docs=(doc_id,)
        )

    @_recorded
    def set_filter_rules(
        self,
        doc_id: str,
        slug: str,
        combinator: str,
        keep_ancestors: bool,
        rules: tuple[FilterRule, ...] | list[FilterRule],
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_filter_rules(folder, slug, combinator, keep_ancestors, rules)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def remove_attr_option(
        self, doc_id: str, name: str, option: str
    ) -> MutationResult[list[Path]]:
        """`value` lists every file written (manifest first)."""
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            written = editor.remove_attr_option(folder, name, option)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=written, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def move_object(
        self,
        doc_id: str,
        obj_id: str,
        *,
        parent_id: str | None = None,
        to_top: bool = False,
        before: str | None = None,
        after: str | None = None,
    ) -> MutationResult[None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.move_object(
                folder, obj_id,
                parent_id=parent_id, to_top=to_top, before=before, after=after,
            )
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=None, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def set_outline(
        self, doc_id: str, ordering: list[dict]
    ) -> MutationResult[int]:
        """Replace the manifest outline with `ordering` in one pass.

        See `editor.set_outline` for the shape and validation rules.
        Returns the number of objects in the new outline.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            editor.set_outline(folder, ordering)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(
            value=len(refreshed.objects),
            refreshed=refreshed,
            affected_docs=(doc_id,),
        )

    @_recorded
    def add_link(
        self,
        doc_id: str,
        source_id: str,
        target_doc: str,
        target_id: str,
        link_type: str,
    ) -> MutationResult[str | None]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            ack = editor.add_link(folder, source_id, target_doc, target_id, link_type)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        return MutationResult(value=ack, refreshed=refreshed, affected_docs=(doc_id,))

    @_recorded
    def remove_link(
        self, doc_id: str, source_id: str, target_doc: str, target_id: str
    ) -> MutationResult[bool]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            removed = editor.remove_link(folder, source_id, target_doc, target_id)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e
        refreshed = self._reload(doc_id).document
        affected = (doc_id,) if removed else ()
        return MutationResult(value=removed, refreshed=refreshed, affected_docs=affected)

    @_recorded
    def delete_object(
        self, doc_id: str, obj_id: str, *, force: bool = False
    ) -> DeleteResult:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            report = editor.delete_object(folder, obj_id, force=force)
        except EditorError as e:
            raise _classify_editor_error(e) from e
        except LoaderError as e:
            raise ServiceError(ErrorCode.LOAD, str(e)) from e
        except (ValueError, OSError) as e:
            raise _classify_write_error(e) from e

        affected = self._affected_docs_from_delete(doc_id, report)
        refreshed: dict[str, Document] = {}
        for did in affected:
            refreshed[did] = self._reload(did).document
        return DeleteResult(report=report, affected_docs=affected, refreshed_docs=refreshed)

    @_recorded
    def ack_all(
        self, doc_id: str, statuses: list[LinkStatus]
    ) -> MutationResult[list[LinkStatus]]:
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            rewritten = suspects_ack_all(folder, statuses)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        except OSError as e:
            raise ServiceError(ErrorCode.IO, str(e)) from e
        refreshed = self._reload(doc_id).document
        affected = (doc_id,) if rewritten else ()
        return MutationResult(
            value=rewritten, refreshed=refreshed, affected_docs=affected
        )

    # --- change listeners + undo ----------------------------------------

    def add_change_listener(self, listener: ChangeListener) -> None:
        """Call `listener(doc_id, label, changes)` after every write method
        that changed files: `doc_id` is the method's document, `label` its
        name in words ("set attribute"), `changes` the `editor.Journal` with
        each file's bytes before and after. The GUI's undo builds on it."""
        self._change_listeners.append(listener)

    def remove_change_listener(self, listener: ChangeListener) -> None:
        with contextlib.suppress(ValueError):
            self._change_listeners.remove(listener)

    def undo_changes(self, changes: editor.Journal) -> tuple[str, ...]:
        """Put the files of a recorded change back as they were before it.

        Returns the ids of the documents whose files it rewrote (their cache
        entries are dropped). Raises `ServiceError(CONFLICT)`, changing
        nothing, if any of the files changed since.
        """
        return self._replay(changes, undo=True)

    def redo_changes(self, changes: editor.Journal) -> tuple[str, ...]:
        """Apply an undone change again; see `undo_changes`."""
        return self._replay(changes, undo=False)

    def _replay(self, changes: editor.Journal, *, undo: bool) -> tuple[str, ...]:
        self._require_write()
        try:
            if undo:
                changes.revert()
            else:
                changes.reapply()
        except EditorError as e:
            raise ServiceError(ErrorCode.CONFLICT, str(e)) from e
        affected = self._docs_of(changes.paths)
        if self._cache is not None:
            self._cache.invalidate(list(affected))
            # Restored or removed objects change other documents' scans.
            self._cache.invalidate_suspects()
        return affected

    # --- git commit ----------------------------------------------------

    def commit(
        self, message: str, paths: Iterable[str] | None = None
    ) -> CommitResult:
        """Commit `paths`; with None, every uncommitted change inside the
        document folders (never other files in the git work tree). A
        document folder deleted or renamed on disk still counts while HEAD
        has it, so its deletions are committed too."""
        self._require_write()
        root = self._require_repo_root()
        try:
            if paths is None:
                repo, git_root = discover_repo(root)
                names = doc_folder_names(root) | head_doc_folder_names(
                    repo, root.relative_to(git_root).as_posix()
                )
                # One status scan, filtered to the document folders.
                prefixes = tuple(
                    (root / name).relative_to(git_root).as_posix() + "/"
                    for name in names
                )
                paths = [
                    p for p in dirty_paths_under(repo, "") if p.startswith(prefixes)
                ]
            short_hash = git_commit(root, message, paths)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        paths_out: tuple[Path, ...] = tuple(Path(p) for p in paths) if paths else ()
        return CommitResult(short_hash=short_hash, paths=paths_out)

    def has_uncommitted_changes(self, doc_id: str) -> bool:
        """True iff `doc_id`'s folder has any uncommitted paths (changed in
        the work tree, or staged by another git client).

        Cheap enough to call from an action-enable check on every selection
        change (one `git status` underneath). Returns False when the doc
        folder isn't inside a git repo.
        """
        try:
            folder = self._doc_folder(doc_id)
        except ServiceError:
            return False
        try:
            repo, root = discover_repo(folder)
        except GitError:
            return False
        folder_rel = folder.resolve().relative_to(root).as_posix()
        return bool(dirty_paths_under(repo, folder_rel))

    def discard_document_changes(self, doc_id: str) -> tuple[str, ...]:
        """Throw away every uncommitted change inside `doc_id`'s folder.

        Modified and deleted files go back to HEAD, staged changes are
        unstaged, and files created since the last commit (new objects,
        links, figures) are removed. Nothing outside the folder is touched.
        Returns the repo-relative paths that changed (empty when the folder
        was clean).

        Raises `ServiceError(GIT)` when the folder isn't in a git repo, a
        merge is in progress, or the document has never been committed
        (there is nothing to restore it to).
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            repo, root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        folder_rel = folder.resolve().relative_to(root).as_posix()
        try:
            return tuple(discard_paths(repo, [folder_rel]))
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        except OSError as e:
            raise ServiceError(ErrorCode.IO, str(e)) from e
        finally:
            if self._cache is not None:
                self._cache.invalidate([doc_id])
                # Links from other documents into this folder may have changed
                # state (a restored or removed target file), at the same HEAD.
                self._cache.invalidate_suspects()
                try:
                    self._load(doc_id)
                except ServiceError:
                    # Have the next repo-wide load report why it fails.
                    self._cache.invalidate_all()

    def prepare_document_commit(self, doc_id: str) -> DocCommitPreview:
        """Compute paths, affected req-ids, default message, and parent warnings.

        Side-effect free — the dialog uses this to populate fields before the
        user clicks OK. Raises `ServiceError(GIT)` only when the doc folder
        isn't inside a git repo; an empty paths tuple is a valid result for
        a clean working tree.
        """
        folder = self._doc_folder(doc_id)
        try:
            repo, root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        folder_rel = folder.resolve().relative_to(root).as_posix()
        paths = tuple(dirty_paths_under(repo, folder_rel))

        prefix_objects = f"{folder_rel}/objects/"
        prefix_links = f"{folder_rel}/links/"
        prefix_figures = f"{folder_rel}/figures/"
        affected: set[str] = set()
        for p in paths:
            if p.startswith(prefix_objects) or p.startswith(prefix_links):
                stem = p.rsplit("/", 1)[-1]
                if stem.endswith(".xml"):
                    stem = stem[:-4]
                if stem:
                    affected.add(stem)
            elif p.startswith(prefix_figures) and p.endswith(".svg"):
                # A figure-only edit revises its object (it flips links to
                # SUSPECT), so it counts like an edit to objects/<id>.xml.
                stem = p.rsplit("/", 1)[-1][:-4]
                if stem:
                    affected.add(stem)
        affected_sorted = tuple(sorted(affected))
        if affected_sorted:
            tail = ", ".join(affected_sorted)
            default_message = f"Edited {doc_id}. Affected requirements: {tail}."
        else:
            default_message = f"Edited {doc_id}. No affected requirements."

        dirty_parents = tuple(self._dirty_parents(doc_id, repo))

        suspect_links: tuple[LinkStatus, ...] = ()
        try:
            scan = self.scan_suspects(doc_id)
            suspect_links = tuple(
                s for s in scan.get(doc_id, ()) if s.state is not LinkState.OK
            )
        except ServiceError:
            # Suspect scanning isn't critical for the commit dialog — surface
            # an empty list rather than blocking the commit on a scan failure.
            suspect_links = ()

        try:
            doc = self.get_document(doc_id)
            current_author = doc.metadata.author
            current_date = doc.metadata.date
        except ServiceError:
            current_author = ""
            current_date = ""
        current_issue = git_current_issue(repo, doc_id)
        # A plain commit cuts no tag: the issue is only bumped when the user
        # changes it ("" for a document without a tag yet). The date stays
        # the manifest's own, since an auto-written date would make every
        # commit touch the same line, a merge hot spot.
        prefill_issue = current_issue
        prefill_author = current_author or (git_user_name(repo) or "")
        prefill_date = current_date

        return DocCommitPreview(
            doc_id=doc_id,
            paths=paths,
            affected_req_ids=affected_sorted,
            default_message=default_message,
            dirty_parents=dirty_parents,
            suspect_links=suspect_links,
            current_issue=current_issue,
            current_author=current_author,
            current_date=current_date,
            prefill_issue=prefill_issue,
            prefill_author=prefill_author,
            prefill_date=prefill_date,
            existing_tags=tag_names(repo),
            upstream_warnings=tuple(self._upstream_warnings(doc_id, repo)),
        )

    def _upstream_warnings(self, doc_id: str, repo) -> list[str]:
        """Open points in the documents `doc_id` links up to, transitively.

        For the commit dialog's Tag field: a tag marks every document as last
        committed, so the dialog warns (it never blocks) about the documents
        up the link chain that have uncommitted changes, suspect or broken
        links, validation errors, or that don't load. Documents beside or
        below `doc_id` aren't checked. One line per problem, e.g.
        "SYS-SyRS — uncommitted changes", documents sorted by id. Quietly
        returns [] if the repo can't be read.
        """
        load_errors: list[DocLoadError] = []
        try:
            docs = self.get_all_documents(load_errors=load_errors)
            root = self._require_repo_root()
        except ServiceError:
            return []
        failed = {e.doc_id for e in load_errors}
        # `seen` keeps a link cycle (and `doc_id` itself) out of the walk.
        seen = {doc_id}
        todo = [doc_id]
        while todo:
            for pid in repo_parent_doc_ids(docs, todo.pop()):
                if pid not in seen:
                    seen.add(pid)
                    todo.append(pid)
        out: list[str] = []
        for pid in sorted(seen - {doc_id}):
            loaded = docs.get(pid)
            if loaded is None:
                # A link to a document that doesn't exist is a broken link,
                # reported by the scan of the document holding it.
                if pid in failed:
                    out.append(f"{pid} — does not load")
                continue
            try:
                rel = loaded.folder.resolve().relative_to(root).as_posix()
            except ValueError:
                rel = None
            if rel and dirty_paths_under(repo, rel):
                out.append(f"{pid} — uncommitted changes")
            try:
                suspect = sum(
                    1
                    for s in self.scan_suspects(pid).get(pid, ())
                    if s.state is not LinkState.OK
                )
            except ServiceError:
                suspect = 0
            if suspect:
                out.append(f"{pid} — {suspect} suspect or broken link(s)")
            errors = len(validate(loaded.document))
            if errors:
                out.append(f"{pid} — {errors} validation error(s)")
        return out

    def _dirty_parents(self, doc_id: str, repo) -> list[str]:
        """Sorted parent doc ids that currently have working-tree changes.

        A parent here is a doc this one links up to (see `repo.parent_doc_ids`).
        We only flag direct parents — transitive ancestors aren't considered.
        Quietly returns [] if the doc isn't loaded or the repo can't be read.
        """
        try:
            docs = self.get_all_documents()
        except ServiceError:
            return []
        parents = repo_parent_doc_ids(docs, doc_id)
        if not parents:
            return []
        out: list[str] = []
        for parent_id in sorted(parents):
            parent_loaded = docs.get(parent_id)
            if parent_loaded is None:
                continue
            try:
                parent_rel = parent_loaded.folder.resolve().relative_to(
                    self._require_repo_root()
                ).as_posix()
            except (ValueError, ServiceError):
                continue
            if dirty_paths_under(repo, parent_rel):
                out.append(parent_id)
        return out

    def commit_document(
        self,
        doc_id: str,
        message: str,
        *,
        new_issue: str | None = None,
        tag: str | None = None,
        tag_message: str = "",
    ) -> CommitResult:
        """Commit only the dirty files inside `doc_id`'s folder.

        Re-scopes paths at commit time (rather than trusting a stale preview)
        so any edit landed between the dialog opening and the user clicking
        OK is included if it's still inside the document folder. Raises
        `ServiceError(VALIDATION)` if the working tree under the folder is
        clean (no-op commit).

        If `new_issue` is provided and differs from the current issue tag,
        create `<doc_id>-issue-<new_issue>` at the freshly-landed commit.
        An empty `new_issue` (the dialog's prefill for a document without a
        tag) means no tag. Tag-creation failures populate `tag_error` on the
        result rather than raising — the commit always stands.

        A non-empty `tag` (the dialog's Tag field) also tags the new commit
        with that name, annotated with `tag_message`; a failure lands in
        `repo_tag_error` the same way.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            repo, root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        folder_rel = folder.resolve().relative_to(root).as_posix()
        paths = dirty_paths_under(repo, folder_rel)
        if not paths:
            raise ServiceError(
                ErrorCode.VALIDATION,
                f"document {doc_id!r} has no uncommitted changes",
            )

        pre_commit_issue = git_current_issue(repo, doc_id)

        try:
            short_hash = git_commit(root, message, paths)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e

        tag_name: str | None = None
        tag_error: str | None = None
        if new_issue and new_issue != pre_commit_issue:
            try:
                tag_name = create_issue_tag(
                    repo, doc_id, new_issue, repo.head.target
                )
            except GitError as e:
                tag_error = str(e)

        repo_tag: str | None = None
        repo_tag_error: str | None = None
        if tag:
            try:
                repo_tag = create_tag(repo, tag, repo.head.target, tag_message)
            except GitError as e:
                repo_tag_error = str(e)

        return CommitResult(
            short_hash=short_hash,
            paths=tuple(Path(p) for p in paths),
            tag_name=tag_name,
            tag_error=tag_error,
            repo_tag=repo_tag,
            repo_tag_error=repo_tag_error,
        )

    def tag_issue(self, doc_id: str, issue: str) -> str:
        """Create `<doc_id>-issue-<issue>` at HEAD. Returns the tag name.

        Used by the MCP `tag_issue` tool when an agent has committed via
        another path and wants to bump the issue separately. Raises
        `ServiceError(GIT)` on tag conflicts or if the doc folder isn't in
        a git repo; `ServiceError(VALIDATION)` for malformed issues.
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            repo, _root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        try:
            head_oid = repo.head.target
        except (Exception,) as e:
            raise ServiceError(
                ErrorCode.GIT, f"HEAD not resolvable: {e}"
            ) from e
        try:
            return create_issue_tag(repo, doc_id, issue, head_oid)
        except GitError as e:
            # Format errors are validation; conflicts are git state.
            msg = str(e)
            if "must be a dot-separated" in msg or "must be higher" in msg:
                raise ServiceError(ErrorCode.VALIDATION, msg) from e
            raise ServiceError(ErrorCode.GIT, msg) from e

    def tag_head(self, doc_id: str, name: str, message: str = "") -> str:
        """Tag HEAD `name`, annotated with `message`. Returns the name.

        The commit dialog's Tag on a document with nothing to commit: it
        marks the current commit, so every document as last committed.
        Raises `ServiceError(VALIDATION)` for a name `tag_name_error`
        refuses, and `ServiceError(GIT)` when the doc folder isn't in a git
        repo, HEAD is unborn, or tagging fails (e.g. no git identity).
        """
        self._require_write()
        folder = self._doc_folder(doc_id)
        try:
            repo, _root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        error = tag_name_error(name, tag_names(repo))
        if error:
            raise ServiceError(ErrorCode.VALIDATION, error)
        try:
            head_oid = repo.head.target
        except (Exception,) as e:
            raise ServiceError(
                ErrorCode.GIT, f"HEAD not resolvable: {e}"
            ) from e
        try:
            return create_tag(repo, name, head_oid, message)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
