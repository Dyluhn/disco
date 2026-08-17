"""Workspace tree hashing: the mutable-mirror scan (skips secrets + symlinks)
and the strict immutable-version scan (rejects them instead).

`_scan_immutable_tree_details` is split into a per-file hash-and-prove step
and a final membership/identity re-check, mirroring the original function's
two phases exactly — this is what brings its cyclomatic complexity back
under budget; relocating the whole function unchanged would not have.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from disco.core.secret_paths import is_runtime_secret_path

from .fs_proof import _file_sha256, _stat_identity


def _scan_tree(root: Path) -> tuple[dict[str, str], int]:
    from disco.tools.projects import store

    if not root.is_dir():
        raise store.StorageError(f"workspace directory missing: {root}")
    hashes: dict[str, str] = {}
    total_bytes = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if is_runtime_secret_path(rel):
            continue
        entry_stat = path.stat()
        hashes[rel] = _file_sha256(path)
        total_bytes += entry_stat.st_size
    return hashes, total_bytes


def _hash_immutable_file(
    path: Path, entry_stat: os.stat_result, rel: str
) -> tuple[str, int, tuple[int, int, int, int, int, int, int]]:
    """Open, hash, and prove one immutable-tree file did not change while it
    was being hashed. Returns ``(sha256_hex, size, post-hash identity)``."""
    from disco.tools.projects import store

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise store.StorageError(f"immutable workspace file unreadable: {rel}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise store.StorageError(f"immutable workspace contains non-regular entry: {rel}")
        if _stat_identity(before) != _stat_identity(entry_stat):
            raise store.StorageError(f"immutable workspace path changed before hashing: {rel}")
        h = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            h.update(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise store.StorageError(f"immutable workspace path changed while hashing: {rel}") from exc
    identity_before = _stat_identity(before)
    identity_after = _stat_identity(after)
    if identity_before != identity_after:
        raise store.StorageError(f"immutable workspace file changed while hashing: {rel}")
    if identity_after != _stat_identity(path_after):
        raise store.StorageError(f"immutable workspace path changed while hashing: {rel}")
    return h.hexdigest(), after.st_size, identity_after


def _assert_immutable_tree_membership_unchanged(
    root: Path,
    initial_paths: tuple[str, ...],
    root_before: os.stat_result,
    identities: dict[str, tuple[int, int, int, int, int, int, int]],
) -> None:
    from disco.tools.projects import store

    try:
        final_entries = sorted(root.rglob("*"))
        root_after = root.lstat()
    except OSError as exc:
        raise store.StorageError(f"immutable workspace changed while hashing: {exc}") from exc
    final_paths = tuple(path.relative_to(root).as_posix() for path in final_entries)
    if final_paths != initial_paths or _stat_identity(root_after) != _stat_identity(root_before):
        raise store.StorageError("immutable workspace membership changed while hashing")
    for path in final_entries:
        rel = path.relative_to(root).as_posix()
        try:
            identity = _stat_identity(path.lstat())
        except OSError as exc:
            raise store.StorageError(
                f"immutable workspace entry changed after hashing: {rel}"
            ) from exc
        if identities.get(rel) != identity:
            raise store.StorageError(f"immutable workspace entry changed after hashing: {rel}")


def _scan_immutable_tree_details(root: Path) -> tuple[dict[str, tuple[str, int]], int]:
    """Hash a published version tree without following or ignoring symlinks.

    ``_scan_tree`` intentionally skips runtime-secret paths and symlinks while
    scanning the mutable mirror.  Neither is acceptable in an immutable
    version: silently omitting one would let the recorded digest describe fewer
    bytes than a consumer can actually reach.
    """
    from disco.tools.projects import store

    if root.is_symlink() or not root.is_dir():
        raise store.StorageError(f"immutable workspace directory missing or symlinked: {root}")

    details: dict[str, tuple[str, int]] = {}
    total_bytes = 0
    try:
        root_before = root.lstat()
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        raise store.StorageError(f"immutable workspace unreadable: {exc}") from exc
    initial_paths = tuple(path.relative_to(root).as_posix() for path in entries)
    identities: dict[str, tuple[int, int, int, int, int, int, int]] = {}

    for path in entries:
        rel = path.relative_to(root).as_posix()
        try:
            entry_stat = path.lstat()
        except OSError as exc:
            raise store.StorageError(f"immutable workspace entry unreadable: {rel}: {exc}") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise store.StorageError(f"immutable workspace contains symlink: {rel}")
        if stat.S_ISDIR(entry_stat.st_mode):
            identities[rel] = _stat_identity(entry_stat)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise store.StorageError(f"immutable workspace contains non-regular entry: {rel}")
        if is_runtime_secret_path(rel):
            raise store.StorageError(f"immutable workspace contains runtime secret: {rel}")

        digest, size, identity = _hash_immutable_file(path, entry_stat, rel)
        identities[rel] = identity
        details[rel] = (digest, size)
        total_bytes += size

    _assert_immutable_tree_membership_unchanged(root, initial_paths, root_before, identities)
    return details, total_bytes


def _scan_immutable_tree(root: Path) -> tuple[dict[str, str], int]:
    details, total_bytes = _scan_immutable_tree_details(root)
    return {rel: digest for rel, (digest, _size) in details.items()}, total_bytes


def _tree_digest_from_hashes(file_hashes: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(file_hashes):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(file_hashes[rel].encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def tree_digest(root: Path) -> str:
    """Stable sha256 over sorted workspace-relative path + file-sha256 pairs."""
    file_hashes, _ = _scan_tree(root)
    return _tree_digest_from_hashes(file_hashes)


def tree_digest_of_files(files: Mapping[str, bytes]) -> str:
    """`tree_digest` over an in-memory {rel: bytes} snapshot — same file-set +
    algorithm as `tree_digest(root)`. For verifying a source tree that was read
    once into memory (no re-traversal, so no read-vs-read TOCTOU)."""
    return _tree_digest_from_hashes(
        {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
    )
