"""Sandbox-tree collection interior for `snapshot_workspace`.

Owns the two ways `staging/` gets populated (the backend's bulk archive
exporter, or the portable list_dir/read_file walk), plus the host-owned-path
scrub and the final staged-tree accounting. Split out of `archive.py` to
bring `snapshot_workspace`'s own logical-line count and cyclomatic
complexity back under budget — extracting a branch into a function call
removes that branch from the caller's McCabe without changing what it does.
Every skip reason and error message is reproduced verbatim.

`_SnapshotWalker` replaces the former nested `_walk`/`_skip`/`_host_owned`
closures 1:1: their shared mutable state (`paths`, `skipped`,
`preserve_paths`, `total_bytes`, `consecutive_failures`) is now instance
state instead of captured cell variables.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from disco.core.secret_paths import is_runtime_secret_path

from .bulk_archive import _write_bulk_archive

if TYPE_CHECKING:
    from ..archive import _WorkspaceIO


class _SnapshotWalker:
    """Recursive sandbox-tree walker — the portable list_dir/read_file
    fallback used when the backend has no bulk archive exporter."""

    def __init__(
        self,
        sandbox: _WorkspaceIO,
        staging: Path,
        *,
        max_depth: int,
        max_file_bytes: int,
        normalized_host_owned: set[str],
    ) -> None:
        self._sandbox = sandbox
        self._staging = staging
        self._max_depth = max_depth
        self._max_file_bytes = max_file_bytes
        self._normalized_host_owned = normalized_host_owned
        self.paths: list[str] = []
        self.skipped: list[str] = []
        self.preserve_paths: set[str] = set()
        self.total_bytes = 0
        self._consecutive_failures = 0

    def _skip(self, child: str, reason: str) -> None:
        from disco.tools.projects import archive

        self.skipped.append(f"{child}: {reason}")
        self._consecutive_failures += 1
        if self._consecutive_failures >= archive._SNAPSHOT_MAX_CONSECUTIVE_FAILURES:
            raise archive.WorkspaceArchiveError(
                f"{self._consecutive_failures} consecutive failures (last: {child!r}: "
                f"{reason}) — transport presumed dead, aborting snapshot"
            )

    def _host_owned(self, child: str) -> str | None:
        return next(
            (
                root
                for root in self._normalized_host_owned
                if child == root or child.startswith(f"{root}/")
            ),
            None,
        )

    async def walk(self, rel: str, depth: int) -> None:
        """Mirrors the former nested `_walk` exactly, including its
        directory-vs-file probe strategy (try read_file, fall back to
        list_dir) and every skip/preserve reason string."""
        from disco.tools.projects import archive

        if depth > self._max_depth:
            raise archive.WorkspaceArchiveError(
                f"workspace depth exceeded the {self._max_depth}-level cap at {rel!r}"
            )
        try:
            entries = await self._sandbox.list_dir(rel or ".")
        except Exception as exc:  # noqa: BLE001 — tolerated per-entry, fatal at root
            if not rel:
                raise archive.WorkspaceArchiveError(f"list_dir {rel!r} failed: {exc}") from exc
            self.preserve_paths.add(rel)
            self._skip(rel, f"list_dir failed: {exc}")
            return
        for name in entries:
            if name in archive._SNAPSHOT_EXCLUDED_DIRS:
                continue  # reproducible dependency/cache tree — never snapshot
            child = f"{rel}/{name}" if rel else name
            if owner_root := self._host_owned(child):
                self.preserve_paths.add(owner_root)
                continue
            if is_runtime_secret_path(child):
                self.skipped.append(f"{child}: runtime secret path excluded")
                continue
            # Distinguish files from directories with a probe: list_dir on a file
            # raises SandboxError. We try read_file first (the common case is a
            # file) and fall back to recursing if reads fail with a recognizable
            # "is a directory" shape. Simpler than a separate stat call.
            data: bytes | None = None
            try:
                data = await self._sandbox.read_file(child)
            except Exception as read_exc:  # noqa: BLE001 — could be a directory; try walking
                try:
                    await self._sandbox.list_dir(child)
                except Exception as exc:  # noqa: BLE001
                    self.preserve_paths.add(child)
                    # Report why the READ failed, not why the directory fallback
                    # failed. For any ordinary file the fallback always fails with
                    # ENOTDIR — "Not a directory" is true, useless, and was the only
                    # thing this message used to say. It masked a 32MB transfer-cap
                    # EFBIG on a 118MB toolchain binary and a still-unidentified
                    # failure on a small built CSS file behind one identical
                    # sentence, and an unreadable entry fails the final seal, so the
                    # discarded exception was the whole diagnosis.
                    self._skip(
                        child,
                        f"could not read ({type(read_exc).__name__}: {read_exc}) "
                        f"and could not descend ({type(exc).__name__}: {exc})",
                    )
                    continue
                await self.walk(child, depth + 1)
                continue
            if len(data) > self._max_file_bytes:
                self._skip(child, f"{len(data)} bytes exceeds the {self._max_file_bytes}-byte cap")
                continue
            target = self._staging / child
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.paths.append(child)
            self.total_bytes += len(data)
            self._consecutive_failures = 0  # a success proves the transport is alive


async def _collect_snapshot_paths(
    sandbox: _WorkspaceIO,
    staging: Path,
    archive_path: Path,
    *,
    max_depth: int,
    max_file_bytes: int,
    normalized_host_owned: set[str],
) -> tuple[list[str], int, list[str], set[str]]:
    """Populate `staging/` via the backend's bulk exporter if it has one,
    else the portable list_dir/read_file walk. Returns
    ``(paths, total_bytes, skipped, preserve_paths)``."""
    from disco.tools.projects import archive

    exporter = getattr(sandbox, "export_workspace_archive", None)
    export_fn = cast(Callable[..., Awaitable[tuple[list[str], list[str]] | None]], exporter)
    exported = (
        await export_fn(archive_path, max_depth=max_depth, max_file_bytes=max_file_bytes)
        if callable(exporter)
        else None
    )
    if exported is None:
        walker = _SnapshotWalker(
            sandbox,
            staging,
            max_depth=max_depth,
            max_file_bytes=max_file_bytes,
            normalized_host_owned=normalized_host_owned,
        )
        await walker.walk("", 0)
        return sorted(walker.paths), walker.total_bytes, walker.skipped, walker.preserve_paths

    reported_skipped, reported_preserve = exported
    if not archive_path.is_file() or archive_path.is_symlink():
        raise archive.WorkspaceArchiveError("workspace archive exporter returned no archive")
    paths, total_bytes, archive_skipped = _write_bulk_archive(
        archive_path,
        staging,
        max_depth=max_depth,
        max_file_bytes=max_file_bytes,
    )
    skipped = [str(item) for item in reported_skipped]
    skipped.extend(archive_skipped)
    preserve_paths = {
        archive._archive_relpath(str(raw), max_depth=max_depth) for raw in reported_preserve
    }
    archive_path.unlink()
    return paths, total_bytes, skipped, preserve_paths


def _discard_sandbox_copies_of_host_owned_paths(
    staging: Path,
    normalized_host_owned: set[str],
    preserve_paths: set[str],
) -> None:
    """Bulk exporters cannot omit host-owned subtrees on demand. Remove any
    sandbox copy from the private staging tree before the no-follow host
    preservation step restores the authoritative host bytes."""
    for rel in normalized_host_owned:
        target = staging.joinpath(*PurePosixPath(rel).parts)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        preserve_paths.add(rel)


def _finalize_staged_tree(staging: Path) -> tuple[list[str], int]:
    """Report the tree that will actually be published, including preserved
    host-owned files and excluding any discarded sandbox copies. Also prunes
    now-empty directories left behind by the scrub/preserve steps."""
    published_files = sorted(
        path for path in staging.rglob("*") if path.is_file() and not path.is_symlink()
    )
    paths = [path.relative_to(staging).as_posix() for path in published_files]
    total_bytes = sum(path.stat().st_size for path in published_files)
    for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()
    return paths, total_bytes
