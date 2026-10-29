"""MCP stdio server exposing mellon tools.

Launch with:
    uv run mellon-mcp [--repo-root <path>] [--read-only]

`--repo-root` defaults to `$CLAUDE_PROJECT_DIR`, then the current directory.

Every tool operates through a single `DocumentService` instance scoped to the
configured repo root; doc_id arguments resolve to `<repo_root>/<doc_id>/`.
There's no path traversal — tools never accept filesystem paths from the
agent, with two deliberate exceptions, both resolving relative paths against
the repo root: `set_figure` reads the SVG at `source_path`, and
`export_document` writes the rendered PDF to `out_path` (which is why
`--read-only` disables it — that flag's promise is "this server cannot write
to the filesystem").

`--read-only` makes every write tool refuse with a `PermissionError`. Writes
otherwise call service methods directly; `ServiceError` is translated to the
long-established `{ok: False, error, error_type}` dict so existing agents
don't see a breaking change.

All logging goes to stderr (MCP stdio reserves stdout for JSON-RPC).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal

from lxml import etree
from mcp.server.fastmcp import FastMCP

from ..editor import EditorError, _child_path
from ..drawio import find_drawio
from ..export import TectonicError, pdf as export_pdf
from ..git_ops import GitError, discover_repo
from .. import __version__, git_ops
from ..repo import DocLoadError, discover_documents
from ..schema import Table, TableCell, TableColumn, TableRow
from ..service import DocumentService, ErrorCode, ServiceError
from .serialize import (
    coverage_report_to_dict,
    document_summary_to_dict,
    link_status_to_dict,
    load_error_to_dict,
    object_to_dict,
    search_hit_to_dict,
    trace_edge_to_dict,
)


log = logging.getLogger("mellon.mcp")

mcp = FastMCP("mellon")
# FastMCP 1.x has no `version` kwarg; without this, serverInfo reports the
# mcp SDK's version instead of Mellon's.
mcp._mcp_server.version = __version__


class _State:
    """Module-level server state. Populated by `main()` before `mcp.run()`."""

    repo_root: Path | None = None
    read_only: bool = False
    service: DocumentService | None = None


# `error_type` strings agents see on the wire. Kept stable across the service
# refactor — tests pin these values.
_ERROR_TYPE_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.NOT_FOUND: "EditorError",
    ErrorCode.VALIDATION: "EditorError",
    ErrorCode.CONFLICT: "EditorError",
    ErrorCode.ALREADY_EXISTS: "AlreadyExists",
    ErrorCode.LOAD: "LoaderError",
    ErrorCode.GIT: "GitError",
    ErrorCode.READ_ONLY: "PermissionError",
    ErrorCode.IO: "OSError",
}


def _require_root() -> Path:
    if _State.repo_root is None:
        raise RuntimeError(
            "MCP server not initialized: run via `mellon-mcp [--repo-root <path>]`."
        )
    return _State.repo_root


def _svc() -> DocumentService:
    """Return the active service, building or rebuilding it if the root or
    read-only flag changed.

    Tests commonly monkeypatch `_State.repo_root` mid-process; lazy
    re-construction keeps the service in sync without forcing tests to also
    set `_State.service`.
    """
    root = _require_root()
    if (
        _State.service is None
        or _State.service.repo_root != root
        or _State.service.read_only != _State.read_only
    ):
        _State.service = DocumentService(root, read_only=_State.read_only)
    return _State.service


def _guard_write() -> None:
    """Enforce --read-only at the MCP boundary.

    Read-only is tracked on `_State.read_only` so tests can monkeypatch it
    mid-process and get the same `PermissionError` the pre-service server
    raised. The service is built read-only too, as a second line of defence.
    """
    if _State.read_only:
        raise PermissionError(
            "Server is running in --read-only mode; write tools are disabled."
        )


def _require_doc(doc_id: str) -> Path:
    """Resolve `doc_id` to its folder; raise ValueError if the doc is missing.

    Kept separate from the service so "no such document" surfaces as a clean
    ValueError (which FastMCP turns into a JSON-RPC error) rather than the
    in-band error dict used for per-object write failures.
    """
    root = _require_root()
    try:
        # A doc_id names a folder directly under the root; "../X/X" must not
        # reach a manifest that happens to exist outside it.
        folder = _child_path(root, doc_id)
    except EditorError:
        folder = None
    if (
        folder is None
        or not folder.is_dir()
        or not (folder / f"{doc_id}.xml").is_file()
    ):
        raise ValueError(f"No document {doc_id!r} in repo at {root}")
    return folder


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_require_root()).as_posix()


def _figure_rel(doc_id: str, obj_id: str) -> str:
    """Repo-relative path of an object's figure (whether or not it exists)."""
    return f"{doc_id}/figures/{obj_id}.svg"


def _rels(paths) -> list[str]:
    return [_rel(Path(p)) for p in paths]


def _write_result(*, affected: list[str], message: str, **extra: Any) -> dict:
    out: dict = {"ok": True, "affected_files": affected, "message": message}
    out.update(extra)
    return out


def _service_error_result(err: ServiceError, *, message: str) -> dict:
    return {
        "ok": False,
        "affected_files": [],
        "message": message,
        "error": err.message,
        "error_type": _ERROR_TYPE_BY_CODE.get(err.code, "ServiceError"),
    }


def _flat_outline(doc) -> list[dict]:
    out: list[dict] = []

    def walk(nodes, depth: int) -> None:
        for n in nodes:
            obj = doc.objects.get(n.ref_id)
            text = obj.description if obj else ""
            first_line = text.splitlines()[0] if text else ""
            out.append(
                {
                    "id": n.ref_id,
                    "depth": depth,
                    "heading_or_text": first_line[:200],
                }
            )
            walk(n.children, depth + 1)

    walk(doc.outline, 0)
    return out


def _trim_tree(edges: list[dict], depth_left: int) -> list[dict]:
    if depth_left <= 0:
        return [dict(e, children=[]) for e in edges]
    return [
        dict(e, children=_trim_tree(e["children"], depth_left - 1)) for e in edges
    ]


# --- Read tools ------------------------------------------------------------


@mcp.tool()
def list_documents(include: list[str] | None = None) -> list[dict]:
    """List every document in the configured repo (compact by default).

    The default response carries only the counts and the `current_issue`
    tag — enough for an agent to decide which docs to drill into. Pass
    `include=["metadata", "parents", "attributes", "outline", "views",
    "filters"]` to add specific heavy fields, or `include=["*"]` / `["all"]`
    for the full fat shape (matches the pre-PR-3 response).

    A document that fails to load (e.g. a file with unresolved merge-conflict
    markers) is still listed, as `{doc_id, folder, load_error: {doc_id,
    file, line, message}}` without counts; fix the named file to bring it
    back.
    """
    log.info("list_documents include=%s", include)
    svc = _svc()
    root = _require_root()
    inc = frozenset(include) if include else None
    out: list[dict] = []
    load_errors: list[DocLoadError] = []
    for loaded in svc.list_documents(load_errors=load_errors):
        out.append(
            document_summary_to_dict(
                loaded.document,
                folder=str(loaded.folder.relative_to(root)),
                current_issue=svc.current_issue(loaded.document.id),
                include=inc,
            )
        )
    for err in load_errors:
        out.append(
            {
                "doc_id": err.doc_id,
                "folder": str(err.folder.relative_to(root)),
                "load_error": load_error_to_dict(err, root),
            }
        )
    out.sort(key=lambda d: d["doc_id"])
    return out


@mcp.tool()
def show_document(doc_id: str, include: list[str] | None = None) -> dict:
    """Manifest-level detail for one document (compact by default).

    The default response carries counts + `current_issue` only. Pass
    `include=["metadata", "parents", "attributes", "outline", "views",
    "filters"]` to add specific fields, or `include=["*"]` / `["all"]` for
    the full fat shape. `current_issue` reflects the latest
    `<doc_id>-issue-*` tag reachable from HEAD; empty when no such tag
    exists or the doc isn't in a git repo.
    """
    log.info("show_document %s include=%s", doc_id, include)
    folder = _require_doc(doc_id)
    svc = _svc()
    doc = svc.get_document(doc_id)
    return document_summary_to_dict(
        doc,
        folder=str(folder.relative_to(_require_root())),
        current_issue=svc.current_issue(doc_id),
        include=frozenset(include) if include else None,
    )


