"""Workspace snapshot / rehydrate / zip — transport-agnostic, via SandboxInstance.

The whole point of going through `SandboxInstance.{list_dir,read_file,write_file}`
is that the same code works across local + gVisor (and any future backend) — the
container-internal `exec` path means we never touch a host path directly, so a
saved project rehydrates into a *fresh* sandbox on a different backend without
care for where the original ran. That portability is the essential property.

Conservative guards (size + depth caps) are guards, not policy — they fail loud
with a typed error if the agent built something pathologically deep / huge, so
a runaway loop can't fill the disk. Defaults are generous (16 deep, 32 MiB per
file) and tunable per call.
"""

from __future__ import annotations

import ctypes
import io
import os
import shutil
import tempfile
import zipfile
from collections.abc import AsyncIterator, Iterator

# --- compatibility re-exports (PKG-10-PROJECTS) ------------------------------
# Imports written with a redundant `X as X` alias in this file are NOT used by
# this module — they are pure re-exports of top-level names it exposed before
# the archive_parts split, kept so the module surface is unchanged for anything
# that reaches them through the module object. isort interleaves them with the
# real imports; the alias is what marks them. PKG-13-FACADES owns their
# eventual deletion.
#
# Three parent-era names are deliberately NOT restored, as a recorded root
# override of the facade-surface differential (Epic 10-A): `contextlib`, `stat`
# and `tarfile` were bare `import <stdlib>` bindings whose only uses moved into
# `archive_parts/`. Root re-ran the consumer analysis and `archive.contextlib`,
# `archive.stat` and `archive.tarfile` have zero call sites anywhere in the
# tree, and re-adding an unused plain `import` would require a per-line lint
# suppression the engineering rules forbid.
from collections.abc import Awaitable as Awaitable
from collections.abc import Callable as Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol
from typing import cast as cast

# The runtime-secret path classifier now lives in `disco.core` (the leaf package)
# so the release validation plane can share the exact same predicate. Re-exported
# here so every existing `from disco.tools.projects import is_runtime_secret_path`
# / `from .archive import is_runtime_secret_path` import keeps resolving unchanged.
# `tools` -> `core` is a legal downward import.
from disco.core.secret_paths import is_runtime_secret_path

from .archive_parts.bulk_archive import _write_bulk_archive as _write_bulk_archive

# `_copy_preserved_snapshot_paths` (fd-anchored, no-follow copy of unreadable /
# host-owned prior paths into staging) is the interior of `snapshot_workspace`
# that carried nearly all of this module's cyclomatic complexity. Its
# decomposition lives in `archive_parts.preserve_copy`; imported here (not
# inlined) so this module stays within the logical-line budget.
from .archive_parts.preserve_copy import _copy_preserved_snapshot_paths
from .archive_parts.preserve_copy import (
    _prepare_authoritative_snapshot_paths as _prepare_authoritative_snapshot_paths,
)
from .archive_parts.preserve_copy import _same_fs_object as _same_fs_object

# The portable sandbox-tree walk (used when a backend has no bulk archive
# exporter), the host-owned-path scrub, and the final staged-tree accounting —
# the rest of `snapshot_workspace`'s interior. See `archive_parts.snapshot_walk`.
from .archive_parts.snapshot_walk import (
    _collect_snapshot_paths,
    _discard_sandbox_copies_of_host_owned_paths,
    _finalize_staged_tree,
)

# Sensible defaults: deep enough for realistic project trees, large enough for the
# kinds of artifacts a build agent produces (bundled JS, small images). Raised
# only by the caller if a project legitimately needs more headroom.
_DEFAULT_MAX_DEPTH = 16
_DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024  # 32 MiB

# Dependency/cache directories are never snapshotted: they are reproducible from
# the source tree (package.json / lockfiles ARE snapshotted), and walking them
# one exec round-trip per file is what broke the dc-02 live rung — a .pnpm-store
# holds thousands of content-addressed files, the walk took minutes over the ssh
# transport and died with a broken pipe, and the manifest was never written.
_SNAPSHOT_EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        ".pnpm-store",
        ".npm",
        ".yarn",
        ".cache",
        ".venv",
        # PEP 370 user site (`pip install --user` -> ~/.local/lib/pythonX.Y/
        # site-packages) is the same reproducible dependency tree as node_modules
        # and the one inside .venv, which are already excluded — it was simply
        # missed because it lives outside a virtualenv. It matters because the
        # final seal refuses any tree with an unreadable entry, and a Playwright
        # install puts a 118MB `driver/node` there, far over the 32MB sandbox
        # transfer cap, so its presence made the run permanently unsealable.
        "site-packages",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

