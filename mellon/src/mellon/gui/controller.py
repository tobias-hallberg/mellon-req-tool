"""Single source of truth for the currently-open repo + selection.

`RepoController` wraps a `DocumentService` (cache enabled) and emits Qt
signals when selection or underlying state changes. Every pane listens to it;
no pane talks to another directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pygit2
from lxml import etree
from PySide6.QtCore import QObject, Signal

from .. import git_ops
from ..git_ops import GitError, discover_repo, dirty_paths_under, is_side_file
from ..repo import DocLoadError, LoadedDoc, folders_fingerprint
from ..schema import FilterRule, Link, RequirementObject, Document
from ..service import DocCommitPreview, DocumentService, ErrorCode, ServiceError
from ..suspects import LinkState, LinkStatus


def describe_load_error(err: DocLoadError) -> str:
    """`file:line` (or the document folder) over the loader's message.

    For tooltips and message boxes about a document that failed to load.
    """
    where = str(err.file if err.file is not None else err.folder)
    if err.line is not None:
        where += f":{err.line}"
    message = err.message
    # Loader messages usually lead with the same path; don't show it twice.
    if err.file is not None and message.startswith(f"{err.file}: "):
        message = message[len(f"{err.file}: "):]
    return f"{where}\n{message}"


def _repo_root_advice(path: Path) -> str:
    """What to do about a folder `git_ops.check_repo_root` refused.

    Empty when its message already names the remedy (a shallow clone, or a
    repository git cannot open).
    """
    try:
        _repo, git_root = discover_repo(path)
    except GitError:
        return (
            "Mellon opens the root folder of a git clone: the folder that "
            "contains .git. Open that folder instead, or clone the repository "
            "with your git client."
        )
    except pygit2.GitError:
        return ""
    if git_root != path:
        # A subfolder of a clone; `git init` here would nest a second repo.
        return f"Open {git_root} instead."
    return ""


@dataclass(frozen=True)
class ObjectView:
    """Fully-denormalized view of one object for the inspector / grid."""

    doc_id: str
    obj_id: str
    object: object  # RequirementObject
    outline_path: tuple[str, ...]
    outgoing: tuple[LinkStatus, ...]
    incoming: tuple[tuple[str, str, str], ...]  # (source_doc, source_id, link_type)


@dataclass(frozen=True)
class ObjectAtCommit:
    """Read-only snapshot of one object as it existed at a historical commit."""

    doc_id: str
    obj_id: str
    short_hash: str
    object: RequirementObject
    outline_path: tuple[str, ...]
    outgoing: tuple[Link, ...]


class RepoController(QObject):
    """Load a repo, track the current doc + object, notify listeners.

    Signals:
        repo_loaded(root): new repo root, `docs()` is now populated.
        document_selected(doc_id): active document changed; panes rebind.
        object_selected(doc_id, obj_id): selection changed within a doc.
        repo_load_failed(root, message): open_repo hit an error.
        edit_failed(doc_id, obj_id, field, message): an edit didn't land.
        edit_succeeded(doc_id, obj_id, field): an edit did land.
    """

    repo_loaded = Signal(str)
    document_selected = Signal(str)
    object_selected = Signal(str, str)
    repo_load_failed = Signal(str, str)
    edit_failed = Signal(str, str, str, str)
    edit_succeeded = Signal(str, str, str)
    committed = Signal(str, str)  # (doc_id, short_hash)
    views_changed = Signal(str)   # doc_id whose views set just changed
    filters_changed = Signal(str) # doc_id whose filters set just changed

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service: DocumentService | None = None
        self._current_doc: str | None = None
        self._current_obj: str | None = None
        # On-disk fingerprint of the current doc's folder (+ parent-doc
        # folders) taken after the last load/reload. `is_doc_stale` compares
        # against it to detect writes made by another process.
        self._disk_snapshot: frozenset[tuple[str, int, int]] | None = None
        # View/filter writes don't go through reload_document, so re-snapshot
        # on their signals to avoid a false "changed on disk" afterwards.
        self.views_changed.connect(lambda *_: self.refresh_disk_snapshot())
        self.filters_changed.connect(lambda *_: self.refresh_disk_snapshot())

    # --- service access -------------------------------------------------

    @property
    def service(self) -> DocumentService | None:
        """Active service (cache-enabled). None when no repo is open."""
        return self._service

    def require_service(self) -> DocumentService:
        if self._service is None:
            raise RuntimeError("no repo is open")
        return self._service

    # --- loading --------------------------------------------------------

    def open_repo(self, root: str | Path) -> bool:
        """Load every document under `root`. Returns True on success.

        Refused (with `repo_load_failed`) when `root` is not the root of a
        full git clone: commits, history and suspect links all depend on it.
        Such a refusal leaves the repo that is already open (if any) open; a
        failed load closes it.
        """
        path = Path(root).resolve()
        # A missing folder is reported by the load below ("cannot read repo
        # root"), which says more than "not a git repository" would.
        if path.is_dir():
            problem = git_ops.check_repo_root(path)
            if problem is not None:
                advice = _repo_root_advice(path)
                self.repo_load_failed.emit(
                    str(path), f"{problem}." + (f"\n\n{advice}" if advice else "")
                )
                return False
        service = DocumentService(path, cache=True)
        try:
            service.get_all_documents()
        except ServiceError as e:
            self._service = None
            self.repo_load_failed.emit(str(path), str(e))
            return False

        self._service = service
        self._current_doc = None
        self._current_obj = None
        self.repo_loaded.emit(str(path))

        docs = service.get_all_documents()
        if docs:
            first_doc = next(iter(sorted(docs)))
            self.select_document(first_doc)
        return True

    # --- accessors ------------------------------------------------------

    @property
    def root(self) -> Path | None:
        return self._service.repo_root if self._service is not None else None

    def docs(self) -> dict[str, LoadedDoc]:
        if self._service is None:
            return {}
        try:
            return self._service.get_all_documents()
        except ServiceError:
            return {}

    def load_errors(self) -> list[DocLoadError]:
        """Documents under the root that failed to load, from the same
        (cached) load as `docs()`. Refreshed by `cache.invalidate_all()`."""
        if self._service is None:
            return []
        errors: list[DocLoadError] = []
        try:
            self._service.get_all_documents(load_errors=errors)
        except ServiceError:
            return []
        return errors

    def load_error(self, doc_id: str) -> DocLoadError | None:
        return next((e for e in self.load_errors() if e.doc_id == doc_id), None)

    def current_doc_id(self) -> str | None:
        return self._current_doc

    def current_obj_id(self) -> str | None:
        return self._current_obj

    def document(self, doc_id: str) -> Document:
        return self.require_service().get_document(doc_id)

    def folder(self, doc_id: str) -> Path:
        return self.require_service().get_loaded_document(doc_id).folder

    # --- selection ------------------------------------------------------

    def select_document(self, doc_id: str) -> None:
        if self._service is None:
            return
        try:
            doc = self._service.get_document(doc_id)
        except ServiceError:
            return
        if self._current_doc == doc_id:
            return
        self._current_doc = doc_id
        self._current_obj = None
        self.document_selected.emit(doc_id)
        self.refresh_disk_snapshot()
        outline_ids = doc.iter_outline_ids()
        if outline_ids:
            self.select_object(doc_id, outline_ids[0])

    def reload_document(self, doc_id: str) -> None:
        """Invalidate the cached copy of `doc_id` and tell listeners to rebuild.

        Safe to call after any service mutation — the service has already
        refreshed its own cache, so this just drops+re-reads defensively and
        fires the UI-facing signals. Preserves the current selection when the
        object still exists post-reload; when it is gone (deleted, discarded,
        removed on disk) the first object is selected, as `select_document`
        does, so no pane keeps showing it.
        """
        if self._service is None:
            return
        if self._service.cache is not None:
            self._service.cache.invalidate([doc_id])
        try:
            doc = self._service.get_document(doc_id)
        except ServiceError:
            return
        self.document_selected.emit(doc_id)
        self.refresh_disk_snapshot()
        if self._current_doc == doc_id and self._current_obj is not None:
            if self._current_obj in doc.objects:
                self.object_selected.emit(doc_id, self._current_obj)
                return
            self._current_obj = None
            first = next(
                (oid for oid in doc.iter_outline_ids() if oid in doc.objects), None
            )
            if first is not None:
                self.select_object(doc_id, first)

    # --- disk freshness ---------------------------------------------------

    def _watched_folders(self) -> list[Path]:
        """Folders whose on-disk state the current document depends on.

        The doc's own folder plus the folders of its declared parent
        documents — a parent edit is what flips this doc's links to suspect,
        so the review workflow needs to notice it too. Deliberately not the
        whole repo: sibling documents can't affect this window's display.
        """
        if self._service is None or self._current_doc is None:
            return []
        try:
            loaded = self._service.get_loaded_document(self._current_doc)
        except ServiceError:
            return []
        folders = [loaded.folder]
        docs = self.docs()
        for parent_id in loaded.document.parents:
            parent = docs.get(parent_id)
            if parent is not None:
                folders.append(parent.folder)
        return folders

    def refresh_disk_snapshot(self) -> None:
        """Re-fingerprint the watched folders; future edits diff against this."""
        folders = self._watched_folders()
        self._disk_snapshot = folders_fingerprint(folders) if folders else None

    def is_doc_stale(self) -> bool:
        """True when the watched folders changed on disk since the snapshot.

        Catches writes by other processes (MCP server, another editor) and by
        sibling editor windows — anything that didn't go through this
        controller. False when no snapshot exists (no doc selected).
        """
        if self._disk_snapshot is None:
            return False
        return folders_fingerprint(self._watched_folders()) != self._disk_snapshot

    def changed_figures(self) -> frozenset[str] | None:
        """Disk check that lets in-place figure edits through.

        Returns None when the watched folders changed in a way that makes
        the cached document stale — what `is_doc_stale` reports. Otherwise
        returns the ids whose existing `figures/<id>.svg` was rewritten since
        the snapshot (typically saved by draw.io; empty when nothing
        changed), and moves the snapshot past those writes.

        Figure contents are never cached — the grid paints them from disk —
        so such an edit can't be overwritten by stale text and needn't block
        editing. A figure added or removed, any other file, or anything in a
        parent document's folder still counts as stale. draw.io's `.$*`
        backup and draft files beside a figure are ignored.
        """
        if self._disk_snapshot is None:
            return frozenset()
        folders = self._watched_folders()
        if not folders:
            return frozenset()
        new = folders_fingerprint(folders)
        changed = new ^ self._disk_snapshot
        if not changed:
            return frozenset()
        # Trailing "/" so a sibling folder sharing a name prefix never matches.
        prefix = f"{folders[0].name}/figures/"
        old_keys = {key for key, _, _ in self._disk_snapshot}
        new_keys = {key for key, _, _ in new}
        ids: set[str] = set()
        for key, _, _ in changed:
            name = key[len(prefix):] if key.startswith(prefix) else None
            if name is None or "/" in name:
                return None
            if is_side_file(name):
                continue
            if not name.endswith(".svg") or key not in old_keys or key not in new_keys:
                return None
            ids.add(name[: -len(".svg")])
        self._disk_snapshot = new
        return frozenset(ids)

    def select_object(self, doc_id: str, obj_id: str) -> None:
        if self._service is None:
            return
        try:
            doc = self._service.get_document(doc_id)
        except ServiceError:
            return
        if obj_id not in doc.objects:
            return
        if doc_id != self._current_doc:
            self._current_doc = doc_id
            self.document_selected.emit(doc_id)
        self._current_obj = obj_id
        self.object_selected.emit(doc_id, obj_id)

    # --- outline navigation ---------------------------------------------

    def outline_depth(self, doc_id: str, obj_id: str) -> int:
        path = self.document(doc_id).outline_path(obj_id)
        return len(path) - 1 if path else -1

    def outline_parent_id(self, doc_id: str, obj_id: str) -> str | None:
        path = self.document(doc_id).outline_path(obj_id)
        if path is None or len(path) < 2:
            return None
        return path[-2]

    def outline_siblings(self, doc_id: str, obj_id: str) -> list[str]:
        doc = self.document(doc_id)
        parent = self.outline_parent_id(doc_id, obj_id)

        def children_of(nodes):
            for n in nodes:
                if n.ref_id == parent:
                    return list(n.children)
                found = children_of(n.children)
                if found is not None:
                    return found
            return None

        if parent is None:
            nodes = list(doc.outline)
        else:
            nodes = children_of(doc.outline) or []
        return [n.ref_id for n in nodes]

    def outline_previous_sibling(self, doc_id: str, obj_id: str) -> str | None:
        sibs = self.outline_siblings(doc_id, obj_id)
        try:
            i = sibs.index(obj_id)
        except ValueError:
            return None
        return sibs[i - 1] if i > 0 else None

    def outline_next_sibling(self, doc_id: str, obj_id: str) -> str | None:
        sibs = self.outline_siblings(doc_id, obj_id)
        try:
            i = sibs.index(obj_id)
        except ValueError:
            return None
        return sibs[i + 1] if i + 1 < len(sibs) else None

    def outline_has_children(self, doc_id: str, obj_id: str) -> bool:
        doc = self.document(doc_id)

        def walk(nodes):
            for n in nodes:
                if n.ref_id == obj_id:
                    return bool(n.children)
                found = walk(n.children)
                if found is not None:
                    return found
            return None

        return bool(walk(doc.outline))

    # --- suspect scans --------------------------------------------------

    def suspect_statuses(self, doc_id: str) -> list[LinkStatus]:
        """All non-OK statuses for `doc_id`. Cached via the service."""
        if self._service is None or doc_id not in self.docs():
            return []
        try:
            scoped = self._service.scan_suspects(doc_id)
        except ServiceError:
            return []
        return [s for s in scoped[doc_id] if s.state is not LinkState.OK]

    def suspect_count(self, doc_id: str) -> int:
        return len(self.suspect_statuses(doc_id))

    def object_worst_state(self, doc_id: str, obj_id: str) -> str:
        statuses = self.suspect_statuses(doc_id)
        my = [s for s in statuses if s.source_id == obj_id]
        if any(s.state is LinkState.BROKEN for s in my):
            return "broken"
        if any(s.state is LinkState.SUSPECT for s in my):
            return "suspect"
        return "ok"

    def build_link_states(self, doc_id: str) -> dict[str, tuple[str, str]]:
        """Per-object `(outgoing, incoming)` link state for every obj in `doc_id`.

        Values are one of "none" (no link in that direction), "ok", "suspect",
        "broken". Existence is read from the schema so it works even when the
        git-backed suspect scan fails (non-git worktrees); state is upgraded
        from "ok" using cached scan results when available.

        The map is computed once per grid rebuild rather than per-cell so the
        gutter delegate's paint() stays O(1).
        """
        result: dict[str, tuple[str, str]] = {}
        if self._service is None:
            return result
        try:
            doc = self._service.get_document(doc_id)
        except ServiceError:
            return result

        all_docs = self.docs()

        has_outgoing = {oid: bool(doc.links.get(oid)) for oid in doc.objects}
        has_incoming = {oid: False for oid in doc.objects}
        for loaded in all_docs.values():
            for link_list in loaded.document.links.values():
                for link in link_list:
                    if link.target_doc == doc_id and link.target_id in has_incoming:
                        has_incoming[link.target_id] = True

        for oid in doc.objects:
            out = "ok" if has_outgoing[oid] else "none"
            inc = "ok" if has_incoming[oid] else "none"
            result[oid] = (out, inc)

        # Priority for worst-state merge; "none" only survives when there are
        # literally no links in that direction.
        rank = {"none": 0, "ok": 1, "suspect": 2, "broken": 3}
        state_name = {
            LinkState.OK: "ok",
            LinkState.SUSPECT: "suspect",
            LinkState.BROKEN: "broken",
        }

        for d_id in all_docs:
            try:
                statuses = self._service.scan_suspects(d_id)[d_id]
            except ServiceError:
                continue  # e.g. non-git repo — keep existence-based fallback
            for s in statuses:
                new = state_name[s.state]
                # Outgoing contribution: source is in this doc.
                if d_id == doc_id and s.source_id in result:
                    out, inc = result[s.source_id]
                    if rank[new] > rank[out]:
                        result[s.source_id] = (new, inc)
                # Incoming contribution: target is in this doc.
                if s.link.target_doc == doc_id and s.link.target_id in result:
                    out, inc = result[s.link.target_id]
                    if rank[new] > rank[inc]:
                        result[s.link.target_id] = (out, new)

        return result

    def in_last_commit(self, doc_id: str) -> bool:
        """True when `doc_id`'s folder is in HEAD: there is a committed
        version for "Discard uncommitted changes" to go back to."""
        if self._service is None:
            return False
        try:
            folder = self.folder(doc_id)
        except (ServiceError, KeyError):
            return False
        try:
            repo, root = discover_repo(folder)
        except GitError:
            return False
        if repo.head_is_unborn:
            return False
        rel = folder.resolve().relative_to(root).as_posix()
        try:
            repo.head.peel(pygit2.Commit).tree[rel]
        except KeyError:
            return False
        return True

    def build_dirty_ids(self, doc_id: str) -> frozenset[str]:
        """Object ids whose object xml, links file, or figure svg is dirty vs HEAD.

        Returns an empty set when the doc isn't in a git repo, the service is
        unavailable, or every file under the doc folder matches HEAD. A
        link-only or figure-only edit still flips its source object's id into
        the set, matching how the commit dialog groups affected requirements.

        Computed once per grid rebuild so the id-column delegate's paint
        stays O(1).
        """
        if self._service is None:
            return frozenset()
        try:
            folder = self._service.get_loaded_document(doc_id).folder
        except ServiceError:
            return frozenset()
        try:
            repo, root = discover_repo(folder)
        except GitError:
            return frozenset()
        folder_rel = folder.resolve().relative_to(root).as_posix()
        sources = (
            (f"{folder_rel}/objects/", ".xml"),
            (f"{folder_rel}/links/", ".xml"),
            (f"{folder_rel}/figures/", ".svg"),
        )
        dirty: set[str] = set()
        for p in dirty_paths_under(repo, folder_rel):
            for prefix, suffix in sources:
                if p.startswith(prefix):
                    stem = p[len(prefix):]
                    if stem.endswith(suffix):
                        stem = stem[: -len(suffix)]
                    if stem and "/" not in stem:
                        dirty.add(stem)
                    break
        return frozenset(dirty)

    def suspects_for_object(self, doc_id: str, obj_id: str) -> list[LinkStatus]:
        return [s for s in self.suspect_statuses(doc_id) if s.source_id == obj_id]

    def attach_service(self, service: DocumentService) -> None:
        """Bind to an already-loaded service without re-parsing the repo.

        Used by DocumentEditorWindow so multiple editors can share one
        DocumentService (and its cache) while each keeping independent
        selection state.
        """
        self._service = service
        self._current_doc = None
        self._current_obj = None
        self.repo_loaded.emit(str(service.repo_root))

    # --- richer views ----------------------------------------------------

    def object_view(self, doc_id: str, obj_id: str) -> ObjectView:
        svc = self.require_service()
        doc = svc.get_document(doc_id)
        obj = doc.objects[obj_id]
        path = doc.outline_path(obj_id) or ()

        outgoing: list[LinkStatus] = []
        try:
            scoped = svc.scan_suspects(doc_id)
            outgoing = [s for s in scoped[doc_id] if s.source_id == obj_id]
        except ServiceError as e:
            if e.code is ErrorCode.GIT:
                for link in doc.links.get(obj_id, ()):
                    outgoing.append(
                        LinkStatus(
                            source_id=obj_id,
                            link=link,
                            state=LinkState.OK,  # placeholder; UI label reads the reason
                            current_hash=None,
                            reason="not in a git repo",
                        )
                    )
            else:
                raise

        incoming = [
            (src_doc, src_id, link.type)
            for src_doc, src_id, link in svc.incoming_links(doc_id, obj_id)
        ]

        return ObjectView(
            doc_id=doc_id,
            obj_id=obj_id,
            object=obj,
            outline_path=tuple(path),
            outgoing=tuple(outgoing),
            incoming=tuple(incoming),
        )

    # --- git history -----------------------------------------------------

    def object_history(
        self, doc_id: str, obj_id: str, max_entries: int = 20
    ) -> list[dict]:
        """Recent commits on HEAD that changed this object's file or its
        figure, newest first.

        Returns `[]` when no repo is open, the doc folder isn't in a git repo,
        or the object's file isn't tracked. Each entry has the same shape as
        `git_ops.log_for_path` (`commit_oid`, `short_hash`, `message`, `author`,
        `email`, `timestamp`) plus an `issue` field: the issue string of the
        tag pointing exactly at that commit, or `""` if no tag bumps there.
        """
        if self._service is None:
            return []
        try:
            folder = self.folder(doc_id)
        except (ServiceError, KeyError):
            return []
        try:
            repo, root = discover_repo(folder)
        except GitError:
            return []
        folder_rel = folder.resolve().relative_to(root).as_posix()
        # The figure counts as part of the object, as it does for suspects.
        paths = [f"{folder_rel}/objects/{obj_id}.xml", f"{folder_rel}/figures/{obj_id}.svg"]
        try:
            rows = git_ops.log_for_path(repo, paths, max_entries=max_entries)
        except GitError:
            return []
        issue_map = git_ops.issue_tags_by_commit(repo, doc_id)
        for row in rows:
            row["issue"] = issue_map.get(row["commit_oid"], "")
        return rows

    # --- views ----------------------------------------------------------

    def create_view(
        self, doc_id: str, name: str, visible_attrs: tuple[str, ...] | list[str]
    ) -> str:
        result = self.require_service().create_view(doc_id, name, visible_attrs)
        self.views_changed.emit(doc_id)
        return result.value

    def delete_view(self, doc_id: str, slug: str) -> None:
        self.require_service().delete_view(doc_id, slug)
        # Drop the GUI-side QSettings entries (widths + active pointer) for
        # this slug so they don't linger if the slug is later reused.
        from .panels.grid_pane import forget_view_storage
        forget_view_storage(doc_id, slug)
        self.views_changed.emit(doc_id)

    def rename_view(self, doc_id: str, slug: str, new_name: str) -> str:
        result = self.require_service().rename_view(doc_id, slug, new_name)
        new_slug = result.value
        # Move the GUI-side widths bucket and active pointer to the new slug.
        from .panels.grid_pane import migrate_view_storage
        migrate_view_storage(doc_id, slug, new_slug)
        self.views_changed.emit(doc_id)
        return new_slug

    def set_view_visible(
        self,
        doc_id: str,
        slug: str,
        visible_attrs: tuple[str, ...] | list[str],
    ) -> None:
        self.require_service().set_view_visible(doc_id, slug, visible_attrs)
        self.views_changed.emit(doc_id)

    # --- filters --------------------------------------------------------

    def create_filter(
        self,
        doc_id: str,
        name: str,
        combinator: str = "all",
        keep_ancestors: bool = True,
        rules: tuple[FilterRule, ...] | list[FilterRule] = (),
    ) -> str:
        result = self.require_service().create_filter(
            doc_id, name, combinator, keep_ancestors, rules
        )
        self.filters_changed.emit(doc_id)
        return result.value

    def delete_filter(self, doc_id: str, slug: str) -> None:
        self.require_service().delete_filter(doc_id, slug)
        self.filters_changed.emit(doc_id)

    def rename_filter(self, doc_id: str, slug: str, new_name: str) -> str:
        result = self.require_service().rename_filter(doc_id, slug, new_name)
        self.filters_changed.emit(doc_id)
        return result.value

    def set_filter_rules(
        self,
        doc_id: str,
        slug: str,
        combinator: str,
        keep_ancestors: bool,
        rules: tuple[FilterRule, ...] | list[FilterRule],
    ) -> None:
        self.require_service().set_filter_rules(
            doc_id, slug, combinator, keep_ancestors, rules
        )
        self.filters_changed.emit(doc_id)

    # --- document commit -------------------------------------------------

    def prepare_document_commit(self, doc_id: str) -> DocCommitPreview:
        """Compute the file list, default message, and parent warnings.

        Pass-through to the service so dialog code never touches it directly.
        Raises `ServiceError(GIT)` only when the doc folder isn't in a repo.
        """
        return self.require_service().prepare_document_commit(doc_id)

    def commit_document(
        self,
        doc_id: str,
        message: str,
        *,
        new_issue: str | None = None,
        tag: str | None = None,
        tag_message: str = "",
    ):
        """Commit only this document's dirty files. Returns the `CommitResult`.

        Emits `committed(doc_id, short_hash)` on success so the status bar
        and any open history tab can refresh. Forwards `new_issue` to the
        service so the matching `<doc_id>-issue-<n>` tag is created at the
        new commit when applicable, and `tag` / `tag_message` for the Tag
        field's tag.
        """
        result = self.require_service().commit_document(
            doc_id, message, new_issue=new_issue, tag=tag, tag_message=tag_message
        )
        self.committed.emit(doc_id, result.short_hash)
        return result

    def tag_head(self, doc_id: str, name: str, message: str = "") -> str:
        """Tag the current commit `name` (the commit dialog's Tag when the
        document has nothing to commit). Pass-through to the service."""
        return self.require_service().tag_head(doc_id, name, message)

    def object_at_commit(
        self, doc_id: str, obj_id: str, commit_ref: str
    ) -> ObjectAtCommit:
        """Parsed snapshot of one object as it existed at `commit_ref`.

        Raises `ServiceError(GIT)` when the doc isn't in a git repo, the
        commit ref can't be resolved, or the object's file wasn't present
        at that commit.

        The historical snapshot is parsed straight from the blob — outline
        path and outgoing links are read from the manifest / links file at
        the same commit, so the dialog reflects what was stored then rather
        than what HEAD shows now. Incoming links are intentionally omitted:
        they're a global cross-doc computation that isn't meaningful at a
        point-in-time view of a single object.
        """
        svc = self.require_service()
        try:
            folder = self.folder(doc_id)
        except KeyError as e:
            raise ServiceError(ErrorCode.NOT_FOUND, str(e)) from e
        try:
            repo, root = discover_repo(folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e

        rel_dir = folder.resolve().relative_to(root).as_posix()
        obj_rel = f"{rel_dir}/objects/{obj_id}.xml"

        try:
            xml_text = git_ops.show_blob_at(repo, obj_rel, commit_ref)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e

        obj = _parse_historical_object(xml_text, obj_id)

        manifest_rel = f"{rel_dir}/{doc_id}.xml"
        outline_path: tuple[str, ...] = ()
        try:
            manifest_xml = git_ops.show_blob_at(repo, manifest_rel, commit_ref)
            outline_path = _outline_path_from_manifest(manifest_xml, obj_id)
        except GitError:
            outline_path = ()

        outgoing: tuple[Link, ...] = ()
        links_rel = f"{rel_dir}/links/{obj_id}.xml"
        try:
            links_xml = git_ops.show_blob_at(repo, links_rel, commit_ref)
            outgoing = _parse_historical_links(links_xml)
        except GitError:
            outgoing = ()

        try:
            resolved = repo.revparse_single(commit_ref).peel(pygit2.Commit)
            short_hash = str(resolved.id)[:7]
        except (pygit2.GitError, KeyError):
            short_hash = commit_ref[:7]

        return ObjectAtCommit(
            doc_id=doc_id,
            obj_id=obj_id,
            short_hash=short_hash,
            object=obj,
            outline_path=outline_path,
            outgoing=outgoing,
        )


def _parse_historical_object(xml_text: str, expected_id: str) -> RequirementObject:
    root = etree.fromstring(xml_text.encode("utf-8"))
    if root.tag != "object":
        raise ServiceError(
            ErrorCode.LOAD,
            f"historical blob root is <{root.tag}>, expected <object>",
        )
    attrs: dict[str, str] = {}
    for a in root.findall("attr"):
        name = a.get("name")
        if not name:
            continue
        attrs[name] = (a.text or "").strip()
    description = ""
    desc_el = root.find("description")
    if desc_el is not None and desc_el.text is not None:
        description = desc_el.text.strip()
    return RequirementObject(
        id=root.get("id") or expected_id,
        attrs=attrs,
        description=description,
    )


def _outline_path_from_manifest(
    manifest_xml: str, obj_id: str
) -> tuple[str, ...]:
    root = etree.fromstring(manifest_xml.encode("utf-8"))
    outline = root.find("outline")
    if outline is None:
        return ()

    def walk(parent: etree._Element, trail: tuple[str, ...]) -> tuple[str, ...] | None:
        for r in parent.findall("ref"):
            ref_id = r.get("id") or ""
            here = trail + (ref_id,)
            if ref_id == obj_id:
                return here
            found = walk(r, here)
            if found is not None:
                return found
        return None

    return walk(outline, ()) or ()


def _parse_historical_links(links_xml: str) -> tuple[Link, ...]:
    root = etree.fromstring(links_xml.encode("utf-8"))
    if root.tag != "links":
        return ()
    out: list[Link] = []
    for el in root.findall("link"):
        target_doc = el.get("target-doc") or ""
        target_id = el.get("target-id") or ""
        link_type = el.get("type") or ""
        ack = el.get("ack")
        if not target_doc or not target_id or not link_type:
            continue
        out.append(
            Link(
                target_doc=target_doc,
                target_id=target_id,
                type=link_type,
                ack=ack,
            )
        )
    return tuple(out)