@mcp.tool()
def search_objects(
    query: str,
    doc_ids: list[str] | None = None,
    attr_filters: dict[str, str] | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Find objects whose id, attributes, or description contain `query`.

    Case-insensitive substring match. `doc_ids` limits the scan to a subset
    of documents (omit to search the whole repo). `attr_filters` requires
    exact equality on the given attribute names (e.g.
    `{"status": "Approved"}` keeps only approved objects). Results are
    sorted by descending score, then by `(doc_id, obj_id)` for stable
    output.

    Scoring is additive: obj_id match +5, each attribute value match +2,
    description match +1. Returns up to `max_results` hits, each with a
    short snippet of the description.

    Use this instead of pulling whole document outlines when the agent
    knows roughly what it's looking for — it's the discovery primitive the
    rest of the MCP doesn't have.
    """
    log.info(
        "search_objects q=%r docs=%s filters=%s max=%d",
        query, doc_ids, attr_filters, max_results,
    )
    svc = _svc()
    hits = svc.search_objects(
        query,
        doc_ids=tuple(doc_ids) if doc_ids else None,
        attr_filters=attr_filters,
        max_results=max_results,
    )
    return [search_hit_to_dict(h) for h in hits]


@mcp.tool()
def show_object(doc_id: str, obj_id: str) -> dict:
    """One object with attributes, description, outline path, and links."""
    log.info("show_object %s %s", doc_id, obj_id)
    _require_doc(doc_id)
    svc = _svc()
    doc = svc.get_document(doc_id)
    if obj_id not in doc.objects:
        raise ValueError(f"No object {obj_id!r} in document {doc_id!r}")

    path = doc.outline_path(obj_id)
    outline_path = list(path) if path is not None else []

    outgoing: list[dict] = []
    try:
        scoped = svc.scan_suspects(doc_id)
        outgoing = [
            link_status_to_dict(s)
            for s in scoped[doc_id]
            if s.source_id == obj_id
        ]
    except ServiceError as e:
        if e.code is ErrorCode.GIT:
            for link in doc.links.get(obj_id, ()):
                outgoing.append(
                    {
                        "source_id": obj_id,
                        "link": {
                            "target_doc": link.target_doc,
                            "target_id": link.target_id,
                            "type": link.type,
                            "ack": link.ack,
                        },
                        "state": "unknown",
                        "current_hash": None,
                        "reason": "not in a git repo",
                    }
                )
        else:
            raise

    incoming = [
        {"source_doc": src_doc, "source_id": src_id, "link_type": link.type}
        for src_doc, src_id, link in svc.incoming_links(doc_id, obj_id)
    ]

    return {
        "doc_id": doc_id,
        "object": object_to_dict(doc.objects[obj_id]),
        "outline_path": outline_path,
        "outgoing": outgoing,
        "incoming": incoming,
    }


@mcp.tool()
def inspect_object(
    doc_id: str,
    obj_id: str,
    include_trace: bool = True,
    trace_depth: int = 2,
    include_history: bool = True,
    history_max: int = 5,
) -> dict:
    """Composite read: object body + outgoing/incoming + trace + git history.

    Equivalent to `show_object` + `trace(direction="both")` + `git_history`
    but in a single round trip. Use this when the agent needs full context on
    a requirement (what it says, what's above and below it in the trace
    chain, when it was last touched).

    Knobs:
      - `include_trace=False` skips the up/down recursive walk.
      - `trace_depth` bounds the walk; pass `0` for first-level neighbors only.
      - `include_history=False` skips git log (cheap when the repo is huge).
      - `history_max` caps the number of commits returned.
    """
    log.info(
        "inspect_object %s/%s trace=%s depth=%s history=%s n=%s",
        doc_id, obj_id, include_trace, trace_depth, include_history, history_max,
    )
    folder = _require_doc(doc_id)
    svc = _svc()
    doc = svc.get_document(doc_id)
    if obj_id not in doc.objects:
        raise ValueError(f"No object {obj_id!r} in document {doc_id!r}")

    path = doc.outline_path(obj_id)
    outline_path = list(path) if path is not None else []

    outgoing: list[dict] = []
    try:
        scoped = svc.scan_suspects(doc_id)
        outgoing = [
            link_status_to_dict(s)
            for s in scoped[doc_id]
            if s.source_id == obj_id
        ]
    except ServiceError as e:
        if e.code is ErrorCode.GIT:
            for link in doc.links.get(obj_id, ()):
                outgoing.append(
                    {
                        "source_id": obj_id,
                        "link": {
                            "target_doc": link.target_doc,
                            "target_id": link.target_id,
                            "type": link.type,
                            "ack": link.ack,
                        },
                        "state": "unknown",
                        "current_hash": None,
                        "reason": "not in a git repo",
                    }
                )
        else:
            raise

    incoming = [
        {"source_doc": src_doc, "source_id": src_id, "link_type": link.type}
        for src_doc, src_id, link in svc.incoming_links(doc_id, obj_id)
    ]

    result: dict[str, Any] = {
        "doc_id": doc_id,
        "object": object_to_dict(doc.objects[obj_id]),
        "outline_path": outline_path,
        "outgoing": outgoing,
        "incoming": incoming,
    }

    if include_trace:
        up_edges = [trace_edge_to_dict(e) for e in svc.trace_up(doc_id, obj_id)]
        down_edges = [trace_edge_to_dict(e) for e in svc.trace_down(doc_id, obj_id)]
        if trace_depth is not None:
            up_edges = _trim_tree(up_edges, trace_depth - 1)
            down_edges = _trim_tree(down_edges, trace_depth - 1)
        result["trace_up"] = up_edges
        result["trace_down"] = down_edges

    if include_history:
        try:
            repo, root = discover_repo(folder)
            rel = f"{doc_id}/objects/{obj_id}.xml"
            if (root / rel).exists():
                result["history"] = git_ops.log_for_path(
                    repo, [rel, _figure_rel(doc_id, obj_id)], max_entries=history_max
                )
            else:
                result["history"] = []
        except GitError:
            result["history"] = []

    return result


@mcp.tool()
def outline(doc_id: str) -> list[dict]:
    """Flat pre-order outline of a document."""
    log.info("outline %s", doc_id)
    _require_doc(doc_id)
    return _flat_outline(_svc().get_document(doc_id))


@mcp.tool()
def validate_document(doc_id: str) -> dict:
    """Run schema validation on a document.

    Returns `{ok, errors[]}` where each error is `{code, message}`. The `code`
    is one of the stable validator codes (`ATTR_MISSING_REQUIRED`,
    `OUTLINE_DUP`, `LINK_TARGET_MISSING`, `RULE_REQUIRED_MISSING`, etc.) so an
    agent can branch on the failure kind without parsing the message text.
    """
    log.info("validate_document %s", doc_id)
    _require_doc(doc_id)
    errors = _svc().validate_document(doc_id)
    return {
        "ok": len(errors) == 0,
        "errors": [{"code": e.code, "message": e.message} for e in errors],
    }


@mcp.tool()
def validate_repo() -> dict:
    """Run schema validation on every document; aggregate the results.

    Cheaper for the agent than looping `validate_document(d)` for every doc.
    Returns `{ok, total_errors, documents: [{doc_id, ok, errors}],
    load_errors}`. Each error dict is `{code, message}` (same shape as
    `validate_document`). `load_errors` lists the documents that failed to
    load, and so could not be validated, as `{doc_id, file, line, message}`.
    `ok` at the top level is True iff every doc loads and validates clean.
    """
    log.info("validate_repo")
    svc = _svc()
    load_errors: list[DocLoadError] = []
    scoped = svc.validate_repo(load_errors=load_errors)
    documents = []
    total = 0
    for doc_id in sorted(scoped.keys()):
        errors = scoped[doc_id]
        total += len(errors)
        documents.append(
            {
                "doc_id": doc_id,
                "ok": len(errors) == 0,
                "errors": [
                    {"code": e.code, "message": e.message} for e in errors
                ],
            }
        )
    return {
        "ok": total == 0 and not load_errors,
        "total_errors": total,
        "documents": documents,
        "load_errors": [load_error_to_dict(e, svc.repo_root) for e in load_errors],
    }


@mcp.tool()
def repo_health() -> dict:
    """One-call snapshot of the repo: counts, validation, suspects, coverage.

    Use this as an agent's *first read* on a fresh repo. Replaces the
    1 + N + 1 + K calls (`list_documents` + `validate_document × N` +
    `suspects()` + `coverage_report × K`) that would otherwise be needed
    just to answer "what state is this in?".

    Returns:
      - `documents[]`: per-doc `{doc_id, object_count, link_count,
        current_issue, parents, validation_error_count, suspect_count,
        broken_count}`
      - `coverage[]`: one row per declared parent relationship, in both
        directions — `outgoing` (does each child obj justify itself?) and
        `incoming` (does each parent obj have a downstream child?). Uses
        the cheap fast path: `include_headings=False`, `transitive=False`.
        For deeper analysis, call `coverage_report` directly.
      - `total_validation_errors`, `total_suspect_count`, `total_broken_count`
        for at-a-glance health.
      - `load_errors[]`: `{doc_id, file, line, message}` for each document
        that failed to load. Such a document is missing from every other
        field, so a non-empty list means the rest is incomplete.
    """
    log.info("repo_health")
    svc = _svc()
    load_errors: list[DocLoadError] = []
    result = svc.repo_health(load_errors=load_errors)
    result["load_errors"] = [load_error_to_dict(e, svc.repo_root) for e in load_errors]
    return result


@mcp.tool()
def suspects(doc_id: str | None = None) -> list[dict]:
    """Scan outgoing links for OK / SUSPECT / BROKEN state."""
    log.info("suspects doc_id=%s", doc_id)
    svc = _svc()
    if doc_id is None:
        scoped = svc.scan_suspects(None)
    else:
        _require_doc(doc_id)
        scoped = svc.scan_suspects(doc_id)

    results: list[dict] = []
    for did, statuses in scoped.items():
        for s in statuses:
            if s.state.value == "ok":
                continue
            d = link_status_to_dict(s)
            d["doc_id"] = did
            results.append(d)
    return results


@mcp.tool()
def trace(
    doc_id: str,
    obj_id: str,
    direction: Literal["up", "down", "both"] = "both",
    max_depth: int | None = 2,
) -> dict:
    """Recursive up/down traceability for an object.

    `max_depth` defaults to 2 (the object's direct neighbors plus one more
    level) to keep responses compact for agents. Pass `max_depth=None` for an
    unbounded walk — useful when chasing a deep V-model chain end-to-end.
    """
    log.info(
        "trace %s/%s direction=%s max_depth=%s", doc_id, obj_id, direction, max_depth
    )
    _require_doc(doc_id)
    svc = _svc()
    # Validate obj exists up front so we raise a ValueError (wire compat).
    doc = svc.get_document(doc_id)
    if obj_id not in doc.objects:
        raise ValueError(f"No object {obj_id!r} in document {doc_id!r}")

    result: dict[str, Any] = {"doc_id": doc_id, "obj_id": obj_id}
    if direction in ("up", "both"):
        up_edges = [trace_edge_to_dict(e) for e in svc.trace_up(doc_id, obj_id)]
        if max_depth is not None:
            up_edges = _trim_tree(up_edges, max_depth - 1)
        result["up"] = up_edges
    if direction in ("down", "both"):
        down_edges = [trace_edge_to_dict(e) for e in svc.trace_down(doc_id, obj_id)]
        if max_depth is not None:
            down_edges = _trim_tree(down_edges, max_depth - 1)
        result["down"] = down_edges
    return result


@mcp.tool()
def incoming_links(doc_id: str, obj_id: str) -> list[dict]:
    """Every link across the repo that points AT (doc_id, obj_id)."""
    log.info("incoming_links %s %s", doc_id, obj_id)
    _require_doc(doc_id)
    return [
        {"source_doc": src_doc, "source_id": src_id, "link_type": link.type}
        for src_doc, src_id, link in _svc().incoming_links(doc_id, obj_id)
    ]


@mcp.tool()
def coverage_report(
    source_doc: str,
    target_doc: str,
    direction: Literal["incoming", "outgoing"] = "incoming",
    link_type: str | None = None,
    include_headings: bool = False,
    transitive: bool = False,
    include_lists: bool = False,
) -> dict:
    """Coverage of `source_doc` against `target_doc`.

    `direction="incoming"` (default, V-model verification): an object in
    `source_doc` is covered iff at least one object in `target_doc` has an
    outgoing link pointing at it. Use this to ask "which SRS reqs have an
    STS test?".

    `direction="outgoing"`: an object in `source_doc` is covered iff it has
    an outgoing link to `target_doc` itself. Use this to ask "which SRS reqs
    are justified by a SyRS req?".

    `transitive=True` follows the trace chain across intermediate documents
    instead of counting only one-hop links. Headings (`type=Heading`) are
    excluded from the denominator unless `include_headings=True`.

    The response always carries totals and `coverage_percent`. The per-object
    `uncovered` and `covered_objects` lists are returned only when
    `include_lists=True` — keep this False unless you specifically need to
    enumerate which objects passed or failed.
    """
    log.info(
        "coverage_report %s/%s direction=%s link_type=%s transitive=%s include_lists=%s",
        source_doc, target_doc, direction, link_type, transitive, include_lists,
    )
    _require_doc(source_doc)
    _require_doc(target_doc)
    try:
        report = _svc().coverage_report(
            source_doc,
            target_doc,
            direction=direction,
            link_type=link_type,
            include_headings=include_headings,
            transitive=transitive,
        )
    except ServiceError as e:
        raise ValueError(e.message) from e
    return coverage_report_to_dict(report, include_lists=include_lists)


# --- Write tools -----------------------------------------------------------


@mcp.tool()
def new_object(
    doc_id: str,
    parent_id: str | None = None,
    description: str = "",
    attrs: dict[str, str] | None = None,
    links: list[dict] | None = None,
    commit_message: str | None = None,
) -> dict:
    """Create a new requirement object in `doc_id`, optionally fully populated.

    Pass `attrs={"type": "Functional", "priority": "Must", ...}` to set
    attributes in the same call, and `links=[{"target_doc": "...",
    "target_id": "...", "type": "satisfies"}, ...]` to wire outgoing links.

    Schema mismatches (unknown attr, bad enum value, disallowed target doc)
    and bad links (missing target object, the same target twice) are caught
    before any file is written. If an attr or link write fails mid-batch,
    the response is `ok=False` with a `partial` block listing what landed
    before the failure; `error_type="OSError"` means an I/O failure.

    The success response includes the populated `object` dict so the agent
    has zero follow-up `show_object` calls. Pass `commit_message="..."` to
    auto-commit the new object + manifest + links files with the standard
    `Via: mellon-mcp` trailer.
    """
    _guard_write()
    log.info(
        "new_object %s parent=%s attrs=%d links=%d",
        doc_id, parent_id, len(attrs or {}), len(links or []),
    )
    _require_doc(doc_id)

    if not attrs and not links:
        # Fast path: simple create. Keeps the legacy result shape identical
        # for callers that pass only doc_id/parent_id/description.
        folder = _require_doc(doc_id)
        try:
            result = _svc().create_object(
                doc_id, parent_id=parent_id, description=description
            )
        except ServiceError as e:
            return _service_error_result(
                e, message=f"failed to create object in {doc_id}"
            )
        new_id = result.value
        affected = [
            _rel(folder / "objects" / f"{new_id}.xml"),
            _rel(folder / f"{doc_id}.xml"),
        ]
        new_obj = result.refreshed.objects.get(new_id)
        return _auto_commit_if_requested(commit_message, _write_result(
            affected=affected,
            message=f"created {new_id}",
            new_id=new_id,
            object=object_to_dict(new_obj) if new_obj is not None else None,
        ))

    try:
        result = _svc().create_object_full(
            doc_id,
            parent_id=parent_id,
            description=description,
            attrs=attrs,
            links=links,
        )
    except ServiceError as e:
        out = _service_error_result(e, message=f"failed to create object in {doc_id}")
        partial = e.details.get("partial") if e.details else None
        if partial:
            out["partial"] = partial
        return out

    new_obj = result.refreshed.objects.get(result.obj_id)
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=_rels(result.affected_files),
        message=f"created {result.obj_id}",
        new_id=result.obj_id,
        object=object_to_dict(new_obj) if new_obj is not None else None,
        attrs_set=list(result.attrs_set),
        links_added=[
            {"target_doc": td, "target_id": ti, "type": lt}
            for (td, ti, lt) in result.links_added
        ],
    ))


@mcp.tool()
def set_attribute(
    doc_id: str,
    obj_id: str,
    name: str,
    value: str,
    commit_message: str | None = None,
) -> dict:
    """Set one attribute value on an object (operates on object data, not schema).

    For setting many attributes at once or several fields on the same object,
    use `update_object` or `bulk_set_attribute`. To add a new attribute to
    the document's schema, use `define_attribute`.

    Pass `commit_message="..."` to auto-commit the changed files with the
    standard `Via: mellon-mcp` trailer; the response then gains a
    `commit.short_hash` field (or `commit_error` on commit failure).
    """
    _guard_write()
    log.info("set_attribute %s %s %s=%s", doc_id, obj_id, name, value)
    folder = _require_doc(doc_id)
    try:
        _svc().set_attribute(doc_id, obj_id, name, value)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to set {name} on {obj_id}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / "objects" / f"{obj_id}.xml")],
        message=f"{obj_id}.{name} = {value!r}",
    ))


@mcp.tool()
def edit_description(
    doc_id: str,
    obj_id: str,
    text: str,
    commit_message: str | None = None,
) -> dict:
    """Replace the description text of an object.

    Pass `commit_message="..."` to auto-commit the changed file.
    """
    _guard_write()
    log.info("edit_description %s %s (%d chars)", doc_id, obj_id, len(text))
    folder = _require_doc(doc_id)
    try:
        _svc().set_description(doc_id, obj_id, text)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to set description on {obj_id}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / "objects" / f"{obj_id}.xml")],
        message=f"{obj_id} description updated ({len(text)} chars)",
    ))


@mcp.tool()
def update_object(
    doc_id: str,
    obj_id: str,
    description: str | None = None,
    attrs: dict[str, str] | None = None,
    add_links: list[dict] | None = None,
    remove_links: list[dict] | None = None,
    commit_message: str | None = None,
) -> dict:
    """Update an object's description, attributes, and links in one call.

    Pass `description=None` to leave it untouched; `description=""` blanks it.
    `attrs={"name": "value", ...}` sets values (use `""` to blank, matching
    `remove_attr_option`'s convention). `add_links` entries are
    `{target_doc, target_id, type}`; `remove_links` entries are
    `{target_doc, target_id}`. Removes are processed before adds, so a
    target appearing in both nets to "add".

    All-None calls are a no-op (`ok=True, affected_files=[]`). Schema
    mismatches (unknown attr, bad enum, disallowed target doc) and bad
    `add_links` (missing target object, the same target twice, a link that
    already exists) are caught before any file is touched. The success
    response carries the refreshed `object` so agents don't need a
    follow-up `show_object` call.

    Pass `commit_message="..."` to auto-commit every file the update
    touched (object + links + manifest) with the standard
    `Via: mellon-mcp` trailer.
    """
    _guard_write()
    log.info(
        "update_object %s/%s desc=%s attrs=%d +links=%d -links=%d",
        doc_id, obj_id,
        "yes" if description is not None else "no",
        len(attrs or {}), len(add_links or []), len(remove_links or []),
    )
    _require_doc(doc_id)
    try:
        result = _svc().update_object(
            doc_id, obj_id,
            description=description,
            attrs=attrs,
            add_links=add_links,
            remove_links=remove_links,
        )
    except ServiceError as e:
        out = _service_error_result(e, message=f"failed to update {obj_id}")
        partial = e.details.get("partial") if e.details else None
        if partial:
            out["partial"] = partial
        return out

    if (
        not result.description_changed
        and not result.attrs_set
        and not result.links_added
        and not result.links_removed
    ):
        return {
            "ok": True,
            "affected_files": [],
            "message": "no changes requested",
            "obj_id": obj_id,
        }

    refreshed_obj = result.refreshed.objects.get(obj_id)
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=_rels(result.affected_files),
        message=f"{obj_id} updated",
        obj_id=obj_id,
        object=object_to_dict(refreshed_obj) if refreshed_obj is not None else None,
        description_changed=result.description_changed,
        attrs_set=list(result.attrs_set),
        links_added=[
            {"target_doc": td, "target_id": ti, "type": lt}
            for (td, ti, lt) in result.links_added
        ],
        links_removed=[
            {"target_doc": td, "target_id": ti}
            for (td, ti) in result.links_removed
        ],
    ))


@mcp.tool()
def copy_object(
    doc_id: str,
    obj_id: str,
    parent_id: str | None = None,
    count: int = 1,
    copy_links: bool = True,
    copy_figure: bool = True,
    commit_message: str | None = None,
) -> dict:
    """Duplicate an existing object N times.

    Each copy gets a freshly-allocated id and lands as a sibling of the
    source — under `parent_id` if given, otherwise under the source's own
    parent (or at top-level if the source is top-level). Attributes are
    always copied; outgoing links and the attached SVG figure (if any) are
    copied based on `copy_links` / `copy_figure`.

    Useful for template-style authoring where the agent has a canonical
    requirement and wants 5 near-duplicates to specialise further (e.g.
    one per HV controller channel). Returns `new_ids: [...]` so the agent
    can update each copy with `update_object` without an extra lookup.
    Pass `commit_message="..."` to auto-commit the copies.
    """
    _guard_write()
    log.info(
        "copy_object %s %s count=%d copy_links=%s copy_figure=%s",
        doc_id, obj_id, count, copy_links, copy_figure,
    )
    folder = _require_doc(doc_id)
    try:
        result = _svc().copy_object(
            doc_id, obj_id,
            parent_id=parent_id, count=count,
            copy_links=copy_links, copy_figure=copy_figure,
        )
    except ServiceError as e:
        out = _service_error_result(e, message=f"failed to copy {obj_id}")
        partial = e.details.get("partial") if e.details else None
        if partial:
            out["partial"] = partial
        return out

    new_ids = result.value
    affected: list[str] = [_rel(folder / f"{doc_id}.xml")]
    for nid in new_ids:
        affected.append(_rel(folder / "objects" / f"{nid}.xml"))
        link_path = folder / "links" / f"{nid}.xml"
        if link_path.exists():
            affected.append(_rel(link_path))
        fig_path = folder / "figures" / f"{nid}.svg"
        if fig_path.exists():
            affected.append(_rel(fig_path))
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=affected,
        message=f"created {len(new_ids)} copy/copies of {obj_id}",
        new_ids=list(new_ids),
    ))


@mcp.tool()
def bulk_set_attribute(
    doc_id: str,
    obj_ids: list[str],
    name: str,
    value: str,
    commit_message: str | None = None,
) -> dict:
    """Set the same attribute value on every object in `obj_ids` (one call, N writes).

    The single biggest agent-throughput hole left after the composite tools.
    Use this for workflows like "mark these 30 reqs as Approved" instead of
    calling `update_object` 30 times. Pre-validates the attr name + value
    against the schema and confirms every `obj_id` exists before any file is
    touched; on validation failure the response is `ok=False` and no objects
    are changed. Mid-batch I/O failure (rare) surfaces `ok=False` with a
    `partial` block listing the objects that were already updated.

    Empty `obj_ids` is a no-op with `ok=True`. `affected_files` lists every
    object file written; `set_on` lists the obj ids actually updated. Pass
    `commit_message="..."` to auto-commit the batch.
    """
    _guard_write()
    log.info(
        "bulk_set_attribute %s n=%d %s=%r",
        doc_id, len(obj_ids), name, value,
    )
    folder = _require_doc(doc_id)
    try:
        result = _svc().bulk_set_attribute(doc_id, obj_ids, name, value)
    except ServiceError as e:
        out = _service_error_result(
            e, message=f"failed to bulk-set {name} on {doc_id}"
        )
        partial = e.details.get("partial") if e.details else None
        if partial:
            out["partial"] = partial
        return out

    set_on = result.value
    affected = [
        _rel(folder / "objects" / f"{obj_id}.xml") for obj_id in set_on
    ]
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=affected,
        message=(
            f"set {name}={value!r} on {len(set_on)} object(s)"
            if set_on else "no objects updated (empty obj_ids)"
        ),
        set_on=list(set_on),
    ))


@mcp.tool()
def set_figure(
    doc_id: str,
    obj_id: str,
    source_path: str,
    commit_message: str | None = None,
) -> dict:
    """Attach an SVG figure to an object.

    Copies `source_path` (an absolute or repo-relative path on the server's
    filesystem) into `<doc_id>/figures/<obj_id>.svg`, replacing any existing
    figure for that object. The source must be a well-formed SVG; PNGs and
    drawio's compressed `.drawio` format are rejected. Use `.drawio.svg`
    (drawio's hybrid export) to keep diagrams editable in drawio while still
    rendering as plain SVG everywhere else — the GUI's "Edit in draw.io"
    and the PDF export rely on that.

    Pass `commit_message="..."` to auto-commit the figure file.
    """
    _guard_write()
    log.info("set_figure %s %s <- %s", doc_id, obj_id, source_path)
    folder = _require_doc(doc_id)
    src = Path(source_path)
    if not src.is_absolute():
        src = _require_root() / src
    try:
        _svc().set_figure(doc_id, obj_id, src)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to attach figure to {obj_id}"
        )
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / "figures" / f"{obj_id}.svg")],
        message=f"attached figure to {obj_id} (figures/{obj_id}.svg)",
    ))


@mcp.tool()
def clear_figure(
    doc_id: str,
    obj_id: str,
    commit_message: str | None = None,
) -> dict:
    """Remove an object's figure. `ok=False` if the object had no figure."""
    _guard_write()
    log.info("clear_figure %s %s", doc_id, obj_id)
    folder = _require_doc(doc_id)
    fig_path = folder / "figures" / f"{obj_id}.svg"
    try:
        result = _svc().clear_figure(doc_id, obj_id)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to clear figure from {obj_id}"
        )
    if not result.value:
        return {
            "ok": False,
            "affected_files": [],
            "message": f"{obj_id} has no figure",
            "error": "no figure attached",
            "error_type": "NotFound",
        }
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(fig_path)],
        message=f"removed figure from {obj_id}",
    ))


