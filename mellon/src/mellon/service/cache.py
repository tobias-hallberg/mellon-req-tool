"""Opt-in repo cache owned by `DocumentService`.

Used by the GUI (one service per open repo) to avoid re-parsing every document
on every keystroke. Disabled for MCP, which is short-lived / stateless.

The cache tracks three things:
    - `LoadedDoc` entries keyed by doc id.
    - The `DocLoadError`s from the last repo-wide load.
    - Per-doc suspect scans (expensive: walks git history), each stored with
      the HEAD commit it was taken at.

Invalidation is explicit: every mutation in `DocumentService` calls
`invalidate(affected_docs)`, which drops both kinds of cached state for those
docs. `invalidate_all` is used when the underlying repo root changes. A
suspect scan taken at another HEAD is a miss, so any commit (from mellon or a
git client) re-scans: it can change the state of links in other documents.
"""

from __future__ import annotations

from typing import Iterable

from ..repo import DocLoadError, LoadedDoc
from ..suspects import LinkStatus


class RepoCache:
    def __init__(self) -> None:
        self._docs: dict[str, LoadedDoc] = {}
        self._suspects: dict[str, tuple[str | None, list[LinkStatus]]] = {}
        self._load_errors: tuple[DocLoadError, ...] = ()
        self._loaded_all: bool = False

    # --- documents ------------------------------------------------------

    def get(self, doc_id: str) -> LoadedDoc | None:
        return self._docs.get(doc_id)

    def put(self, loaded: LoadedDoc) -> None:
        self._docs[loaded.document.id] = loaded
        # It loads now, so it is no longer a failed document.
        self._load_errors = tuple(
            e for e in self._load_errors if e.doc_id != loaded.document.id
        )

    def put_all(
        self, docs: dict[str, LoadedDoc], load_errors: Iterable[DocLoadError] = ()
    ) -> None:
        self._docs = dict(docs)
        self._load_errors = tuple(load_errors)
        self._suspects.clear()
        self._loaded_all = True

    def get_all(self) -> dict[str, LoadedDoc] | None:
        """Return the full doc map only if we have loaded all docs at least once."""
        return dict(self._docs) if self._loaded_all else None

    def load_errors(self) -> list[DocLoadError]:
        """Documents that failed to load in the last `put_all` (and have not
        loaded since)."""
        return list(self._load_errors)

    def invalidate(self, doc_ids: Iterable[str]) -> None:
        for did in doc_ids:
            self._docs.pop(did, None)
            self._suspects.pop(did, None)

    def invalidate_all(self) -> None:
        self._docs.clear()
        self._load_errors = ()
        self._suspects.clear()
        self._loaded_all = False

    # --- suspect scans --------------------------------------------------

    def suspect_status(self, doc_id: str, head: str | None) -> list[LinkStatus] | None:
        """The cached scan for `doc_id`, or None if absent or taken at
        another HEAD commit."""
        hit = self._suspects.get(doc_id)
        if hit is None or hit[0] != head:
            return None
        return hit[1]

    def put_suspect_status(
        self, doc_id: str, statuses: list[LinkStatus], head: str | None
    ) -> None:
        self._suspects[doc_id] = (head, list(statuses))

    def invalidate_suspects(self) -> None:
        """Drop every cached suspect scan, keeping the loaded documents.

        For work-tree changes that HEAD doesn't key: a scan also checks that
        link targets exist on disk, so restoring or removing files in one
        document can change other documents' scans.
        """
        self._suspects.clear()
