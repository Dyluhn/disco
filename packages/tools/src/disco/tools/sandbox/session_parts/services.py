"""BP-G9 — multi-service tracking (`SandboxSession.ensure_service`).

Generalization of the static auto-preview: the agent can register arbitrarily
many USER_PORT-binding services (an API on 3000, a Vite dev server on 5173, a
worker admin UI on 8080, …), each with its own tmux session, its own URL, and
its own rematerialize entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._container import USER_PORTS
from ..base import SandboxError

if TYPE_CHECKING:
    from ..session import SandboxSession


async def ensure_service(
    session: SandboxSession,
    name: str,
    port: int,
    command: str,
    *,
    exec_dir: str | None,
) -> str | None:
    """BP-G9 — start + track a USER_PORT-binding service.

    Generalization of `ensure_preview`: the static auto-preview is one
    such service; the agent can register arbitrarily many (an API on
    3000, a Vite dev server on 5173, a worker admin UI on 8080, …).
    Each gets its own tmux session, its own URL, and its own rematerialize
    entry — so multi-service builds are first-class, not a special case
    of the single 'preview' path.

    Behavior:
      - Refuses to track a port outside `USER_PORTS` (containment: a
        non-curated port must NEVER become a tracked/exposed URL).
      - If the port is ALREADY bound on this box (the agent's own dev
        server, or another tracker's service), returns None — no fight
        (same polite-backing-off rule as `ensure_preview`).
      - Otherwise launches `command` as a tmux session named `name`
        in `exec_dir` (default: the live workspace, same as
        `ensure_preview`) and records the service in
        `session._tracked_services[port]`. Returns the exposed URL on
        success.

    The C3 rehydrate path in `_recreate` re-issues every tracked
    service on a fresh box (the recorded `command` survives the
    tmux-level reset, just like C3's `_persistent_servers`).
    """
    from ..port_owner import port_owner
    from ..session import TrackedService

    if port not in USER_PORTS:
        raise SandboxError(f"port {port} is not in USER_PORTS; refusing to track as service")

    inst = await session._ensure()
    owner = await port_owner(inst, port)
    if owner is not None and owner.pid is not None:
        # Port already claimed by something on this box — record the
        # intent (so the wake machinery can see "we wanted a service
        # on this port") but DON'T fight the current owner. The
        # rematerialize skip-check will short-circuit on recreate.
        session._tracked_services[port] = TrackedService(
            name=name,
            port=port,
            command=command,
            exec_dir=exec_dir,
        )
        return None

    cwd = exec_dir
    if cwd is None:
        res = await inst.exec_shell("pwd", timeout_s=5)
        cwd = res.stdout.strip()

    await session.sessions.exec(name, command, exec_dir=cwd)
    session._tracked_services[port] = TrackedService(
        name=name,
        port=port,
        command=command,
        exec_dir=cwd,
    )
    return inst.expose_port(port)