def _table_from_dict(data: Any) -> Table:
    """Build a Table from the JSON shape accepted by `set_table`.

    Raises ValueError on a malformed shape (not on geometry — the editor owns
    the geometry check). A cell may be a bare string or an object with `text`
    plus optional integer `colspan`/`rowspan`.
    """
    if not isinstance(data, dict):
        raise ValueError("table must be an object with 'columns' and 'rows'")
    cols_in = data.get("columns", [])
    rows_in = data.get("rows", [])
    if not isinstance(cols_in, list) or not isinstance(rows_in, list):
        raise ValueError("table 'columns' and 'rows' must be lists")

    columns: list[TableColumn] = []
    for c in cols_in:
        if not isinstance(c, dict):
            raise ValueError("each column must be an object like {'align': 'left'}")
        columns.append(TableColumn(align=str(c.get("align", "left"))))

    rows: list[TableRow] = []
    for r in rows_in:
        if not isinstance(r, dict):
            raise ValueError("each row must be an object with a 'cells' list")
        cells_in = r.get("cells", [])
        if not isinstance(cells_in, list):
            raise ValueError("row 'cells' must be a list")
        cells: list[TableCell] = []
        for cell in cells_in:
            if isinstance(cell, str):
                cells.append(TableCell(text=cell))
                continue
            if not isinstance(cell, dict):
                raise ValueError("each cell must be a string or an object")
            colspan = cell.get("colspan", 1)
            rowspan = cell.get("rowspan", 1)
            if not isinstance(colspan, int) or not isinstance(rowspan, int):
                raise ValueError("cell 'colspan'/'rowspan' must be integers")
            cells.append(
                TableCell(text=str(cell.get("text", "")), colspan=colspan, rowspan=rowspan)
            )
        rows.append(TableRow(cells=tuple(cells), header=bool(r.get("header", False))))

    return Table(columns=tuple(columns), rows=tuple(rows))


