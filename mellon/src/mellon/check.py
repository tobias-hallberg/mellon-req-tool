"""`mellon-check`: a pass/fail gate over a repository's documents, for CI.

Reports every document that fails to load, every validation error and every
suspect or broken link, using the same `DocumentService` calls as the GUI and
the MCP server, so it agrees with them.

Exit status: 0 clean, 1 problems found, 2 the root can't be checked at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, git_ops
from .git_ops import GitError
from .link_index import from_scratch
from .repo import DocLoadError
from .service import DocumentService, ErrorCode, ServiceError
from .suspects import LinkState, scan_document
from .validator import link_not_allowed, link_target_missing, validate


# Requirements: MELLON-CHECK-SRS-137
def _load_error_line(err: DocLoadError, root: Path) -> str:
    """`<doc>: LOAD_ERROR <file>:<line>: <reason>`, the file relative to the
    root; without the line when the reason concerns no one line."""
    if err.file is None:
        return f"{err.doc_id}: LOAD_ERROR {err.message}"
    reason = err.message
    prefix = f"{err.file}: "
    if reason.startswith(prefix):
        reason = reason[len(prefix):]
    try:
        where = err.file.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        where = str(err.file)
    if err.line is not None:
        where = f"{where}:{err.line}"
    return f"{err.doc_id}: LOAD_ERROR {where}: {reason}"


# Requirements: MELLON-CHECK-SRS-2, MELLON-CHECK-SRS-137, MELLON-CHECK-SRS-147,
#   MELLON-CHECK-SRS-148, MELLON-CHECK-SRS-149
def _problems(svc: DocumentService) -> tuple[list[str], int]:
    """One line per problem, and the number of documents that loaded.

    The repository is read once: each document is either validated and
    scanned from that one reading, or reported as a load error, so a file
    that changes while the check runs can't drop a document unreported.
    """
    load_errors: list[DocLoadError] = []
    docs = svc.get_all_documents(load_errors=load_errors)
    lines = [_load_error_line(e, svc.repo_root) for e in load_errors]
    for doc_id in sorted(docs):
        lines += [f"{doc_id}: {e.code} {e.message}" for e in validate(docs[doc_id].document)]
    for doc_id in sorted(docs):
        loaded = docs[doc_id]
        doc = loaded.document
        try:
            statuses = scan_document(doc, loaded.folder)
        except GitError as e:
            raise ServiceError(ErrorCode.GIT, str(e)) from e
        for s in statuses:
            if s.state is LinkState.OK:
                continue
            # MELLON-CHECK-SRS-134: one line per check problem. A broken link
            # the validation already printed, as LINK_TARGET_MISSING (a target
            # in its own document) or as LINK_NOT_ALLOWED (a target outside
            # the document and its parents, MELLON-CHECK-SRS-33), gets no
            # second BROKEN line; one into a parent document has only the
            # BROKEN line.
            if s.state is LinkState.BROKEN and (
                link_target_missing(doc, s.link) or link_not_allowed(doc, s.link)
            ):
                continue
            link = f"{s.source_id} -> {s.link.target_doc}/{s.link.target_id}"
            lines.append(f"{doc_id}: {s.state.name} {link} ({s.reason})")
    return lines, len(docs)


# Requirements: MELLON-CHECK-SRS-114, MELLON-CHECK-SRS-115, MELLON-CHECK-SRS-116,
#   MELLON-CHECK-SRS-117, MELLON-CHECK-SRS-124, MELLON-CHECK-SRS-127, MELLON-CHECK-SRS-134,
#   MELLON-CHECK-SRS-146, MELLON-CHECK-SRS-147, MELLON-CHECK-SRS-148, MELLON-CHECK-SRS-149,
#   MELLON-STORE-SRS-12, MELLON-STORE-SRS-210
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mellon-check",
        description=(
            "Check a mellon repository: load errors, validation errors, and "
            "suspect or broken links."
        ),
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
    args = parser.parse_args(argv)

    repo_root = args.repo_root or Path(
        os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()
    )
    root = repo_root.resolve()
    if not root.is_dir():
        print(f"repo root {root} is not a directory", file=sys.stderr)
        return 2
    problem = git_ops.check_repo_root(root)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2

    try:
        with from_scratch():
            lines, doc_count = _problems(DocumentService(root, read_only=True))
    except ServiceError as e:
        print(e.message, file=sys.stderr)
        return 2

    for line in lines:
        print(line)
    print(f"{doc_count} document(s) checked, {len(lines)} problem(s)")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
