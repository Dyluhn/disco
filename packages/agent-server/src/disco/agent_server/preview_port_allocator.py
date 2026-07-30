"""Kernel-backed preview port allocation and lease ownership."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import os
import shlex
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .preview_models import (
    _PORT_BIND_TEST_SRC,
    _TMUX_PREFIX,
    NoPreviewPortAvailableError,
    PreviewSession,
    PreviewStatus,
    _fcntl,
    _PreviewPortPool,
)


@dataclass
class _PreviewPortLease:
    """Kernel-backed reservation for one shared-host preview port."""

    fd: int
    path: str

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)


class _PreviewPortResource(ABC):
    """State and leases for one conversation-scoped preview resource."""

    MAX_RESTARTS = 3

    def __init__(
        self,
        sandbox: Any,
        *,
        port_pool: _PreviewPortPool | None = None,
        health_attempts: int = 10,
        health_interval_s: float = 0.3,
        supervise_interval_s: float = 4.0,
        owns_sandbox: bool = False,
    ) -> None:
        self._sandbox = sandbox
        self._owns_sandbox = owns_sandbox
        self._pool: list[int] = (
            list(port_pool.ports) if port_pool is not None else _default_port_pool(sandbox)
        )
        self._health_attempts = max(1, health_attempts)
        self._health_interval_s = health_interval_s
        self._supervise_interval_s = supervise_interval_s
        self._sessions: dict[str, PreviewSession] = {}
        # Explicit successful preview_start selections, oldest to newest. The
        # canonical UI route follows the newest still-servable selection; supervisor
        # restarts never reorder it, while an idempotent explicit start does.
        self._selection_order: list[str] = []
        self._lock = asyncio.Lock()
        self._supervisor: asyncio.Task[None] | None = None
        self._closed = False
        self._auto_preview_coordinated = False  # P1 #2: legacy auto-preview stood down once
        # Process sandboxes share one host network across conversations and Agent
        # server processes. Socket probing alone has a check/use gap, so keep a
        # kernel lease for every allocated port through the preview lifecycle.
        self._port_leases: dict[int, _PreviewPortLease] = {}
        # Host-only bindings from an immutable final workspace contract to the
        # freshly revalidated runtime session serving those exact bytes.
        self._sealed_contracts: dict[str, tuple[str, dict[str, Any]]] = {}

    @abstractmethod
    async def _port_owner(self, port: int) -> Any | None: ...

    @abstractmethod
    async def _probe_health(self, port: int, *, require_success: bool = False) -> bool: ...

    @abstractmethod
    async def _stop_locked(self, name: str) -> str: ...

    async def _allocate_port(self, *, reclaim_name: str | None = None) -> int:
        """THE single place a preview port is chosen. The model has no input here — the
        port comes from the curated platform pool, skipping (a) any port already held by
        one of our previews, (b) any port the SANDBOX is already tracking as a service —
        the legacy static auto-preview or an agent-launched dev server (P1 #2: allocating
        onto one would mean the legacy server's response falsely validates ours), and
        (c) any port with a LIVE listener right now (a real owner we don't track), as
        proven from INSIDE the sandbox by socket ownership / bindability rather than
        HTTP health alone. This is the ownership point that kills port-fixation AND
        cross-server contamination."""
        taken = {s.port for s in self._sessions.values() if s.status is not PreviewStatus.STOPPED}
        taken |= self._sandbox_tracked_ports()
        checked_count = 0
        checked_sample: list[int] = []
        for port in self._pool:
            if port in taken:
                continue
            checked_count += 1
            if len(checked_sample) < 20:
                checked_sample.append(port)
            lease: _PreviewPortLease | None = None
            if self._shares_host_network():
                lease = self._try_host_port_lease(port)
                if lease is None:
                    continue
            # A real listener already owns this curated port (not one of ours) -> skip it,
            # or its response would falsely validate a process that EADDRINUSE'd. HTTP
            # health is only the last fallback; the primary checks are in-sandbox socket
            # ownership and bindability.
            try:
                if not await self._port_available_for_allocation(port, reclaim_name=reclaim_name):
                    continue
                if lease is not None:
                    self._port_leases[port] = lease
                    lease = None
                return port
            finally:
                if lease is not None:
                    lease.close()
        raise NoPreviewPortAvailableError(
            "no free platform preview port found after checking "
            f"{checked_count} candidate(s)"
            + (f" (first candidates: {checked_sample})" if checked_sample else "")
        )

    def _shares_host_network(self) -> bool:
        """Whether this manager competes for one host-wide TCP namespace."""

        explicit = getattr(self._sandbox, "shares_host_network", None)
        if isinstance(explicit, bool):
            return explicit
        return (getattr(self._sandbox, "backend_name", "") or "") == "process"

    @staticmethod
    def _try_host_port_lease(port: int) -> _PreviewPortLease | None:
        """Acquire ``port`` without waiting; a busy lease selects another port."""

        if _fcntl is None:
            raise NoPreviewPortAvailableError(
                "shared-host preview allocation requires POSIX file locking"
            )
        from .workspace_process_fence import workspace_process_lock_dir

        path = workspace_process_lock_dir() / f"preview-port-{int(port)}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(path, flags, 0o600)
            opened = os.fstat(fd)
            named = path.lstat()
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                named.st_dev,
                named.st_ino,
            ):
                raise OSError("preview port lease changed identity or is not a regular file")
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            raise NoPreviewPortAvailableError(
                f"shared-host preview port {port} could not be reserved: {exc}"
            ) from exc
        return _PreviewPortLease(fd=fd, path=str(path))

    def _release_port_lease(self, port: int) -> None:
        lease = self._port_leases.pop(port, None)
        if lease is not None:
            lease.close()

    async def _port_available_for_allocation(
        self, port: int, *, reclaim_name: str | None = None
    ) -> bool:
        """Return True only when `port` is genuinely free in the sandbox.

        `_probe_health` can miss non-HTTP listeners or listeners that are wedged before
        serving a response. Allocation therefore checks the socket owner first, optionally
        reclaims a stale preview that belongs to this conversation, then bind-tests the
        port from inside the sandbox when that seam is available.
        """
        owner = await self._port_owner(port)
        if owner is not None and getattr(owner, "pid", None) is not None:
            if await self._reclaim_stale_preview_port(port, owner, reclaim_name=reclaim_name):
                owner = await self._port_owner(port)
                if owner is not None and getattr(owner, "pid", None) is not None:
                    return False
            else:
                return False

        bindable = await self._bind_test_port(port)
        if bindable is not None:
            return bindable

        if owner is not None:
            return getattr(owner, "pid", None) is None

        # Last fallback for old/fake sandboxes without exec-shell attribution. Keep the
        # previous behavior here, but only after stronger in-sandbox occupancy checks fail.
        return not await self._probe_health(port)

    async def _bind_test_port(self, port: int) -> bool | None:
        """Try to bind `port` inside the sandbox. True means bindable/free, False means
        occupied, None means this sandbox cannot run the bind probe."""
        exec_shell = getattr(self._sandbox, "exec_shell", None)
        if exec_shell is None:
            return None
        try:
            res = await exec_shell(
                f"python3 -c {shlex.quote(_PORT_BIND_TEST_SRC)} {int(port)}",
                timeout_s=5,
            )
        except Exception:  # noqa: BLE001 — fall back to owner/health probes
            return None
        return getattr(res, "exit_code", 1) == 0

    async def _reclaim_stale_preview_port(
        self, port: int, owner: Any, *, reclaim_name: str | None = None
    ) -> bool:
        """If `port` is held by a stale preview from this same conversation, stop that
        preview and let allocation reuse the port. Foreign, unattributed, and actively
        tracked previews are never reclaimed here."""
        owner_session = getattr(owner, "session", None)
        stale_name = self._stale_preview_name_for_owner(
            port, str(owner_session) if owner_session else None, reclaim_name=reclaim_name
        )
        if stale_name is None:
            return False

        if stale_name in self._sessions:
            await self._stop_locked(stale_name)
        else:
            try:
                await self._sandbox.sessions.kill_foreground(stale_name)
            except Exception:  # noqa: BLE001 — failed reclaim means "not available"
                return False
        return True

    def _stale_preview_name_for_owner(
        self, port: int, owner_session: str | None, *, reclaim_name: str | None = None
    ) -> str | None:
        if not owner_session:
            return None
        ns = getattr(getattr(self._sandbox, "sessions", None), "namespace", "") or ""
        prefix = f"{_TMUX_PREFIX}-{ns}"
        if not owner_session.startswith(prefix):
            return None
        owner_name = owner_session[len(prefix) :]
        known = self._sessions.get(owner_name)
        if known is not None and known.port == port and known.status is PreviewStatus.STOPPED:
            return owner_name
        if reclaim_name is not None and owner_name == reclaim_name:
            return owner_name
        return None

    def _sandbox_tracked_ports(self) -> set[int]:
        """Curated ports the SANDBOX itself is already tracking (the static auto-preview
        on 8000, any `ensure_service` dev server). Consulted so allocation never lands on
        a port a legacy/coexisting server owns. Best-effort: a fake/old sandbox without
        the accessor contributes nothing (the live-listener probe still guards us)."""
        accessor = getattr(self._sandbox, "tracked_ports", None)
        if accessor is None:
            return set()
        try:
            return set(accessor())
        except Exception:  # noqa: BLE001 — never let a tracking read break allocation
            return set()


def _default_port_pool(sandbox: Any) -> list[int]:
    """The curated platform preview-port pool, in preference order.

    Built from the SAME curated ports the rest of the platform exposes (USER_PORTS),
    minus the noVNC bridge. On a SHARED-host backend (process/local) the agent-server's
    own control ports (8000/8800/5173) are excluded so the platform can never allocate
    a preview onto a port the dev stack already owns — keeping the model out of port
    selection must not put the PLATFORM into a port collision either."""
    from disco.core.loop.preview_target import (
        PREVIEW_PORTS,
        managed_host_preview_ports,
        reserved_control_ports,
    )
    from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS

    ordered = [p for p in PREVIEW_PORTS if p in USER_PORTS and p != NOVNC_PORT]
    explicit = getattr(sandbox, "shares_host_network", None)
    host_shared = (
        explicit
        if isinstance(explicit, bool)
        else (getattr(sandbox, "backend_name", "") or "") == "process"
    )
    if host_shared:
        reserved = reserved_control_ports()
        ordered = [p for p in ordered if p not in reserved]
        dynamic = list(managed_host_preview_ports())
        # Conversations start at different points in the large range, avoiding
        # a herd on its first socket while retaining deterministic allocation.
        identity = str(getattr(sandbox, "conversation_id", "") or "")
        if dynamic and identity:
            offset = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") % len(
                dynamic
            )
            dynamic = dynamic[offset:] + dynamic[:offset]
        ordered.extend(port for port in dynamic if port not in ordered)
    return ordered
