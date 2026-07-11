"""Build-project persistence — app-layer storage for the workspace files the agent
builds, mirrored 1:1 from the sandbox `/workspace` tree to the user-chosen host
directory.

Two boundaries:
- `archive`: transport-agnostic workspace I/O via the SandboxInstance interface
  (snapshot OUT to disk, rehydrate IN from disk, stream a zip for download).
- `store`: per-project manifest + workspace tree on disk, with named typed
  errors so graceful failure (missing path / files gone / not writable) lights
  up clearly at the agent-server boundary.
"""

from __future__ import annotations

from .archive import (
    SnapshotResult,
    aiter_zip_workspace,
    is_runtime_secret_path,
    rehydrate_workspace,
    snapshot_workspace,
    zip_workspace,
)
from .store import (
    ProjectRecord,
    ProjectStore,
    StorageError,
    StorageStatus,
    default_projects_root,
    resolve_projects_root,
    validate_root,
)

__all__ = [
    "ProjectRecord",
    "ProjectStore",
    "SnapshotResult",
    "StorageError",
    "StorageStatus",
    "aiter_zip_workspace",
    "default_projects_root",
    "is_runtime_secret_path",
    "rehydrate_workspace",
    "resolve_projects_root",
    "snapshot_workspace",
    "validate_root",
    "zip_workspace",
]
