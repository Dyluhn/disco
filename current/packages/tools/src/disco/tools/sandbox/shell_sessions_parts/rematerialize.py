"""C3 replay of the persistent-server registry (`ShellSessionManager`).

`rehydrate_persistent_servers` re-issues each recorded persistent-server
command on the current instance after a sandbox recreate; `stop_foreground_server`
revokes one exact tracked generation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..shell_sessions import ShellSessionManager

_LOG = logging.getLogger(__name__)


async def stop_foreground_server(
    manager: ShellSessionManager,
    name: str,
    *,
    expected_command: str,
    expected_port: int,
) -> str:
    """Stop and revoke one exact foreground-server generation.

    Object identity is captured before the await because a newer same-name
    generation may legitimately reuse the exact command and port.
    """
    current = manager._persistent_servers.get(name)
    expected = (
        current
        if current is not None
        and current.command == expected_command
        and current.port == expected_port
        else None
    )
    try:
        return await manager.kill_foreground(name)
    finally:
        # Stop intent revokes this captured generation even when transport
        # fails ambiguously after accepting the signal. Identity still protects
        # any replacement registered while the awaited stop was in flight.
        if expected is not None and manager._persistent_servers.get(name) is expected:
            manager._persistent_servers.pop(name, None)


async def rehydrate_persistent_servers(
    manager: ShellSessionManager, port_check: Callable[[int], Awaitable[bool]] | None = None
) -> list[str]:
    """Re-issue each recorded persistent-server command on the CURRENT
    instance. Called by `SandboxSession._recreate` after a fresh instance
    is up so a real app (vite / express / uvicorn / http.server on a
    non-default port) survives suspend/wake, not just the static
    auto-preview.

    `port_check(port)` -> True iff a USER_PORT is already bound on the
    fresh instance. We SKIP those to avoid duplicating a server that's
    already running (the no-duplication guarantee). Defaults to a /proc
    probe via `port_owner`.

    Best-effort: any single failure is logged and the rehydrate continues
    with the next entry. The function NEVER raises — rehydrate is a
    convenience, like the static auto-preview, and the box must not care.
    Returns a list of human-readable log lines for diagnostics/tests.
    """
    from ..port_owner import port_owner

    if port_check is None:

        async def _default_check(port: int) -> bool:
            try:
                inst = await manager._get_instance()
            except Exception:  # noqa: BLE001 — instance gone; just attempt rehydrate
                return False
            try:
                owner = await port_owner(inst, port)
            except Exception:  # noqa: BLE001 — probe failed; treat as not bound
                return False
            return owner is not None and owner.pid is not None

        port_check = _default_check

    logs: list[str] = []
    # Snapshot the keys so the dict isn't mutated mid-iteration by the
    # recording that happens inside the re-issued exec() call.
    for name, srv in list(manager._persistent_servers.items()):
        try:
            if await port_check(srv.port):
                logs.append(
                    f"port {srv.port} already bound on the new instance — "
                    f"skipping rehydrate of session '{name}' "
                    f"(cmd: {srv.command})"
                )
                continue
        except Exception as exc:  # noqa: BLE001 — port-check failure is non-fatal
            logs.append(
                f"port-check for {srv.port} failed while rehydrating "
                f"'{name}': {exc!r} — attempting re-issue anyway"
            )
        try:
            await manager.exec(name, srv.command, srv.exec_dir)
        except Exception as exc:  # noqa: BLE001 — best-effort; one bad rehydrate must not block the rest
            logs.append(
                f"failed to re-materialize persistent server '{name}' "
                f"(cmd: {srv.command}): {exc!r}"
            )
            _LOG.warning("rehydrate of persistent server %r failed: %r", name, exc)
            continue
        logs.append(
            f"re-materialized persistent server '{name}' on port {srv.port} "
            f"(cmd: {srv.command})"
        )
    return logs
