"""Preserve-path copy interior for `snapshot_workspace`: fd-anchored,
no-follow copy of unreadable/host-owned prior paths into a fresh staging
tree.

Split out of `archive.py` (house pattern for oversized modules/callables) —
the original `_copy_preserved_snapshot_paths` plus its nested `_copy_entry`
closure carried nearly all of the module's cyclomatic complexity in one
place. The split here is a pure decomposition of that SAME branching into
smaller named callables (a dispatcher plus a directory handler and a file
handler); every guard, exception type, and error message is reproduced
verbatim and in original order. The former nested-closure parameters
(`directory_flags`, `file_flags`, `max_depth`, `max_file_bytes`) are now
explicit keyword arguments instead of captured cell variables.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from disco.core.secret_paths import is_runtime_secret_path


def _prepare_authoritative_snapshot_paths(
    staging: Path,
    authoritative_paths: set[str],
    *,
    max_depth: int,
) -> None:
    """Remove fresh file-shaped conflicts above server-owned snapshot roots."""
    from disco.tools.projects import archive

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
        rel = archive._archive_relpath(raw, max_depth=max_depth)
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


def _excluded(rel: str) -> bool:
    from disco.tools.projects import archive

    parts = PurePosixPath(rel).parts
    return is_runtime_secret_path(rel) or any(
        part in archive._SNAPSHOT_EXCLUDED_DIRS for part in parts
    )


def _open_prior_snapshot_root(dest: Path, *, directory_flags: int) -> int | None:
    """Open `dest` as a no-follow directory fd, proving it is still the same
    filesystem object that was just lstat'd. Returns None (no error) when the
    prior snapshot simply does not exist yet — that is not a preservation
    failure, it's "nothing to preserve from"."""
    from disco.tools.projects import archive

    try:
        root_listed = os.stat(dest, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise archive.WorkspaceArchiveError(f"cannot inspect prior snapshot safely: {exc}") from exc
    try:
        root_fd = os.open(dest, directory_flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise archive.WorkspaceArchiveError(f"cannot open prior snapshot safely: {exc}") from exc

    root_opened = os.fstat(root_fd)
    if not stat.S_ISDIR(root_opened.st_mode) or not _same_fs_object(root_listed, root_opened):
        os.close(root_fd)
        raise archive.WorkspaceArchiveError("prior snapshot changed before it could be preserved")
    return root_fd


def _copy_preserved_entry(
    parent_fd: int,
    name: str,
    rel: str,
    target: Path,
    depth: int,
    *,
    directory_flags: int,
    file_flags: int,
    max_depth: int,
    max_file_bytes: int,
) -> None:
    from disco.tools.projects import archive

    if _excluded(rel):
        return
    if depth > max_depth:
        raise archive.WorkspaceArchiveError(
            f"preserved workspace depth exceeded the {max_depth}-level cap at {rel!r}"
        )
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise archive.WorkspaceArchiveError(
            f"cannot inspect preserved path {rel!r}: {exc}"
        ) from exc

    if stat.S_ISDIR(info.st_mode):
        _copy_preserved_directory(
            parent_fd,
            name,
            rel,
            target,
            depth,
            info,
            directory_flags=directory_flags,
            file_flags=file_flags,
            max_depth=max_depth,
            max_file_bytes=max_file_bytes,
        )
        return

    _copy_preserved_file(
        parent_fd, name, rel, target, info, file_flags=file_flags, max_file_bytes=max_file_bytes
    )


def _copy_preserved_directory(
    parent_fd: int,
    name: str,
    rel: str,
    target: Path,
    depth: int,
    info: os.stat_result,
    *,
    directory_flags: int,
    file_flags: int,
    max_depth: int,
    max_file_bytes: int,
) -> None:
    from disco.tools.projects import archive

    if target.exists() and not target.is_dir():
        return
    try:
        child_fd = os.open(name, directory_flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise archive.WorkspaceArchiveError(
            f"cannot open preserved directory {rel!r} safely: {exc}"
        ) from exc
    opened = os.fstat(child_fd)
    if not stat.S_ISDIR(opened.st_mode) or not _same_fs_object(info, opened):
        os.close(child_fd)
        raise archive.WorkspaceArchiveError(
            f"preserved directory {rel!r} changed before it could be copied"
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        for child in sorted(os.listdir(child_fd)):
            child_rel = f"{rel}/{child}"
            _copy_preserved_entry(
                child_fd,
                child,
                child_rel,
                target / child,
                depth + 1,
                directory_flags=directory_flags,
                file_flags=file_flags,
                max_depth=max_depth,
                max_file_bytes=max_file_bytes,
            )
    finally:
        os.close(child_fd)


def _copy_preserved_file(
    parent_fd: int,
    name: str,
    rel: str,
    target: Path,
    info: os.stat_result,
    *,
    file_flags: int,
    max_file_bytes: int,
) -> None:
    from disco.tools.projects import archive

    if not stat.S_ISREG(info.st_mode) or target.exists():
        return
    if info.st_nlink != 1:
        raise archive.WorkspaceArchiveError(f"preserved file {rel!r} is hardlinked")
    try:
        source_fd = os.open(name, file_flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise archive.WorkspaceArchiveError(
            f"cannot open preserved file {rel!r} safely: {exc}"
        ) from exc
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_fs_object(info, opened):
            raise archive.WorkspaceArchiveError(
                f"preserved file {rel!r} changed before it could be copied"
            )
        if opened.st_nlink != 1:
            raise archive.WorkspaceArchiveError(f"preserved file {rel!r} is hardlinked")
        if opened.st_size > max_file_bytes:
            raise archive.WorkspaceArchiveError(
                f"preserved file {rel!r} exceeds the {max_file_bytes}-byte cap"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with os.fdopen(os.dup(source_fd), "rb") as source, target.open("xb") as output:
            while block := source.read(1024 * 1024):
                copied += len(block)
                if copied > max_file_bytes:
                    raise archive.WorkspaceArchiveError(
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
            raise archive.WorkspaceArchiveError(f"preserved file {rel!r} changed while copied")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)


def _copy_one_preserved_path(
    root_fd: int,
    raw: str,
    staging: Path,
    *,
    directory_flags: int,
    file_flags: int,
    max_depth: int,
    max_file_bytes: int,
) -> None:
    from disco.tools.projects import archive

    rel = archive._archive_relpath(raw, max_depth=max_depth)
    if _excluded(rel):
        return
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
        return
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
                raise archive.WorkspaceArchiveError(
                    f"cannot traverse preserved path {rel!r} safely: {exc}"
                ) from exc
            opened_dirs.append(parent_fd)
            opened = os.fstat(parent_fd)
            if not stat.S_ISDIR(opened.st_mode) or not _same_fs_object(listed, opened):
                raise archive.WorkspaceArchiveError(
                    f"preserved path {rel!r} changed during traversal"
                )
        if not missing:
            _copy_preserved_entry(
                parent_fd,
                parts[-1],
                rel,
                staging.joinpath(*parts),
                len(parts) - 1,
                directory_flags=directory_flags,
                file_flags=file_flags,
                max_depth=max_depth,
                max_file_bytes=max_file_bytes,
            )
    finally:
        for opened_fd in reversed(opened_dirs):
            os.close(opened_fd)


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
    root_fd = _open_prior_snapshot_root(dest, directory_flags=directory_flags)
    if root_fd is None:
        return

    try:
        for raw in sorted(preserve_paths):
            _copy_one_preserved_path(
                root_fd,
                raw,
                staging,
                directory_flags=directory_flags,
                file_flags=file_flags,
                max_depth=max_depth,
                max_file_bytes=max_file_bytes,
            )
    finally:
        os.close(root_fd)