# After this many CONSECUTIVE per-entry failures the transport is presumed dead
# (a dropped ssh pipe fails every subsequent call) — abort instead of grinding
# through thousands of doomed round-trips.
_SNAPSHOT_MAX_CONSECUTIVE_FAILURES = 10
# Server-owned durable audit state is not part of the sandbox's authored tree.
# A later sandbox snapshot must preserve it from the host mirror rather than
# letting an older/absent in-box copy erase deployment ownership evidence.
_DEFAULT_HOST_OWNED_PATHS = (".disco/cloudflare/deployments",)


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
    skipped: list[str] = field(default_factory=list)  # unreadable entries we tolerated


def _archive_relpath(name: str, *, max_depth: int) -> str:
    if "\\" in name:
        raise WorkspaceArchiveError(f"workspace archive member has invalid separator: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise WorkspaceArchiveError(f"workspace archive member escapes workspace: {name!r}")
    if len(path.parts) - 1 > max_depth:
        raise WorkspaceArchiveError(
            f"workspace depth exceeded the {max_depth}-level cap at {path.as_posix()!r}"
        )
    return path.as_posix()


def _publish_snapshot(staging: Path, dest: Path) -> None:
    """Atomically publish a complete tree on the Linux campaign filesystem."""

    if not dest.exists():
        staging.replace(dest)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise WorkspaceArchiveError("atomic snapshot exchange is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    rc = renameat2(
        -100,
        os.fsencode(staging),
        -100,
        os.fsencode(dest),
        2,
    )
    if rc != 0:
        error = ctypes.get_errno()
        raise WorkspaceArchiveError(f"atomic snapshot exchange failed: {os.strerror(error)}")
    # After RENAME_EXCHANGE the old authoritative tree occupies `staging`.
    shutil.rmtree(staging)


# ---- snapshot OUT ----------------------------------------------------------


async def snapshot_workspace(
    sandbox: _WorkspaceIO,
    dest: Path,
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    host_owned_paths: tuple[str, ...] = _DEFAULT_HOST_OWNED_PATHS,
) -> SnapshotResult:
    """Mirror the sandbox /workspace tree to `dest/` on disk, preserving structure.

    Backends may expose one bounded bulk archive (Podman does); otherwise the
    portable list/read walker remains the fallback. Both paths build in a private
    sibling staging directory and publish only a complete tree, so FINISHED-time
    cancellation can never leave partial bytes at the authoritative destination.

    Resilience (dc-02 live-rung lesson): dependency/cache dirs
    (_SNAPSHOT_EXCLUDED_DIRS) are skipped outright, and a single unreadable
    entry no longer aborts the snapshot — it's recorded in `result.skipped`
    and the walk continues, so the manifest still gets written and the project
    stays revivable. Only a presumed-dead transport
    (_SNAPSHOT_MAX_CONSECUTIVE_FAILURES consecutive failures) or an unlistable
    workspace ROOT raises — those mean "we saved nothing trustworthy".
    """
    if dest.is_symlink():
        raise WorkspaceArchiveError("snapshot destination must not be a symlink")
    if dest.exists() and not dest.is_dir():
        raise WorkspaceArchiveError("snapshot destination must be a directory")
    dest.parent.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix=f".{dest.name}.snapshot-", dir=dest.parent))
    transaction.chmod(0o700)
    staging = transaction / "tree"
    try:
        staging.mkdir(mode=0o700)
    except BaseException:
        shutil.rmtree(transaction, ignore_errors=True)
        raise
    archive_path = transaction / "workspace.tar"

    normalized_host_owned = {
        _archive_relpath(path, max_depth=max_depth) for path in host_owned_paths
    }

    try:
        paths, total_bytes, skipped, preserve_paths = await _collect_snapshot_paths(
            sandbox,
            staging,
            archive_path,
            max_depth=max_depth,
            max_file_bytes=max_file_bytes,
            normalized_host_owned=normalized_host_owned,
        )

        _discard_sandbox_copies_of_host_owned_paths(staging, normalized_host_owned, preserve_paths)

        _copy_preserved_snapshot_paths(
            dest,
            staging,
            preserve_paths,
            authoritative_paths=normalized_host_owned,
            max_depth=max_depth,
            max_file_bytes=max_file_bytes,
        )
        # Report the tree that will actually be published, including preserved
        # host-owned files and excluding any discarded sandbox copies.
        paths, total_bytes = _finalize_staged_tree(staging)
        _publish_snapshot(staging, dest)
        return SnapshotResult(
            file_count=len(paths), total_bytes=total_bytes, paths=paths, skipped=skipped
        )
    finally:
        if transaction.exists():
            shutil.rmtree(transaction)


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
    for path in sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(src).as_posix()
        if is_runtime_secret_path(rel):
            continue
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
        for path in sorted(p for p in src.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(src).as_posix()
            if not is_runtime_secret_path(rel):
                zf.write(path, arcname=rel)
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