@mcp.tool()
def set_table(
    doc_id: str,
    obj_id: str,
    table: dict,
    commit_message: str | None = None,
) -> dict:
    """Create or replace an object's structured table.

    `table` is `{"columns": [{"align": "left"|"center"|"right"}, ...], "rows":
    [{"header": false, "cells": [{"text": "...", "colspan": 1, "rowspan": 1},
    ...]}, ...]}`. A cell may also be a bare string for a plain unmerged cell;
    `colspan`/`rowspan` default to 1 and may be omitted.

    The table is validated for consistent geometry (every grid slot covered
    exactly once, nothing overflowing), valid alignments, and positive spans
    before anything is written. By convention the object's `type` is "Table"
    and its `description` is the caption, but neither is required.

    Pass `commit_message="..."` to auto-commit the object file.
    """
    _guard_write()
    log.info("set_table %s %s", doc_id, obj_id)
    folder = _require_doc(doc_id)
    try:
        built = _table_from_dict(table)
    except ValueError as e:
        return {
            "ok": False,
            "affected_files": [],
            "message": f"malformed table for {obj_id}",
            "error": str(e),
            "error_type": "EditorError",
        }
    try:
        _svc().set_table(doc_id, obj_id, built)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to set table on {obj_id}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / "objects" / f"{obj_id}.xml")],
        message=f"set table on {obj_id}",
    ))


