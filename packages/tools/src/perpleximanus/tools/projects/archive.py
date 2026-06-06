"""Workspace snapshot / rehydrate / zip — transport-agnostic, via SandboxInstance.

The whole point of going through `SandboxInstance.{list_dir,read_file,write_file}`
is that the same code works across local + gVisor (and any future backend) — the
container-internal `exec` path means we never touch a host path directly, so a
saved project rehydrates into a *fresh* sandbox on a different backend without
care for where the original ran. That portability is the load-bearing property.

Conservative guards (size + depth caps) are guards, not policy — they fail loud
with a typed error if the agent built something pathologically deep / huge, so
a runaway loop can't fill the disk. Defaults are generous (16 deep, 32 MiB per
file) and tunable per call.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Sensible defaults: deep enough for realistic project trees, large enough for the
# kinds of artifacts a build agent produces (bundled JS, small images). Raised
# only by the caller if a project legitimately needs more headroom.
_DEFAULT_MAX_DEPTH = 16
_DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024  # 32 MiB


class _WorkspaceIO(Protocol):
    """The read/write surface we need on a sandbox instance. Anything implementing
    SandboxInstance (or SandboxSession, which conforms) works as-is."""

    async def list_dir(self, path: str) -> list[str]: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...


class WorkspaceArchiveError(Exception):
    """A snapshot or rehydrate hit a hard limit / I/O failure. Carries the offending
    path + reason so the agent-server can surface it in a system-reminder."""


@dataclass(frozen=True)
class SnapshotResult:
    """What a snapshot produced. Returned to the agent-server so it can update
    the manifest + tell the model/user what was saved."""

    file_count: int
    total_bytes: int
    paths: list[str]  # workspace-relative POSIX paths, sorted


# ---- snapshot OUT ----------------------------------------------------------


async def snapshot_workspace(
    sandbox: _WorkspaceIO,
    dest: Path,
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
) -> SnapshotResult:
    """Mirror the sandbox /workspace tree to `dest/` on disk, preserving structure.

    The walk goes through SandboxInstance.list_dir (one level at a time — the
    interface doesn't expose recursion, so we do it client-side). Every leaf is
    read with read_file and written to disk under the corresponding relative
    path. Existing files at dest are overwritten; existing dirs are reused.
    """
    dest.mkdir(parents=True, exist_ok=True)

    paths: list[str] = []
    total_bytes = 0

    async def _walk(rel: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > max_depth:
            raise WorkspaceArchiveError(
                f"workspace depth exceeded the {max_depth}-level cap at {rel!r}"
            )
        try:
            entries = await sandbox.list_dir(rel or ".")
        except Exception as exc:  # noqa: BLE001 — surface the real cause
            raise WorkspaceArchiveError(f"list_dir {rel!r} failed: {exc}") from exc
        for name in entries:
            child = f"{rel}/{name}" if rel else name
            # Distinguish files from directories with a probe: list_dir on a file
            # raises SandboxError. We try read_file first (the common case is a
            # file) and fall back to recursing if reads fail with a recognizable
            # "is a directory" shape. Simpler than a separate stat call.
            data: bytes | None = None
            try:
                data = await sandbox.read_file(child)
            except Exception:  # noqa: BLE001 — could be a directory; try walking
                try:
                    await sandbox.list_dir(child)
                except Exception as exc:  # noqa: BLE001
                    raise WorkspaceArchiveError(
                        f"could not read or descend into {child!r}: {exc}"
                    ) from exc
                await _walk(child, depth + 1)
                continue
            if len(data) > max_file_bytes:
                raise WorkspaceArchiveError(
                    f"file {child!r} is {len(data)} bytes, exceeds the "
                    f"{max_file_bytes}-byte cap"
                )
            target = dest / child
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            paths.append(child)
            total_bytes += len(data)

    await _walk("", 0)
    paths.sort()
    return SnapshotResult(file_count=len(paths), total_bytes=total_bytes, paths=paths)


# ---- rehydrate IN ----------------------------------------------------------


async def rehydrate_workspace(sandbox: _WorkspaceIO, src: Path) -> int:
    """Walk a snapshot tree on disk and write_file each entry into the sandbox.

    The sandbox is assumed to be a fresh /workspace; existing files would be
    overwritten silently (matches the file_write tool's behavior). Returns the
    file count restored. Empty src directories are a silent no-op — the
    "rehydrate after files were deleted under us" case is handled by the
    caller checking up front whether src has any contents."""
    if not src.exists() or not src.is_dir():
        return 0
    count = 0
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src).as_posix()
        try:
            await sandbox.write_file(rel, path.read_bytes())
        except Exception as exc:  # noqa: BLE001 — surface the real path
            raise WorkspaceArchiveError(f"rehydrate {rel!r} failed: {exc}") from exc
        count += 1
    return count


# ---- streaming zip for download -------------------------------------------


def zip_workspace(src: Path) -> Iterator[bytes]:
    """Stream a zip archive of the project workspace tree as bytes chunks.

    Build the zip into a BytesIO buffer (the tree is bounded by the snapshot
    caps), then yield it in chunks suitable for a FastAPI StreamingResponse.
    A truly streaming-write zip is possible but adds complexity for no
    user-visible benefit at our sizes."""
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"workspace directory not found: {src}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            zf.write(path, arcname=path.relative_to(src).as_posix())
    buf.seek(0)
    chunk = 64 * 1024
    while True:
        block = buf.read(chunk)
        if not block:
            break
        yield block


async def aiter_zip_workspace(src: Path) -> AsyncIterator[bytes]:
    """Async wrapper over zip_workspace so FastAPI's StreamingResponse can iterate
    it directly without a sync-to-async bridge in the endpoint."""
    for block in zip_workspace(src):
        yield block
