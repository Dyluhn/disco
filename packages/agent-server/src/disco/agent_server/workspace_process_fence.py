"""Cross-process serialization for one mutable workspace.

The per-conversation ``asyncio.Lock`` owned by ``WorkspaceCoordinator`` is the
in-process ordering primitive.  This module supplies the matching OS-backed
fence for installations that run more than one Agent server process.  The lock
is deliberately keyed exactly like the historical Cloudflare deploy lock so
deploys and agent ingress cannot drift into separate coordination domains.

There is no TTL.  ``flock`` ownership follows the open descriptor and is
released by cancellation, normal exit, or process death; stealing a live lock
by wall-clock age would allow the old process to keep mutating remote state.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows; strict fence unavailable
    _fcntl = None  # type: ignore[assignment]


class WorkspaceProcessFenceUnavailable(RuntimeError):
    """The host cannot provide the required cross-process workspace fence."""


class WorkspaceProcessBusy(RuntimeError):
    """Another process currently owns the mutable-workspace critical section."""


@dataclass
class _FenceToken:
    key: str
    fds: set[int]


_ACTIVE_TOKENS: ContextVar[tuple[_FenceToken, ...]] = ContextVar(
    "disco_workspace_process_fences",
    default=(),
)
_LOCK_DIR_ENV = "DISCO_DEPLOY_LOCK_DIR"


def _validate_lock_directory_chain(base: Path, *, euid: int | None) -> None:
    """Reject path components another host principal can replace or retarget.

    Checking only ``base`` is not sufficient: an otherwise safe leaf below an
    ancestor symlink can resolve to a different inode for the next process and
    split one logical fence.  A sticky system directory such as ``/tmp`` is the
    one writable-parent exception; its ownership rules protect an already
    created, process-owned child entry.
    """

    current = Path(base.anchor)
    for part in base.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise WorkspaceProcessFenceUnavailable(
                f"workspace process-fence path component is unavailable: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorkspaceProcessFenceUnavailable(
                "workspace process-fence directory has a symlinked ancestor"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceProcessFenceUnavailable(
                "workspace process-fence path component is not a directory"
            )
        if euid is not None and info.st_uid not in {0, euid}:
            raise WorkspaceProcessFenceUnavailable(
                "workspace process-fence path component is not owned by this process user"
            )
        writable_by_peer = bool(info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        sticky_parent = current != base and bool(info.st_mode & stat.S_ISVTX)
        if writable_by_peer and not sticky_parent:
            raise WorkspaceProcessFenceUnavailable(
                "workspace process-fence path component is group/world writable"
            )


def workspace_process_lock_dir() -> Path:
    """Return the one server-controlled lock directory used by every caller."""

    override = os.environ.get(_LOCK_DIR_ENV, "").strip()
    if override:
        base = Path(override)
    else:
        xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        if xdg_state:
            base = Path(xdg_state) / "disco" / "deploy-locks"
        elif xdg_cfg:
            base = Path(xdg_cfg) / "disco" / "deploy-locks"
        else:
            base = Path(tempfile.gettempdir()) / "disco-deploy-locks"
    if not base.is_absolute():
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence directory must be an absolute path"
        )
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = base.lstat()
    except OSError as exc:
        raise WorkspaceProcessFenceUnavailable(
            f"workspace process-fence directory is unavailable: {exc}"
        ) from exc
    if base.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence directory is missing or symlinked"
        )
    geteuid = getattr(os, "geteuid", None)
    euid: int | None = None
    if callable(geteuid):
        detected_euid = geteuid()
        if not isinstance(detected_euid, int):
            raise WorkspaceProcessFenceUnavailable(
                "workspace process-fence owner identity is unavailable"
            )
        euid = detected_euid
    _validate_lock_directory_chain(base, euid=euid)
    if euid is not None and info.st_uid != euid:
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence directory is not owned by this process user"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence directory is group/world writable"
        )
    return base


def workspace_process_lock_path(workspace: Path) -> Path:
    """Derive the canonical, non-disclosing lock path for *workspace*."""

    try:
        key = str(workspace.resolve())
    except OSError:
        key = str(workspace.absolute())
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return workspace_process_lock_dir() / f"{digest}.lock"


def _active_token(key: str) -> _FenceToken | None:
    return next(
        (token for token in reversed(_ACTIVE_TOKENS.get()) if token.fds and token.key == key),
        None,
    )


@contextmanager
def _retain_inherited_fence(token: _FenceToken) -> Iterator[None]:
    """Keep the OS lock alive when a nested or inherited task outlives its parent."""

    try:
        retained = os.dup(next(iter(token.fds)))
    except (OSError, StopIteration) as exc:
        raise WorkspaceProcessFenceUnavailable(
            "inherited workspace process fence is no longer live"
        ) from exc
    token.fds.add(retained)
    try:
        yield
    finally:
        token.fds.discard(retained)
        os.close(retained)


def workspace_process_fence_held(workspace: Path) -> bool:
    """Whether the current inherited task scope is inside this workspace fence."""

    return _active_token(str(workspace_process_lock_path(workspace))) is not None


def _open_lock(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags, 0o600)
        opened = os.fstat(fd)
        named = path.lstat()
    except OSError as exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise WorkspaceProcessFenceUnavailable(
            f"workspace process fence could not be opened: {exc}"
        ) from exc
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        named.st_dev,
        named.st_ino,
    ):
        os.close(fd)
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence path changed or is not a regular file"
        )
    return fd


def _acquire_nonblocking(fd: int, path: Path) -> None:
    if _fcntl is None:
        raise WorkspaceProcessFenceUnavailable(
            "cross-process workspace fencing requires POSIX flock"
        )
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise WorkspaceProcessBusy("another process is mutating this workspace") from exc
        raise WorkspaceProcessFenceUnavailable(
            f"workspace process fence could not be acquired: {exc}"
        ) from exc
    opened = os.fstat(fd)
    try:
        named = path.lstat()
    except OSError as exc:
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence path disappeared after acquisition"
        ) from exc
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        named.st_dev,
        named.st_ino,
    ):
        raise WorkspaceProcessFenceUnavailable(
            "workspace process-fence identity changed after acquisition"
        )


def _release(fd: int) -> None:
    # Do not issue explicit LOCK_UN: inherited tasks retain dup() descriptors to
    # the same open-file description, and closing the last one is the exact
    # lifetime boundary at which the kernel should release the flock.
    os.close(fd)


@contextmanager
def try_workspace_process_fence(workspace: Path) -> Iterator[None]:
    """Acquire immediately or raise :class:`WorkspaceProcessBusy`."""

    path = workspace_process_lock_path(workspace)
    key = str(path)
    active = _active_token(key)
    if active is not None:
        with _retain_inherited_fence(active):
            yield
        return
    fd = _open_lock(path)
    token = _FenceToken(key, {fd})
    reset = None
    try:
        _acquire_nonblocking(fd, path)
        reset = _ACTIVE_TOKENS.set((*_ACTIVE_TOKENS.get(), token))
        yield
    finally:
        if reset is not None:
            _ACTIVE_TOKENS.reset(reset)
        token.fds.discard(fd)
        _release(fd)


@asynccontextmanager
async def workspace_process_fence(
    workspace: Path,
    *,
    wait: bool,
    poll_interval: float = 0.05,
) -> AsyncIterator[None]:
    """Acquire without blocking the event loop, optionally waiting for a peer."""

    path = workspace_process_lock_path(workspace)
    key = str(path)
    active = _active_token(key)
    if active is not None:
        with _retain_inherited_fence(active):
            yield
        return
    fd = _open_lock(path)
    token = _FenceToken(key, {fd})
    reset = None
    try:
        while True:
            try:
                _acquire_nonblocking(fd, path)
                break
            except WorkspaceProcessBusy:
                if not wait:
                    raise
                await asyncio.sleep(poll_interval)
        reset = _ACTIVE_TOKENS.set((*_ACTIVE_TOKENS.get(), token))
        yield
    finally:
        if reset is not None:
            _ACTIVE_TOKENS.reset(reset)
        token.fds.discard(fd)
        _release(fd)


__all__ = [
    "WorkspaceProcessBusy",
    "WorkspaceProcessFenceUnavailable",
    "try_workspace_process_fence",
    "workspace_process_fence",
    "workspace_process_fence_held",
    "workspace_process_lock_dir",
    "workspace_process_lock_path",
]
