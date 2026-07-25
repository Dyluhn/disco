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

import contextlib
import ctypes
import io
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

# The runtime-secret path classifier now lives in `disco.core` (the leaf package)
# so the release validation plane can share the exact same predicate. Re-exported
# here so every existing `from disco.tools.projects import is_runtime_secret_path`
# / `from .archive import is_runtime_secret_path` import keeps resolving unchanged.
# `tools` -> `core` is a legal downward import.
from disco.core.secret_paths import is_runtime_secret_path

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


def _prepare_authoritative_snapshot_paths(
    staging: Path,
    authoritative_paths: set[str],
    *,
    max_depth: int,
) -> None:
    """Remove fresh file-shaped conflicts above server-owned snapshot roots."""

    def remove_entry(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()

    for raw in authoritative_paths:
        rel = _archive_relpath(raw, max_depth=max_depth)
        parts = PurePosixPath(rel).parts
        current = staging
        for part in parts[:-1]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
            remove_entry(current)
            current.mkdir()
        remove_entry(staging.joinpath(*parts))


def _same_fs_object(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    ) == (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))


def _copy_preserved_snapshot_paths(
    dest: Path,
    staging: Path,
    preserve_paths: set[str],
    *,
    authoritative_paths: set[str],
    max_depth: int,
    max_file_bytes: int,
) -> None:
    """Copy unreadable prior paths through no-follow directory descriptors.

    The fresh snapshot wins ordinary unreadable-path conflicts. Server-owned
    ``authoritative_paths`` instead win at their exact root and at every
    file-shaped ancestor, while compatible fresh siblings remain. Anchoring
    traversal at an open destination directory prevents a host-side link swap
    from redirecting a preservation read outside the last authoritative tree.
    """

    if not preserve_paths:
        return

    # Reserve server-owned namespaces even on the first snapshot, when no
    # prior destination exists to copy. Sandbox bytes must never occupy an
    # ancestor that would make the host-owned subtree unreachable.
    _prepare_authoritative_snapshot_paths(
        staging,
        authoritative_paths,
        max_depth=max_depth,
    )

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        root_listed = os.stat(dest, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceArchiveError(f"cannot inspect prior snapshot safely: {exc}") from exc
    try:
        root_fd = os.open(dest, directory_flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceArchiveError(f"cannot open prior snapshot safely: {exc}") from exc

    root_opened = os.fstat(root_fd)
    if not stat.S_ISDIR(root_opened.st_mode) or not _same_fs_object(root_listed, root_opened):
        os.close(root_fd)
        raise WorkspaceArchiveError("prior snapshot changed before it could be preserved")

    def _excluded(rel: str) -> bool:
        parts = PurePosixPath(rel).parts
        return is_runtime_secret_path(rel) or any(part in _SNAPSHOT_EXCLUDED_DIRS for part in parts)

    def _copy_entry(parent_fd: int, name: str, rel: str, target: Path, depth: int) -> None:
        if _excluded(rel):
            return
        if depth > max_depth:
            raise WorkspaceArchiveError(
                f"preserved workspace depth exceeded the {max_depth}-level cap at {rel!r}"
            )
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceArchiveError(f"cannot inspect preserved path {rel!r}: {exc}") from exc

        if stat.S_ISDIR(info.st_mode):
            if target.exists() and not target.is_dir():
                return
            try:
                child_fd = os.open(name, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise WorkspaceArchiveError(
                    f"cannot open preserved directory {rel!r} safely: {exc}"
                ) from exc
            opened = os.fstat(child_fd)
            if not stat.S_ISDIR(opened.st_mode) or not _same_fs_object(info, opened):
                os.close(child_fd)
                raise WorkspaceArchiveError(
                    f"preserved directory {rel!r} changed before it could be copied"
                )
            try:
                target.mkdir(parents=True, exist_ok=True)
                for child in sorted(os.listdir(child_fd)):
                    child_rel = f"{rel}/{child}"
                    _copy_entry(child_fd, child, child_rel, target / child, depth + 1)
            finally:
                os.close(child_fd)
            return

        if not stat.S_ISREG(info.st_mode) or target.exists():
            return
        if info.st_nlink != 1:
            raise WorkspaceArchiveError(f"preserved file {rel!r} is hardlinked")
        try:
            source_fd = os.open(name, file_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceArchiveError(
                f"cannot open preserved file {rel!r} safely: {exc}"
            ) from exc
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode) or not _same_fs_object(info, opened):
                raise WorkspaceArchiveError(
                    f"preserved file {rel!r} changed before it could be copied"
                )
            if opened.st_nlink != 1:
                raise WorkspaceArchiveError(f"preserved file {rel!r} is hardlinked")
            if opened.st_size > max_file_bytes:
                raise WorkspaceArchiveError(
                    f"preserved file {rel!r} exceeds the {max_file_bytes}-byte cap"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            copied = 0
            with os.fdopen(os.dup(source_fd), "rb") as source, target.open("xb") as output:
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > max_file_bytes:
                        raise WorkspaceArchiveError(
                            f"preserved file {rel!r} grew beyond the {max_file_bytes}-byte cap"
                        )
                    output.write(block)
            after = os.fstat(source_fd)
            before_version = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            )
            after_version = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if copied != opened.st_size or after_version != before_version:
                target.unlink(missing_ok=True)
                raise WorkspaceArchiveError(f"preserved file {rel!r} changed while copied")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(source_fd)

    try:
        for raw in sorted(preserve_paths):
            rel = _archive_relpath(raw, max_depth=max_depth)
            if _excluded(rel):
                continue
            parts = PurePosixPath(rel).parts
            # A successfully captured file at an ancestor wins over an older
            # ordinary preserved subtree. Server-owned roots were prepared
            # above and therefore win this conflict instead.
            current_target = staging
            blocked_by_fresh_file = False
            for part in parts[:-1]:
                current_target /= part
                if current_target.exists() and not current_target.is_dir():
                    blocked_by_fresh_file = True
                    break
            if blocked_by_fresh_file:
                continue
            parent_fd = root_fd
            opened_dirs: list[int] = []
            try:
                missing = False
                for part in parts[:-1]:
                    try:
                        listed = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                        parent_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                    except FileNotFoundError:
                        missing = True
                        break
                    except OSError as exc:
                        raise WorkspaceArchiveError(
                            f"cannot traverse preserved path {rel!r} safely: {exc}"
                        ) from exc
                    opened_dirs.append(parent_fd)
                    opened = os.fstat(parent_fd)
                    if not stat.S_ISDIR(opened.st_mode) or not _same_fs_object(listed, opened):
                        raise WorkspaceArchiveError(
                            f"preserved path {rel!r} changed during traversal"
                        )
                if not missing:
                    _copy_entry(
                        parent_fd,
                        parts[-1],
                        rel,
                        staging.joinpath(*parts),
                        len(parts) - 1,
                    )
            finally:
                for opened_fd in reversed(opened_dirs):
                    os.close(opened_fd)
    finally:
        os.close(root_fd)


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


def _write_bulk_archive(
    archive_path: Path,
    staging: Path,
    *,
    max_depth: int,
    max_file_bytes: int,
) -> tuple[list[str], int, list[str]]:
    """Validate and materialize a backend archive without trusting tar paths/types."""

    paths: list[str] = []
    skipped: list[str] = []
    total_bytes = 0
    consecutive_failures = 0
    seen: set[str] = set()
    try:
        archive = tarfile.open(archive_path, mode="r:")
    except (tarfile.TarError, EOFError) as exc:
        raise WorkspaceArchiveError(f"workspace archive is invalid: {exc}") from exc
    with archive:
        for member in archive:
            rel = _archive_relpath(member.name, max_depth=max_depth)
            if rel in seen:
                raise WorkspaceArchiveError(f"workspace archive contains duplicate path {rel!r}")
            rel_parts = PurePosixPath(rel).parts
            ancestors = [PurePosixPath(*rel_parts[:i]).as_posix() for i in range(1, len(rel_parts))]
            if any(ancestor in seen for ancestor in ancestors) or any(
                prior.startswith(rel + "/") for prior in seen
            ):
                raise WorkspaceArchiveError(
                    f"workspace archive contains conflicting path prefix {rel!r}"
                )
            seen.add(rel)
            if not member.isreg():
                raise WorkspaceArchiveError(f"workspace archive contains non-regular entry {rel!r}")
            if any(part in _SNAPSHOT_EXCLUDED_DIRS for part in PurePosixPath(rel).parts):
                skipped.append(f"{rel}: dependency/cache path excluded")
                continue
            if is_runtime_secret_path(rel):
                skipped.append(f"{rel}: runtime secret path excluded")
                continue
            if member.size > max_file_bytes:
                skipped.append(f"{rel}: {member.size} bytes exceeds the {max_file_bytes}-byte cap")
                consecutive_failures += 1
                if consecutive_failures >= _SNAPSHOT_MAX_CONSECUTIVE_FAILURES:
                    raise WorkspaceArchiveError(
                        f"{consecutive_failures} consecutive failures (last: {rel!r}: "
                        "oversized) — transport presumed dead, aborting snapshot"
                    )
                continue
            source = archive.extractfile(member)
            if source is None:
                raise WorkspaceArchiveError(f"workspace archive cannot read {rel!r}")
            data = source.read(max_file_bytes + 1)
            if len(data) != member.size:
                raise WorkspaceArchiveError(
                    f"workspace archive truncated {rel!r}: expected {member.size}, got {len(data)}"
                )
            target = staging / rel
            current = staging
            for part in PurePosixPath(rel).parts[:-1]:
                current /= part
                if current.exists() and not current.is_dir():
                    current.unlink()
                current.mkdir(exist_ok=True)
            if target.is_dir():
                shutil.rmtree(target)
            target.write_bytes(data)
            paths.append(rel)
            total_bytes += len(data)
            consecutive_failures = 0
    paths.sort()
    return paths, total_bytes, skipped


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
    paths: list[str] = []
    skipped: list[str] = []
    preserve_paths: set[str] = set()
    total_bytes = 0
    consecutive_failures = 0

    def _skip(child: str, reason: str) -> None:
        nonlocal consecutive_failures
        skipped.append(f"{child}: {reason}")
        consecutive_failures += 1
        if consecutive_failures >= _SNAPSHOT_MAX_CONSECUTIVE_FAILURES:
            raise WorkspaceArchiveError(
                f"{consecutive_failures} consecutive failures (last: {child!r}: "
                f"{reason}) — transport presumed dead, aborting snapshot"
            )

    def _host_owned(child: str) -> str | None:
        return next(
            (
                root
                for root in normalized_host_owned
                if child == root or child.startswith(f"{root}/")
            ),
            None,
        )

    async def _walk(rel: str, depth: int) -> None:
        nonlocal total_bytes, consecutive_failures
        if depth > max_depth:
            raise WorkspaceArchiveError(
                f"workspace depth exceeded the {max_depth}-level cap at {rel!r}"
            )
        try:
            entries = await sandbox.list_dir(rel or ".")
        except Exception as exc:  # noqa: BLE001 — tolerated per-entry, fatal at root
            if not rel:
                raise WorkspaceArchiveError(f"list_dir {rel!r} failed: {exc}") from exc
            preserve_paths.add(rel)
            _skip(rel, f"list_dir failed: {exc}")
            return
        for name in entries:
            if name in _SNAPSHOT_EXCLUDED_DIRS:
                continue  # reproducible dependency/cache tree — never snapshot
            child = f"{rel}/{name}" if rel else name
            if owner_root := _host_owned(child):
                preserve_paths.add(owner_root)
                continue
            if is_runtime_secret_path(child):
                skipped.append(f"{child}: runtime secret path excluded")
                continue
            # Distinguish files from directories with a probe: list_dir on a file
            # raises SandboxError. We try read_file first (the common case is a
            # file) and fall back to recursing if reads fail with a recognizable
            # "is a directory" shape. Simpler than a separate stat call.
            data: bytes | None = None
            try:
                data = await sandbox.read_file(child)
            except Exception as read_exc:  # noqa: BLE001 — could be a directory; try walking
                try:
                    await sandbox.list_dir(child)
                except Exception as exc:  # noqa: BLE001
                    preserve_paths.add(child)
                    # Report why the READ failed, not why the directory fallback
                    # failed. For any ordinary file the fallback always fails with
                    # ENOTDIR — "Not a directory" is true, useless, and was the only
                    # thing this message used to say. It masked a 32MB transfer-cap
                    # EFBIG on a 118MB toolchain binary and a still-unidentified
                    # failure on a small built CSS file behind one identical
                    # sentence, and an unreadable entry fails the final seal, so the
                    # discarded exception was the whole diagnosis.
                    _skip(
                        child,
                        f"could not read ({type(read_exc).__name__}: {read_exc}) "
                        f"and could not descend ({type(exc).__name__}: {exc})",
                    )
                    continue
                await _walk(child, depth + 1)
                continue
            if len(data) > max_file_bytes:
                _skip(child, f"{len(data)} bytes exceeds the {max_file_bytes}-byte cap")
                continue
            target = staging / child
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            paths.append(child)
            total_bytes += len(data)
            consecutive_failures = 0  # a success proves the transport is alive

    try:
        exporter = getattr(sandbox, "export_workspace_archive", None)
        export_fn = cast(Callable[..., Awaitable[tuple[list[str], list[str]] | None]], exporter)
        exported = (
            await export_fn(
                archive_path,
                max_depth=max_depth,
                max_file_bytes=max_file_bytes,
            )
            if callable(exporter)
            else None
        )
        if exported is None:
            await _walk("", 0)
            paths.sort()
        else:
            reported_skipped, reported_preserve = exported
            if not archive_path.is_file() or archive_path.is_symlink():
                raise WorkspaceArchiveError("workspace archive exporter returned no archive")
            paths, total_bytes, archive_skipped = _write_bulk_archive(
                archive_path,
                staging,
                max_depth=max_depth,
                max_file_bytes=max_file_bytes,
            )
            skipped.extend(str(item) for item in reported_skipped)
            skipped.extend(archive_skipped)
            for raw in reported_preserve:
                preserve_paths.add(_archive_relpath(str(raw), max_depth=max_depth))
            archive_path.unlink()

        # Bulk exporters cannot omit host-owned subtrees on demand. Remove any
        # sandbox copy from the private staging tree before the no-follow host
        # preservation step restores the authoritative host bytes.
        for rel in normalized_host_owned:
            target = staging.joinpath(*PurePosixPath(rel).parts)
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            preserve_paths.add(rel)

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
        published_files = sorted(
            path for path in staging.rglob("*") if path.is_file() and not path.is_symlink()
        )
        paths = [path.relative_to(staging).as_posix() for path in published_files]
        total_bytes = sum(path.stat().st_size for path in published_files)
        for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
            with contextlib.suppress(OSError):
                directory.rmdir()
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
