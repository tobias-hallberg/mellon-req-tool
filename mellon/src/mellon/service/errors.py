"""Unified error taxonomy for adapters (GUI / MCP).

`DocumentService` catches `EditorError`, `LoaderError`, and `GitError` from the
core library and re-raises them as a single `ServiceError` tagged with an
`ErrorCode`. Adapters translate the code to their own surface:
    GUI  -> status bar / dialog severity
    MCP  -> `{ok: False, error_type: code.value, error: message}`

Code strings are stable public values; treat them as part of the MCP wire format.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    NOT_FOUND = "not_found"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    ALREADY_EXISTS = "already_exists"
    LOAD = "load"
    GIT = "git"
    READ_ONLY = "read_only"
    IO = "io"


class ServiceError(Exception):
    """Any failure surfaced by `DocumentService`.

    `code` categorizes the failure for programmatic handling; `message` is the
    human-readable text (usually copied from the wrapped exception); `details`
    carries structured context for adapters that want it (e.g. MCP dicts).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}

    def __str__(self) -> str:
        return self.message