@mcp.tool()
def clear_table(
    doc_id: str,
    obj_id: str,
    commit_message: str | None = None,
) -> dict:
    """Remove an object's table. `ok=False` if the object had no table."""
    _guard_write()
    log.info("clear_table %s %s", doc_id, obj_id)
    folder = _require_doc(doc_id)
    try:
        result = _svc().clear_table(doc_id, obj_id)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to clear table from {obj_id}"
        )
    if not result.value:
        return {
            "ok": False,
            "affected_files": [],
            "message": f"{obj_id} has no table",
            "error": "no table attached",
            "error_type": "NotFound",
        }
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / "objects" / f"{obj_id}.xml")],
        message=f"removed table from {obj_id}",
    ))


@mcp.tool()
def edit_metadata(
    doc_id: str,
    field: str,
    value: str,
    commit_message: str | None = None,
) -> dict:
    """Set one metadata field on the manifest.

    Valid fields: `title`, `author`, `date`. Issue is **not** a metadata
    field — it lives as a git tag (`<doc-id>-issue-<n>`); use `tag_issue`
    to bump it.
    """
    _guard_write()
    log.info("edit_metadata %s %s=%s", doc_id, field, value)
    folder = _require_doc(doc_id)
    try:
        _svc().set_metadata(doc_id, field, value)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to set {field} on {doc_id}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"{doc_id}.{field} = {value!r}",
    ))


@mcp.tool()
def tag_issue(doc_id: str, issue: str) -> dict:
    """Create the `<doc_id>-issue-<issue>` lightweight tag at HEAD.

    `issue` must be a dot-separated numeric sequence (e.g. `'1'`, `'1.2'`,
    `'12.23.54'`). Refuses if the tag already exists.
    """
    _guard_write()
    log.info("tag_issue %s issue=%s", doc_id, issue)
    _require_doc(doc_id)
    try:
        tag_name = _svc().tag_issue(doc_id, issue)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to tag {doc_id}-issue-{issue}"
        )
    return _write_result(
        affected=[],
        message=f"created tag {tag_name}",
        tag_name=tag_name,
    )


@mcp.tool()
def add_parent_doc(
    doc_id: str,
    parent_doc: str,
    commit_message: str | None = None,
) -> dict:
    """Declare `parent_doc` as a parent of `doc_id`."""
    _guard_write()
    log.info("add_parent_doc %s parent=%s", doc_id, parent_doc)
    folder = _require_doc(doc_id)
    try:
        _svc().add_parent(doc_id, parent_doc)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to add parent {parent_doc}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"{doc_id} now declares parent {parent_doc}",
    ))


@mcp.tool()
def remove_parent_doc(
    doc_id: str,
    parent_doc: str,
    commit_message: str | None = None,
) -> dict:
    """Remove a parent-doc declaration. `ok=False` if the parent wasn't declared."""
    _guard_write()
    log.info("remove_parent_doc %s parent=%s", doc_id, parent_doc)
    folder = _require_doc(doc_id)
    try:
        result = _svc().remove_parent(doc_id, parent_doc)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to remove parent {parent_doc}")

    if not result.value:
        return {
            "ok": False,
            "affected_files": [],
            "message": f"{doc_id} does not declare parent {parent_doc}",
            "error": "parent not declared",
            "error_type": "NotFound",
        }
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"{doc_id} no longer declares parent {parent_doc}",
    ))


@mcp.tool()
def add_attr_option(
    doc_id: str,
    attr_name: str,
    option: str,
    commit_message: str | None = None,
) -> dict:
    """Append a new enum option to an existing enum attribute."""
    _guard_write()
    log.info("add_attr_option %s %s += %s", doc_id, attr_name, option)
    folder = _require_doc(doc_id)
    try:
        _svc().add_attr_option(doc_id, attr_name, option)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to add option to {attr_name}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"{attr_name} now includes option {option!r}",
    ))


@mcp.tool()
def define_attribute(
    doc_id: str,
    name: str,
    kind: str,
    required: bool = False,
    options: list[str] | None = None,
    label: str = "",
    default_value: str | None = None,
    commit_message: str | None = None,
) -> dict:
    """Declare a new attribute on the document schema (operates on schema, not object data).

    `kind` must be one of `"enum"`, `"text"`, `"integer"`, `"real"`,
    `"boolean"`, or `"date"`. `options` is required for `kind=enum` and must
    be empty/omitted for every other kind. Non-string kinds are stored as
    canonical strings (`"true"`/`"false"`, base-10 integers, ISO 8601 dates)
    and type-checked by `validate_document`. `label` is an optional
    human-readable display name shown in the UI.

    By default existing objects are not modified; if `required=True` they
    fail validation until the attribute is set. Pass `default_value=…` to
    backfill the value on every existing object in one call — the response
    lists the affected ids in `backfilled`. To later change an object's
    attribute value, use `set_attribute`.
    """
    _guard_write()
    log.info(
        "define_attribute %s %s (%s) default_value=%s",
        doc_id, name, kind,
        "set" if default_value is not None else "none",
    )
    folder = _require_doc(doc_id)
    opts = tuple(options or ())
    try:
        result = _svc().add_attribute(
            doc_id, name, kind, required, opts, label, default_value=default_value
        )
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to define attribute {name!r}"
        )
    qualifier = "required" if required else "optional"
    affected = [_rel(folder / f"{doc_id}.xml")]
    backfilled: list[str] = []
    if default_value is not None:
        backfilled = list(result.value or [])
        for obj_id in backfilled:
            affected.append(_rel(folder / "objects" / f"{obj_id}.xml"))
    out_kwargs: dict[str, Any] = {}
    if default_value is not None:
        out_kwargs["backfilled"] = backfilled
        message = (
            f"defined attribute {name} ({kind}, {qualifier}); "
            f"backfilled {len(backfilled)} object(s) with default {default_value!r}"
        )
    else:
        message = f"defined attribute {name} ({kind}, {qualifier})"
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=affected,
        message=message,
        **out_kwargs,
    ))


@mcp.tool()
def change_attribute_required(
    doc_id: str,
    name: str,
    required: bool,
    commit_message: str | None = None,
) -> dict:
    """Flip the `required` flag on an existing attribute.

    Idempotent. Flipping `required=true` does not modify existing objects;
    those that lack the attribute will surface as `ATTR_MISSING_REQUIRED`
    on the next `validate_document`.
    """
    _guard_write()
    log.info("change_attribute_required %s %s -> %s", doc_id, name, required)
    folder = _require_doc(doc_id)
    try:
        _svc().change_attribute_required(doc_id, name, required)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to change required on {name!r}"
        )
    qualifier = "required" if required else "optional"
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"{name} is now {qualifier}",
    ))


@mcp.tool()
def delete_attribute(
    doc_id: str,
    name: str,
    commit_message: str | None = None,
) -> dict:
    """Delete an attribute and cascade-strip it from every object and rule.

    Silent cascade: object files lose the matching `<attr>` element and rules
    lose the matching directive (rules that become empty are dropped).
    `affected_files` lists the manifest plus every object and rules file the
    cascade rewrote, so `commit_message="..."` commits all of them.
    """
    _guard_write()
    log.info("delete_attribute %s %s", doc_id, name)
    _require_doc(doc_id)
    try:
        result = _svc().delete_attribute(doc_id, name)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to delete attribute {name!r}"
        )
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=_rels(result.value),
        message=f"deleted attribute {name} (cascaded to objects and rules)",
    ))


@mcp.tool()
def remove_attr_option(
    doc_id: str,
    attr_name: str,
    option: str,
    commit_message: str | None = None,
) -> dict:
    """Remove an enum option; blanks (but keeps) matching object attributes.

    Objects whose `<attr>` text equals the removed option have the text
    cleared so the user can see the now-blank value rather than silently lose
    it. The validator will surface any required-but-empty cases.
    `affected_files` lists the manifest plus every blanked object file.
    """
    _guard_write()
    log.info("remove_attr_option %s %s -= %s", doc_id, attr_name, option)
    _require_doc(doc_id)
    try:
        result = _svc().remove_attr_option(doc_id, attr_name, option)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to remove option from {attr_name}"
        )
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=_rels(result.value),
        message=f"{attr_name} no longer includes option {option!r}",
    ))


