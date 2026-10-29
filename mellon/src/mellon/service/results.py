"""Result shapes for `DocumentService` mutations.

Every mutation returns the raw value (new id, bool, etc.) together with the
already-reloaded target document and a list of doc ids that changed on disk.
Callers use `affected_docs` for cache invalidation and signal emission —
crucial for cascading deletes that touch sibling documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

from ..editor import DeleteReport
from ..schema import Document
from ..suspects import LinkStatus


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class MutationResult(Generic[T]):
    value: T
    refreshed: Document
    affected_docs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompositeWriteResult:
    """Outcome of a composite multi-field write (`create_object_full`, `update_object`).

    The MCP layer turns this into a single `{ok, affected_files, object, …}`
    response so an agent never has to follow up with `show_object` to see the
    end-state of a batched edit.
    """

    obj_id: str
    refreshed: Document
    affected_files: tuple[Path, ...]
    attrs_set: tuple[str, ...]
    links_added: tuple[tuple[str, str, str], ...]  # (target_doc, target_id, type)
    links_removed: tuple[tuple[str, str], ...]     # (target_doc, target_id)
    description_changed: bool


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Outcome of `delete_object`.

    `report.performed` is False when the delete was blocked by incoming links
    (and `force=False`); in that case `refreshed_docs` is empty. When the
    delete went through, `refreshed_docs` maps each affected doc id to its
    freshly loaded post-mutation `Document`.
    """

    report: DeleteReport
    affected_docs: tuple[str, ...]
    refreshed_docs: dict[str, Document] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Outcome of a per-document commit.

    `tag_name` is set on success when the caller passed `new_issue` and the
    `<doc-id>-issue-<n>` tag was created. `tag_error` carries the failure
    message when tag creation hit an error (the commit itself still landed —
    we never roll a commit back over a tag failure). `repo_tag` /
    `repo_tag_error` are the same for the free-named tag of the dialog's Tag
    field (`commit_document(tag=...)`).
    """

    short_hash: str
    paths: tuple[Path, ...] = ()
    tag_name: str | None = None
    tag_error: str | None = None
    repo_tag: str | None = None
    repo_tag_error: str | None = None


@dataclass(frozen=True, slots=True)
class DocCommitPreview:
    """What `commit_document` would commit, for the dialog to display.

    `paths` lists every repo-relative posix path inside the document folder
    that has working-tree changes (new/modified/deleted/typechange/renamed).
    `affected_req_ids` is the sorted, deduped set of object ids whose object
    or links file is dirty — empty when only the manifest or rules file
    changed. `default_message` follows the project convention:
    "Edited <doc>. Affected requirements: <id>, <id>." or, when nothing
    affects a requirement, "Edited <doc>. No affected requirements."
    `dirty_parents` is the sorted list of *parent* document ids (documents
    this one links up to) that also have uncommitted changes — the dialog
    surfaces this as a warning so the user knows a paired change in a
    parent doc won't be included in this commit.

    `suspect_links` lists this doc's outgoing links that are currently
    SUSPECT or BROKEN. It's a soft warning: the dialog shows them so the
    user can decide whether to ack first, but commit isn't blocked.

    The `current_*` fields hold the manifest's existing metadata values so
    the dialog can compare against them and only call `set_metadata` for
    fields the user changed. The `prefill_*` fields hold the suggested new
    values: the current issue (so a plain commit cuts no tag; '' when the
    document has no issue tag yet), author from the manifest (falling back
    to git `user.name`), and the manifest's existing date.

    For the Tag field: `existing_tags` holds every tag name in the repo, so
    the dialog can refuse a taken name as the user types, and
    `upstream_warnings` lists the open points (uncommitted changes, suspect
    or broken links, validation errors, a failed load) of the documents this
    one links up to, transitively, one line each — shown only while a tag is
    entered.
    """

    doc_id: str
    paths: tuple[str, ...]
    affected_req_ids: tuple[str, ...]
    default_message: str
    dirty_parents: tuple[str, ...]
    suspect_links: tuple[LinkStatus, ...] = ()
    current_issue: str = ""
    current_author: str = ""
    current_date: str = ""
    prefill_issue: str = ""
    prefill_author: str = ""
    prefill_date: str = ""
    existing_tags: tuple[str, ...] = ()
    upstream_warnings: tuple[str, ...] = ()
