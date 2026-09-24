"""Restore host-owned snapshot files through the backend's verified batch seam."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from disco.core.secret_paths import is_runtime_secret_path
from disco.tools.sandbox.file_batch import SandboxFileBatchResult, SandboxFileMutation

if TYPE_CHECKING:
    from ..archive import _WorkspaceIO


def _source_files(src: Path) -> list[Path]:
    return [
        path
        for path in sorted(src.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and not is_runtime_secret_path(path.relative_to(src).as_posix())
    ]


async def _verify_restored_files(
    sandbox: _WorkspaceIO,
    mutations: tuple[SandboxFileMutation, ...],
    result: SandboxFileBatchResult,
) -> None:
    from ..archive import WorkspaceArchiveError

    expected = {
        mutation.path: (hashlib.sha256(mutation.after).hexdigest(), len(mutation.after))
        for mutation in mutations
        if mutation.after is not None
    }
    observed = {
        change.path: (change.after_sha256, change.after_size_bytes) for change in result.changes
    }
    if len(observed) != len(result.changes) or any(
        path not in expected or proof != expected[path] for path, proof in observed.items()
    ):
        raise WorkspaceArchiveError("workspace rehydration batch evidence did not match files")
    # The batch reports transitions only: already-identical files are omitted.
    # Verify these on retry rather than mistaking them for loss.
    for path in expected.keys() - observed.keys():
        actual = await sandbox.read_file(path)
        if (hashlib.sha256(actual).hexdigest(), len(actual)) != expected[path]:
            raise WorkspaceArchiveError(f"rehydrated file differs from snapshot: {path}")


async def _restore_batch(sandbox: _WorkspaceIO, mutations: tuple[SandboxFileMutation, ...]) -> bool:
    from ..archive import WorkspaceArchiveError

    commit = getattr(sandbox, "_commit_file_batch", None)
    if not callable(commit) or not mutations:
        return False
    try:
        commit_fn = cast(
            Callable[[tuple[SandboxFileMutation, ...]], Awaitable[SandboxFileBatchResult | None]],
            commit,
        )
        result = await commit_fn(mutations)
    except Exception as exc:
        raise WorkspaceArchiveError(f"workspace rehydration batch failed: {exc}") from exc
    # None means this backend does not support batching. A failed dispatch must
    # never silently fall back after it may already have changed guest bytes.
    if result is None:
        return False
    if not isinstance(result, SandboxFileBatchResult):
        raise WorkspaceArchiveError("workspace rehydration returned invalid batch evidence")
    if result.failure is not None:
        raise WorkspaceArchiveError(
            f"rehydrate {result.failure.path!r} failed: {result.failure.message}"
        )
    await _verify_restored_files(sandbox, mutations, result)
    return True


async def restore_files(sandbox: _WorkspaceIO, src: Path) -> int:
    if not src.exists() or not src.is_dir():
        return 0
    files = _source_files(src)
    return await restore_file_bytes(
        sandbox, tuple((path.relative_to(src).as_posix(), path.read_bytes()) for path in files)
    )


async def restore_file_bytes(sandbox: _WorkspaceIO, files: tuple[tuple[str, bytes], ...]) -> int:
    from ..archive import WorkspaceArchiveError

    mutations = tuple(SandboxFileMutation(path, data) for path, data in files)
    if not await _restore_batch(sandbox, mutations):
        for path, data in files:
            try:
                await sandbox.write_file(path, data)
            except Exception as exc:
                raise WorkspaceArchiveError(f"rehydrate {path!r} failed: {exc}") from exc
    return len(files)