@mcp.tool()
def manage_view(
    doc_id: str,
    action: Literal["create", "rename", "delete", "edit"],
    slug: str | None = None,
    name: str | None = None,
    visible_attrs: list[str] | None = None,
    commit_message: str | None = None,
) -> dict:
    """Create, rename, delete, or edit a saved grid view in one tool.

    Views are named selections of user-attribute columns persisted at
    `<doc_id>/views/<slug>.xml`. System columns (id, description, etc.) are
    always shown regardless of `visible_attrs`.

    Per-action required parameters:
      - `action="create"`: `name` (required), `visible_attrs` (optional).
        Returns the auto-generated slug.
      - `action="rename"`: `slug` + `name` (both required). If `name`
        slugifies to the existing slug, only the display name is updated;
        otherwise the file is moved (with `-2`/`-3`… on collision). Returns
        the (possibly new) slug.
      - `action="delete"`: `slug` (required).
      - `action="edit"`: `slug` + `visible_attrs` (both required). Replaces
        the view's column list, preserving order; duplicates dropped.

    Missing required params for the chosen action surface as
    `error_type: "EditorError"` with a clear message; agents can branch on
    `action` before calling.
    """
    _guard_write()
    log.info("manage_view %s action=%s slug=%s", doc_id, action, slug)
    folder = _require_doc(doc_id)
    svc = _svc()

    if action == "create":
        if not name:
            return {
                "ok": False, "affected_files": [], "message": "create requires `name`",
                "error": "create requires `name`", "error_type": "EditorError",
            }
        try:
            result = svc.create_view(doc_id, name, tuple(visible_attrs or ()))
        except ServiceError as e:
            return _service_error_result(e, message=f"failed to create view {name!r}")
        new_slug = result.value
        return _auto_commit_if_requested(commit_message, _write_result(
            affected=[_rel(folder / "views" / f"{new_slug}.xml")],
            message=f"created view {new_slug!r} ({name!r})",
            slug=new_slug,
        ))

    if action == "rename":
        if not slug or not name:
            return {
                "ok": False, "affected_files": [], "message": "rename requires `slug` and `name`",
                "error": "rename requires `slug` and `name`", "error_type": "EditorError",
            }
        old_path = folder / "views" / f"{slug}.xml"
        try:
            result = svc.rename_view(doc_id, slug, name)
        except ServiceError as e:
            return _service_error_result(
                e, message=f"failed to rename view {slug!r}"
            )
        new_slug = result.value
        new_path = folder / "views" / f"{new_slug}.xml"
        affected = [_rel(new_path)]
        if new_slug != slug:
            affected.append(_rel(old_path))
        return _auto_commit_if_requested(commit_message, _write_result(
            affected=affected,
            message=(
                f"renamed view {slug!r} to {new_slug!r} ({name!r})"
                if new_slug != slug
                else f"renamed view {slug!r} to {name!r}"
            ),
            slug=new_slug,
        ))

    if action == "delete":
        if not slug:
            return {
                "ok": False, "affected_files": [], "message": "delete requires `slug`",
                "error": "delete requires `slug`", "error_type": "EditorError",
            }
        path = folder / "views" / f"{slug}.xml"
        try:
            svc.delete_view(doc_id, slug)
        except ServiceError as e:
            return _service_error_result(e, message=f"failed to delete view {slug!r}")
        return _auto_commit_if_requested(commit_message, _write_result(
            affected=[_rel(path)],
            message=f"deleted view {slug!r}",
        ))

    if action == "edit":
        if not slug or visible_attrs is None:
            return {
                "ok": False, "affected_files": [],
                "message": "edit requires `slug` and `visible_attrs`",
                "error": "edit requires `slug` and `visible_attrs`",
                "error_type": "EditorError",
            }
        path = folder / "views" / f"{slug}.xml"
        try:
            svc.set_view_visible(doc_id, slug, tuple(visible_attrs))
        except ServiceError as e:
            return _service_error_result(
                e, message=f"failed to edit view {slug!r}"
            )
        return _auto_commit_if_requested(commit_message, _write_result(
            affected=[_rel(path)],
            message=f"updated visible attributes of view {slug!r}",
            slug=slug,
        ))

    return {
        "ok": False, "affected_files": [],
        "message": f"unknown manage_view action: {action!r}",
        "error": f"unknown action {action!r}; valid: create/rename/delete/edit",
        "error_type": "EditorError",
    }


@mcp.tool()
def create_document(
    doc_id: str,
    title: str = "",
    parent_docs: list[str] | None = None,
    attr_schema: list[dict] | None = None,
    commit_message: str | None = None,
) -> dict:
    """Create a new document (folder + manifest) in the repo.

    `doc_id` must be unique and contain only letters, digits, hyphens, and
    underscores (e.g. "VCU3-HRS"). `parent_docs` lists upstream document IDs
    this document derives requirements from. `attr_schema` defines the
    attribute columns; each entry is
      {"name": str, "kind": "enum"|"text", "required": bool, "options": [str]}
    where `options` is required for kind "enum". Attributes can also be added
    later via `define_attribute`. Without `attr_schema`, a default schema is
    used and a `<doc_id>.rules.xml` is written next to the manifest;
    `affected_files` lists every file created.

    Unlike `list_documents` / `show_document`, the `summary` field on the
    response always carries the full fat shape (metadata + attributes +
    parents + outline + views + filters): an agent just created the doc and
    almost certainly wants to see its final shape without a follow-up call.
    """
    _guard_write()
    log.info("create_document %s", doc_id)
    root = _require_root()
    folder = root / doc_id
    try:
        result = _svc().create_document(
            doc_id, title=title, parents=parent_docs, attr_schema=attr_schema
        )
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to create document {doc_id!r}")
    rel_folder = folder.relative_to(root).as_posix()
    affected = [folder / f"{doc_id}.xml"]
    # Written alongside the manifest when no attr_schema is given.
    rules = folder / f"{doc_id}.rules.xml"
    if rules.is_file():
        affected.append(rules)
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=_rels(affected),
        message=f"created document {doc_id!r}",
        doc_id=doc_id,
        folder=rel_folder,
        summary=document_summary_to_dict(
            result.refreshed, folder=rel_folder, include=frozenset({"*"})
        ),
    ))


@mcp.tool()
def move_object(
    doc_id: str,
    obj_id: str,
    parent_id: str | None = None,
    to_top: bool = False,
    before: str | None = None,
    after: str | None = None,
    commit_message: str | None = None,
) -> dict:
    """Reparent and/or reorder an object in the outline."""
    _guard_write()
    log.info(
        "move_object %s %s parent=%s to_top=%s before=%s after=%s",
        doc_id, obj_id, parent_id, to_top, before, after,
    )
    folder = _require_doc(doc_id)
    try:
        _svc().move_object(
            doc_id, obj_id,
            parent_id=parent_id, to_top=to_top, before=before, after=after,
        )
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to move {obj_id}")
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"{obj_id} moved",
    ))


@mcp.tool()
def reorder_outline(
    doc_id: str,
    ordering: list[dict],
    commit_message: str | None = None,
) -> dict:
    """Rewrite a document's outline in one call.

    `ordering` mirrors the shape `show_document(include=["outline"])` returns:
    a list of `{"id": str, "children": [...]}` dicts (recursive). The new
    outline must reference every object in the document exactly once —
    missing or extra ids fail validation before any file is written.

    Use this for declarative restructuring (move 10 objects to a new
    parent, swap two subtrees, etc.) instead of looping `move_object`.
    A typical workflow:
      1. `out = show_document(doc_id, include=["outline"])["outline"]`
      2. mutate `out` client-side
      3. `reorder_outline(doc_id, out)`
    """
    _guard_write()
    log.info("reorder_outline %s (%d top-level)", doc_id, len(ordering))
    folder = _require_doc(doc_id)
    try:
        result = _svc().set_outline(doc_id, ordering)
    except ServiceError as e:
        return _service_error_result(
            e, message=f"failed to reorder outline of {doc_id}"
        )
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / f"{doc_id}.xml")],
        message=f"outline reordered ({result.value} object(s))",
    ))


@mcp.tool()
def add_link(
    source_doc: str,
    source_id: str,
    target_doc: str,
    target_id: str,
    link_type: str,
    commit_message: str | None = None,
) -> dict:
    """Add an outgoing link from (source_doc, source_id) to (target_doc, target_id)."""
    _guard_write()
    log.info(
        "add_link %s/%s -> %s/%s (%s)",
        source_doc, source_id, target_doc, target_id, link_type,
    )
    folder = _require_doc(source_doc)
    try:
        result = _svc().add_link(source_doc, source_id, target_doc, target_id, link_type)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to add link from {source_id}")
    ack = result.value
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=[_rel(folder / "links" / f"{source_id}.xml")],
        message=(
            f"{source_id} --[{link_type}]--> {target_doc}/{target_id} (ack={ack})"
            if ack else
            f"{source_id} --[{link_type}]--> {target_doc}/{target_id} (never reviewed)"
        ),
        ack=ack,
    ))


