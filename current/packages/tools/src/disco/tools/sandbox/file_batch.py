"""Transport-neutral evidence for an optional sandbox file-batch capability.

The ordinary sandbox protocol stays deliberately small.  Container backends may
implement this additive capability so a deterministic producer can cross a remote
runtime boundary once instead of once per filesystem primitive.  Backends without
it keep using the portable single-file path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class SandboxFileMutation:
    """One requested final state; ``None`` means the file should be absent."""

    path: str
    after: bytes | None


@dataclass(frozen=True, slots=True)
class SandboxFileChange:
    """A backend-proven file transition, expressed without retaining file bytes."""

    path: str
    before_sha256: str | None
    after_sha256: str | None
    after_size_bytes: int | None


@dataclass(frozen=True, slots=True)
class SandboxFileBatchFailure:
    """A handled batch refusal/failure with the backend's exact known boundary."""

    phase: Literal["prevalidation", "stale", "commit"]
    error: str
    kind: str
    message: str
    path: str | None = None
    failed_path_state: Literal["unchanged", "committed", "unknown"] | None = None
    underlying: dict[str, Any] | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SandboxFileBatchResult:
    """Acknowledged changes plus an optional handled failure."""

    changes: tuple[SandboxFileChange, ...]
    failure: SandboxFileBatchFailure | None = None


__all__ = [
    "SandboxFileBatchFailure",
    "SandboxFileBatchResult",
    "SandboxFileChange",
    "SandboxFileMutation",
]
