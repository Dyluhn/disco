"""Low-level fd-anchored stat-identity and hashing primitives.

Leaf module: every read here re-proves the filesystem object it just
inspected is still the SAME object before trusting its bytes — the shared
TOCTOU-closing idiom used throughout `store.py`'s strict version proof.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read_regular_file_beneath(root_fd: int, rel: str, *, expected_size: int) -> bytes:
    """Read one manifest path through no-follow directory descriptors."""
    from disco.tools.projects import store

    parts = PurePosixPath(rel).parts
    if not parts:
        raise store.StorageError("verified file path is empty")
    parent_fd = os.dup(root_fd)
    file_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            except OSError as exc:
                raise store.StorageError(f"verified directory path changed: {rel}: {exc}") from exc
            os.close(parent_fd)
            parent_fd = child_fd
        # Do not let a pathname replacement with a FIFO block the server before
        # the fstat type check can reject it.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        except OSError as exc:
            raise store.StorageError(f"verified file path changed: {rel}: {exc}") from exc
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise store.StorageError(f"verified file is no longer the expected regular file: {rel}")
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining > 0:
            block = os.read(file_fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(file_fd)
        try:
            path_after = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise store.StorageError(f"verified file path changed during read: {rel}") from exc
        if _stat_identity(before) != _stat_identity(after) or _stat_identity(
            after
        ) != _stat_identity(path_after):
            raise store.StorageError(f"verified file changed during read: {rel}")
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise store.StorageError(f"verified file size changed during read: {rel}")
        return data
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)
