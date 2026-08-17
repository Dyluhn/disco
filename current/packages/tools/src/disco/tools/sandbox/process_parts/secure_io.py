"""Workspace-jailed file I/O via no-follow directory descriptors.

`read_file` and `delete_file` (`ProcessSandboxInstance`) both walk the target
path one path component at a time through `os.open(..., dir_fd=...)` with
`O_NOFOLLOW`, so a symlink planted anywhere along the path is refused rather
than followed — the descriptor chain, not just the lexical `_resolve` jail, is
the containment. Every `OSError` → `SandboxPermissionError` mapping and every
size/type check is preserved verbatim and in the same order as the class body
this was extracted from.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .._container import MAX_SANDBOX_READ_BYTES, SANDBOX_READ_TIMEOUT_S
from ..base import SandboxPermissionError, strip_redundant_workspace_prefix

if TYPE_CHECKING:
    from ..process import ProcessSandboxInstance


async def read_file(instance: ProcessSandboxInstance, path: str) -> bytes:
    instance._alive()
    clean = strip_redundant_workspace_prefix(path)
    instance._resolve(path)  # lexical jail before the descriptor walk

    def _read_securely() -> bytes:
        return _read_file_securely(instance._workspace, clean, path)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_read_securely), timeout=SANDBOX_READ_TIMEOUT_S
        )
    except TimeoutError as exc:
        raise OSError(
            errno.ETIMEDOUT,
            f"read_file {path!r}: exceeded the {SANDBOX_READ_TIMEOUT_S}s read timeout",
        ) from exc


def _read_file_securely(workspace: Path, clean: str, path: str) -> bytes:
    parts = [part for part in Path(clean).parts if part not in ("", ".")]
    if not parts:
        raise SandboxPermissionError(f"read_file {path!r}: invalid workspace path")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    opened: list[int] = []
    try:
        current = _walk_to_parent_dir(workspace, parts, dir_flags, path, opened)
        descriptor = _open_target_file(current, parts[-1], path, opened)
        return _read_capped(descriptor, path)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise SandboxPermissionError(
                f"read_file {path!r}: symlink and non-regular paths are denied"
            ) from exc
        raise
    finally:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _walk_to_parent_dir(
    workspace: Path, parts: list[str], dir_flags: int, path: str, opened: list[int]
) -> int:
    current = os.open(workspace, dir_flags)
    opened.append(current)
    for part in parts[:-1]:
        if part == "..":
            if len(opened) == 1:
                raise SandboxPermissionError(f"read_file {path!r}: path escapes workspace")
            os.close(opened.pop())
            current = opened[-1]
            continue
        current = os.open(part, dir_flags, dir_fd=current)
        opened.append(current)
    return current


def _open_target_file(current: int, last_part: str, path: str, opened: list[int]) -> int:
    if last_part == "..":
        raise SandboxPermissionError(f"read_file {path!r}: directory paths are not readable files")
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(last_part, file_flags, dir_fd=current)
    opened.append(descriptor)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise SandboxPermissionError(
            f"read_file {path!r}: only regular, non-symlink files may be read"
        )
    if info.st_size > MAX_SANDBOX_READ_BYTES:
        raise OSError(
            errno.EFBIG,
            f"read_file {path!r} exceeds the {MAX_SANDBOX_READ_BYTES}-byte transfer cap",
        )
    return descriptor


def _read_capped(descriptor: int, path: str) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_SANDBOX_READ_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > MAX_SANDBOX_READ_BYTES:
        raise OSError(
            errno.EFBIG, f"read_file {path!r} grew beyond the transfer cap while reading"
        )
    return data


async def delete_file(instance: ProcessSandboxInstance, path: str) -> None:
    """Unlink one regular file through no-follow directory descriptors."""
    instance._alive()
    path = strip_redundant_workspace_prefix(path)

    def _delete_securely() -> None:
        import posixpath

        normalized = posixpath.normpath(path.replace("\\", "/"))
        if normalized in {"", ".", ".."} or normalized.startswith("../"):
            raise SandboxPermissionError(f"delete_file path escapes workspace: {path!r}")
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        opened: list[int] = []
        try:
            current = os.open(instance._workspace, dir_flags)
            opened.append(current)
            for part in parts[:-1]:
                current = os.open(part, dir_flags, dir_fd=current)
                opened.append(current)
            info = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise SandboxPermissionError(
                    f"delete_file {path!r}: only regular, non-symlink files may be deleted"
                )
            os.unlink(parts[-1], dir_fd=current)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                raise SandboxPermissionError(
                    f"delete_file {path!r}: symlink and non-regular paths are denied"
                ) from exc
            raise
        finally:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    await asyncio.to_thread(_delete_securely)
