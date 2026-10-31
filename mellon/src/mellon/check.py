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
from .repo import DocLoadError
from .service import DocumentService, ServiceError
from .suspects import LinkState


def _problems(svc: DocumentService) -> tuple[list[str], int]:
    """One line per problem, and the number of documents that loaded."""
    load_errors: list[DocLoadError] = []
    validation = svc.validate_repo(load_errors=load_errors)
    lines = [f"{e.doc_id}: LOAD_ERROR {e.message}" for e in load_errors]
    for doc_id in sorted(validation):
        lines += [f"{doc_id}: {e.code} {e.message}" for e in validation[doc_id]]
    scan = svc.scan_suspects(None)
    for doc_id in sorted(scan):
        for s in scan[doc_id]:
            if s.state is not LinkState.OK:
                link = f"{s.source_id} -> {s.link.target_doc}/{s.link.target_id}"
                lines.append(f"{doc_id}: {s.state.name} {link} ({s.reason})")
    return lines, len(validation)


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