@mcp.tool()
def remove_link(
    source_doc: str,
    source_id: str,
    target_doc: str,
    target_id: str,
    commit_message: str | None = None,
) -> dict:
    """Remove an outgoing link. `ok=False` if the link didn't exist."""
    _guard_write()
    log.info(
        "remove_link %s/%s -> %s/%s",
        source_doc, source_id, target_doc, target_id,
    )
    folder = _require_doc(source_doc)
    links_path = folder / "links" / f"{source_id}.xml"
    existed_before = links_path.exists()
    try:
        result = _svc().remove_link(source_doc, source_id, target_doc, target_id)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to remove link from {source_id}")

    if not result.value:
        return {
            "ok": False,
            "affected_files": [],
            "message": f"no link from {source_id} to {target_doc}/{target_id}",
            "error": "link not found",
            "error_type": "NotFound",
        }
    affected = [_rel(links_path)] if existed_before else []
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=affected,
        message=f"removed {source_id} -> {target_doc}/{target_id}",
        links_file_deleted=not links_path.exists(),
    ))


@mcp.tool()
def delete_object(
    doc_id: str,
    obj_id: str,
    force: bool = False,
    commit_message: str | None = None,
) -> dict:
    """Hard-delete an object."""
    _guard_write()
    log.info("delete_object %s %s force=%s", doc_id, obj_id, force)
    _require_doc(doc_id)
    try:
        result = _svc().delete_object(doc_id, obj_id, force=force)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to delete {obj_id}")

    report = result.report
    incoming_summary = [
        {"source_doc": s_doc, "source_id": s_id, "link_type": ltype}
        for s_doc, s_id, ltype in report.incoming
    ]
    if not report.performed:
        return {
            "ok": False,
            "affected_files": [],
            "message": (
                f"{obj_id} has {len(incoming_summary)} incoming link(s); "
                "re-run with force=True to cascade-strip them"
            ),
            "error": "blocked by incoming links",
            "error_type": "BlockedByIncomingLinks",
            "incoming": incoming_summary,
        }
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=_rels(list(report.removed_files) + list(report.modified_files)),
        message=f"deleted {obj_id}",
        incoming_stripped=incoming_summary,
        removed_files=_rels(report.removed_files),
        modified_files=_rels(report.modified_files),
    ))


