"""Durable JSON write/read: crash-safe tmp+rename writes, no-follow bounded
reads with full before/after identity proof.

Sibling of `fs_proof` (uses its `_stat_identity`); both are leaf primitives
the rest of `store.py`'s collaborators build on.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .fs_proof import _stat_identity


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(path: Path) -> None:
    """Flush every staged file and directory before publishing its name."""
    from disco.tools.projects import store

    entries = sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for entry in entries:
        entry_stat = entry.lstat()
        if stat.S_ISLNK(entry_stat.st_mode):
            raise store.StorageError(f"cannot sync symlinked staged entry: {entry}")
        if stat.S_ISREG(entry_stat.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(entry, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif not stat.S_ISDIR(entry_stat.st_mode):
            raise store.StorageError(f"cannot sync non-regular staged entry: {entry}")
    for directory in sorted(
        (item for item in entries if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
    _fsync_directory(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Durably replace JSON without following a caller-planted temp symlink."""
    from disco.tools.projects import store

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise store.StorageError(f"JSON parent directory is missing or symlinked: {path.parent}")
    # Remove the predictable temp name used by the legacy writer. Unlinking a
    # planted symlink removes the link itself, never its target; the new writer
    # then uses a unique O_EXCL tempfile instead.
    legacy_tmp = path.with_suffix(path.suffix + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        legacy_tmp.unlink()
    encoded = json.dumps(payload, indent=2).encode("utf-8")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _read_json_regular_nofollow(
    path: Path,
    *,
    label: str,
    missing_ok: bool = False,
    max_bytes: int = 16 * 1024 * 1024,
) -> Any:
    """Read bounded JSON from one stable regular-file identity."""
    from disco.tools.projects import store

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return store._MISSING_JSON
        raise store.StorageError(f"{label} is missing") from None
    except OSError as exc:
        raise store.StorageError(
            f"{label} is symlinked or could not be opened safely: {exc}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise store.StorageError(f"{label} is not a regular file")
        if before.st_size > max_bytes:
            raise store.StorageError(f"{label} exceeds its size bound")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(fd)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise store.StorageError(f"{label} changed while it was read") from exc
        if _stat_identity(before) != _stat_identity(after) or _stat_identity(
            after
        ) != _stat_identity(path_after):
            raise store.StorageError(f"{label} changed while it was read")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise store.StorageError(f"{label} size changed while it was read")
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise store.StorageError(f"{label} contains invalid JSON: {exc}") from exc
    finally:
        os.close(fd)
