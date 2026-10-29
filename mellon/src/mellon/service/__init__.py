"""Service façade between adapters (GUI/MCP) and core modules.

Adapters import only from `mellon.service` and `mellon.schema`. The service
owns error translation, document caching, and the load→mutate→reload cycle.
"""

from .cache import RepoCache
from .document import DocumentService
from .errors import ErrorCode, ServiceError
from .results import (
    CommitResult,
    CompositeWriteResult,
    DeleteResult,
    DocCommitPreview,
    MutationResult,
)

__all__ = [
    "CommitResult",
    "CompositeWriteResult",
    "DeleteResult",
    "DocCommitPreview",
    "DocumentService",
    "ErrorCode",
    "MutationResult",
    "RepoCache",
    "ServiceError",
]