@mcp.tool()
def ack_suspects(
    doc_id: str,
    dry_run: bool = False,
    link_keys: list[dict] | None = None,
    commit_message: str | None = None,
) -> dict:
    """Acknowledge SUSPECT links in `doc_id` against their current target hashes.

    By default (`link_keys=None`) every SUSPECT link in the document is acked.
    Pass `link_keys=[{"source_id": "...", "target_doc": "...", "target_id": "..."}, ...]`
    to ack only that subset — useful when the agent has reviewed a handful of
    suspects individually rather than the whole doc.

    The response includes:
      - `acknowledged`: links that were re-acked
      - `skipped`: requested keys that matched no SUSPECT link (already OK,
        missing, or broken), each with a `reason`

    When `dry_run=True`, no files are written and `would_acknowledge` is
    returned instead of `acknowledged`.
    """
    _guard_write()
    log.info(
        "ack_suspects %s dry_run=%s keys=%s",
        doc_id, dry_run, len(link_keys) if link_keys is not None else "all",
    )
    folder = _require_doc(doc_id)
    svc = _svc()
    try:
        scoped = svc.scan_suspects(doc_id)
    except ServiceError as e:
        if e.code is ErrorCode.GIT:
            return _service_error_result(e, message=f"ack requires a git repo ({doc_id})")
        return _service_error_result(e, message=f"failed to scan suspects for {doc_id}")

    statuses = scoped[doc_id]
    ackable = [
        s for s in statuses
        if s.state.value == "suspect" and s.current_hash is not None
    ]

    skipped: list[dict] = []
    if link_keys is None:
        suspect = ackable
    else:
        ackable_by_key = {
            (s.source_id, s.link.target_doc, s.link.target_id): s for s in ackable
        }
        all_by_key = {
            (s.source_id, s.link.target_doc, s.link.target_id): s for s in statuses
        }
        suspect = []
        seen: set[tuple[str, str, str]] = set()
        for raw in link_keys:
            key = (
                raw.get("source_id", ""),
                raw.get("target_doc", ""),
                raw.get("target_id", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            if key in ackable_by_key:
                suspect.append(ackable_by_key[key])
            elif key in all_by_key:
                existing = all_by_key[key]
                skipped.append({
                    "key": {
                        "source_id": key[0],
                        "target_doc": key[1],
                        "target_id": key[2],
                    },
                    "reason": (
                        f"link state is {existing.state.value!r}, not suspect"
                        if existing.state.value != "suspect"
                        else "no current_hash available (broken target)"
                    ),
                })
            else:
                skipped.append({
                    "key": {
                        "source_id": key[0],
                        "target_doc": key[1],
                        "target_id": key[2],
                    },
                    "reason": "no such outgoing link on this object",
                })

    if dry_run:
        return {
            "ok": True,
            "affected_files": [],
            "message": (
                f"would acknowledge {len(suspect)} link(s) "
                f"(dry run — no files written)"
            ),
            "would_acknowledge": [link_status_to_dict(s) for s in suspect],
            "skipped": skipped,
        }
    try:
        result = svc.ack_all(doc_id, suspect)
    except ServiceError as e:
        return _service_error_result(e, message=f"failed to ack {doc_id}")
    rewritten = result.value
    affected = [_rel(folder / "links" / f"{s.source_id}.xml") for s in rewritten]
    return _auto_commit_if_requested(commit_message, _write_result(
        affected=affected,
        message=f"acknowledged {len(rewritten)} suspect link(s)",
        acknowledged=[link_status_to_dict(s) for s in rewritten],
        skipped=skipped,
    ))


# --- Git / review tools ----------------------------------------------------


_COMMIT_TRAILER = "Via: mellon-mcp"


def _with_trailer(message: str) -> str:
    """Append the standard `Via: mellon-mcp` trailer to a commit message."""
    stripped = message.rstrip()
    sep = "\n\n" if "\n\n" in stripped or "\n" not in stripped else "\n"
    return f"{stripped}{sep}{_COMMIT_TRAILER}\n"


def _auto_commit_if_requested(commit_message: str | None, result: dict) -> dict:
    """Commit affected_files with the standard trailer when commit_message is set.

    Opt-in per mutation tool. On commit success the result gains a `commit`
    field with `short_hash` and `commit_message`. On commit failure (e.g.
    repo isn't a git repo, nothing to commit) the result gains a
    `commit_error` field — the original write is never rolled back.

    No-op when `commit_message` is None/empty, the write didn't succeed
    (`ok=False`), or no files changed (`affected_files=[]`). The empty-paths
    case is a no-op rather than an error because some mutation tools return
    `ok=True` with empty `affected_files` for "no-op" requests (e.g. the
    all-None `update_object` case).
    """
    if not commit_message or not result.get("ok"):
        return result
    paths = result.get("affected_files") or []
    if not paths:
        return result
    full_message = _with_trailer(commit_message)
    try:
        cr = _svc().commit(full_message, paths=paths)
        result["commit"] = {
            "short_hash": cr.short_hash,
            "commit_message": full_message,
        }
    except ServiceError as e:
        result["commit_error"] = {
            "error": e.message,
            "error_type": _ERROR_TYPE_BY_CODE.get(e.code, "GitError"),
        }
    return result


def _parse_object_xml(xml_text: str) -> dict:
    """Extract id / attrs / description from an object-file XML blob."""
    root = etree.fromstring(xml_text.encode("utf-8"))
    return {
        "id": root.get("id", ""),
        "attrs": {a.get("name"): (a.text or "") for a in root.findall("attr")},
        "description": (
            root.find("description").text
            if root.find("description") is not None
            and root.find("description").text is not None
            else ""
        ),
    }


@mcp.tool()
def commit_changes(
    message: str,
    paths: list[str] | None = None,
    include_trailer: bool = True,
) -> dict:
    """Commit pending changes on HEAD.

    With no `paths`, commits every uncommitted change inside the document
    folders (nothing else in the git work tree). Refused while a merge is in
    progress or when git user.name/user.email are not configured.
    """
    _guard_write()
    log.info(
        "commit_changes paths=%s message=%r",
        paths,
        message[:60] + ("…" if len(message) > 60 else ""),
    )
    full_message = _with_trailer(message) if include_trailer else message
    try:
        result = _svc().commit(full_message, paths=paths)
    except ServiceError as e:
        return _service_error_result(e, message="commit failed")
    return {
        "ok": True,
        "affected_files": [],
        "message": f"committed {result.short_hash}",
        "short_hash": result.short_hash,
        "commit_message": full_message,
    }


@mcp.tool()
def git_history(doc_id: str, obj_id: str, max_entries: int = 10) -> list[dict]:
    """Recent commits on HEAD's history that changed this object's file or
    its figure (`figures/<obj_id>.svg`)."""
    log.info("git_history %s %s max=%d", doc_id, obj_id, max_entries)
    folder = _require_doc(doc_id)
    try:
        repo, root = discover_repo(folder)
    except GitError:
        return []
    rel = f"{doc_id}/objects/{obj_id}.xml"
    if not (root / rel).exists():
        raise ValueError(f"No object {obj_id!r} in document {doc_id!r}")
    try:
        return git_ops.log_for_path(
            repo, [rel, _figure_rel(doc_id, obj_id)], max_entries=max_entries
        )
    except GitError:
        return []


@mcp.tool()
def show_object_at(doc_id: str, obj_id: str, commit_ref: str) -> dict:
    """Parsed object as it looked at `commit_ref`."""
    log.info("show_object_at %s %s @ %s", doc_id, obj_id, commit_ref)
    folder = _require_doc(doc_id)
    try:
        repo, _ = discover_repo(folder)
    except GitError as e:
        raise ValueError(f"{doc_id} is not inside a git repo") from e
    rel = f"{doc_id}/objects/{obj_id}.xml"
    xml = git_ops.show_blob_at(repo, rel, commit_ref)
    parsed = _parse_object_xml(xml)
    import pygit2
    resolved = repo.revparse_single(commit_ref).peel(pygit2.Commit)
    parsed["commit"] = str(resolved.id)[:7]
    return parsed


@mcp.tool()
def review_suspect_link(
    source_doc: str,
    source_id: str,
    target_doc: str,
    target_id: str,
) -> dict:
    """Collapse the suspect-review flow into a single agent-facing call.

    `commits_between` lists the parent's commits since the ack, including
    ones that changed only its figure; for those the parent text before and
    now is the same.
    """
    log.info(
        "review_suspect_link %s/%s -> %s/%s",
        source_doc, source_id, target_doc, target_id,
    )
    folder = _require_doc(source_doc)
    svc = _svc()
    doc = svc.get_document(source_doc)
    links = doc.links.get(source_id, ())
    link = next(
        (
            l for l in links
            if l.target_doc == target_doc and l.target_id == target_id
        ),
        None,
    )
    if link is None:
        raise ValueError(
            f"No link from {source_id} to {target_doc}/{target_id} in {source_doc}"
        )

    try:
        scoped = svc.scan_suspects(source_doc)
    except ServiceError as e:
        if e.code is ErrorCode.GIT:
            raise ValueError(f"review requires a git repo ({source_doc})") from e
        raise
    statuses = scoped[source_doc]
    status = next(
        (
            s for s in statuses
            if s.source_id == source_id and s.link == link
        ),
        None,
    )
    state = status.state.value if status is not None else "unknown"
    reason = status.reason if status is not None else ""
    current_commit = status.current_hash if status is not None else None

    repo, root = discover_repo(folder)
    target_rel = f"{target_doc}/objects/{target_id}.xml"
    target_path = root / target_rel

    parent_text_now: str | None = None
    if target_path.is_file():
        try:
            parent_text_now = _parse_object_xml(
                target_path.read_text(encoding="utf-8")
            )["description"]
        except etree.XMLSyntaxError:
            parent_text_now = None

    acked = link.ack
    parent_text_at_ack: str | None = None
    if acked is not None:
        try:
            xml_at_ack = git_ops.show_blob_at(repo, target_rel, acked)
            parent_text_at_ack = _parse_object_xml(xml_at_ack)["description"]
        except (GitError, etree.XMLSyntaxError):
            parent_text_at_ack = None

    commits_between: list[dict] = []
    if (
        acked is not None
        and current_commit is not None
        and acked != current_commit
    ):
        try:
            # A figure-only revision makes the link suspect too, so list it.
            commits_between = git_ops.log_for_path(
                repo,
                [target_rel, _figure_rel(target_doc, target_id)],
                max_entries=20,
                since_commit=acked,
            )
        except GitError:
            commits_between = []

    return {
        "source_doc": source_doc,
        "source_id": source_id,
        "target_doc": target_doc,
        "target_id": target_id,
        "link_type": link.type,
        "state": state,
        "reason": reason,
        "parent_text_at_ack": parent_text_at_ack,
        "parent_text_now": parent_text_now,
        "acked_at_commit": acked,
        "current_commit": current_commit,
        "commits_between": commits_between,
    }


@mcp.tool()
def export_document(
    doc_id: str,
    out_path: str,
    attrs: list[str] | None = None,
    rev: str | None = None,
) -> dict:
    """Render a document to PDF (LaTeX via tectonic) and write it to disk.

    `out_path` is the destination file; it must end in `.pdf`, must not be
    inside the repo's `.git` directory, and relative paths resolve against
    the repo root. This tool writes to a filesystem path, so `--read-only`
    refuses it.

    `rev` labels the cover page: omit it to use the document's current issue
    tag, pass an explicit string to override, or pass "" to suppress the rev
    entirely. `attrs` narrows which attributes print under each requirement
    (default: type, status, priority, verification-method).

    Needs the `tectonic` binary, looked up as: the `MELLON_TECTONIC`
    environment variable, then `<exe-dir>/tools/` in the installed (frozen)
    build, then PATH, then `<project-root>/tools/` in a source checkout. A
    missing binary surfaces as `error_type: "TectonicError"`, not a crash.
    The PDF is not repo content — nothing is staged or committed.

    Attached figures are drawn by draw.io desktop (found via the
    `MELLON_DRAWIO` environment variable, its standard install folders, then
    PATH) and print as numbered figures captioned with the object's
    description. A figure that couldn't be rendered — draw.io missing, or an
    empty diagram — prints a placeholder line instead, and the result lists
    those object ids in `unrendered_figures` (plus `drawio_found: false`
    when draw.io wasn't found).

    A document that declares parents ends with a Traceability section: a
    link table of each object's parent objects, then the parent objects
    nothing links to. The PDF shows no link states; the document's suspect
    and broken outgoing links are checked first and listed in
    `suspect_links` (as in `suspects`), but don't stop the export.
    """
    _guard_write()
    log.info("export_document %s -> %s rev=%r", doc_id, out_path, rev)
    folder = _require_doc(doc_id)
    svc = _svc()
    try:
        doc = svc.get_document(doc_id)
    except ServiceError as e:
        return _service_error_result(e, message="export failed")

    if rev is None:
        rev_used: str | None = svc.current_issue(doc_id) or None
    else:
        rev_used = rev or None

    out = Path(out_path)
    if not out.is_absolute():
        out = _require_root() / out
    if out.suffix.lower() != ".pdf":
        return _service_error_result(
            ServiceError(
                ErrorCode.VALIDATION, f"out_path must end in .pdf: {out_path!r}"
            ),
            message="export failed",
        )
    if out.resolve().is_relative_to((_require_root() / ".git").resolve()):
        return _service_error_result(
            ServiceError(
                ErrorCode.VALIDATION,
                f"out_path must not be inside the repo's .git directory: {out_path!r}",
            ),
            message="export failed",
        )

    missing: list[str] = []
    try:
        parents = svc.parent_documents(doc_id)
    except ServiceError as e:
        return _service_error_result(e, message="export failed")
    # The PDF shows no link states: report suspect / broken links instead.
    try:
        problems = [
            s for s in svc.scan_suspects(doc_id)[doc_id] if s.state.value != "ok"
        ]
    except ServiceError:
        problems = []  # no git: nothing to check against
    try:
        if attrs is not None:
            blob = export_pdf(
                doc, folder=folder, attrs=tuple(attrs), rev=rev_used,
                unrendered=missing, parents=parents,
            )
        else:
            blob = export_pdf(
                doc, folder=folder, rev=rev_used, unrendered=missing,
                parents=parents,
            )
    except TectonicError as e:
        return {
            "ok": False,
            "affected_files": [],
            "message": "export failed",
            "error": str(e),
            "error_type": "TectonicError",
        }
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
    except OSError as e:
        return {
            "ok": False,
            "affected_files": [],
            "message": "export failed",
            "error": f"could not write {out}: {e}",
            "error_type": "OSError",
        }
    message = f"exported {doc_id} to {out}"
    extra: dict[str, Any] = {}
    if missing:
        message += (
            f"; {len(missing)} figure(s) not rendered (see unrendered_figures)"
        )
        extra["unrendered_figures"] = missing
        if find_drawio() is None:
            extra["drawio_found"] = False
    if problems:
        n_broken = sum(s.state.value == "broken" for s in problems)
        message += (
            f"; {len(problems) - n_broken} suspect and {n_broken} broken "
            "link(s) (see suspect_links)"
        )
        extra["suspect_links"] = [link_status_to_dict(s) for s in problems]
    return _write_result(
        affected=[],
        message=message,
        out_path=str(out),
        bytes=len(blob),
        rev=rev_used,
        **extra,
    )


# --- Entrypoint ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mellon-mcp",
        description="MCP stdio server for a mellon repository.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help=(
            "Path to the repository root (directory containing document "
            "folders). Defaults to $CLAUDE_PROJECT_DIR, then the current "
            "directory."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Refuse every write tool. Read tools still work.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repo_root = args.repo_root or Path(
        os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()
    )
    root = repo_root.resolve()
    if not root.is_dir():
        log.error("repo root %s is not a directory", root)
        return 2
    problem = git_ops.check_repo_root(root)
    if problem is not None:
        log.error("%s", problem)
        return 2
    try:
        docs = discover_documents(root)
    except Exception as e:
        log.error("failed to discover documents in %s: %s", root, e)
        return 2
    if not docs:
        log.warning(
            "repo root %s contains no documents; the server will start anyway",
            root,
        )

    _State.repo_root = root
    _State.read_only = args.read_only
    _State.service = DocumentService(root, read_only=args.read_only)
    log.info(
        "mellon-mcp serving %s (%d document(s))%s",
        root,
        len(docs),
        " [read-only]" if args.read_only else "",
    )

    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
