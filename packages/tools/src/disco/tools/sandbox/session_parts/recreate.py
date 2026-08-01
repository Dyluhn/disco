"""Mid-session death recovery (`SandboxSession._recreate`).

The load-bearing behavior (carried lesson #1 from the sandbox arc): a box can
die MID-SESSION — an OOM kills the WHOLE box, not just the offending command
(the VM 202 finding). `recreate` replaces the dead instance with a fresh one
and then runs every best-effort recovery hook in order: the owner's
`on_recreate` rehydrate, the C5 `.disco/MEMORY.md` read-back, and the C3
server-rematerialize replay — none of which is allowed to raise past this
function; a recovery convenience must never wedge the box.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..base import SandboxInstance
    from ..session import SandboxSession

_LOG = logging.getLogger(__name__)


async def recreate(session: SandboxSession, dead: SandboxInstance) -> None:
    """Replace a dead instance with a fresh one. Idempotent under concurrent
    callers (only the first past the lock with the dead instance re-creates). A
    create() that itself fails (infra truly down) propagates as
    SandboxUnavailableError — the session can't paper over a dead host."""
    async with session._lock:
        if session._closed or session._instance is not dead:
            return  # someone else already handled it, or we were closed
        session._instance = None
        try:
            await dead.destroy()
        except Exception:  # noqa: BLE001 — it's already gone; best-effort cleanup
            pass
        session._instance = await session._service.create(
            session._spec, owner_id=session.owner_id, conversation_id=session.conversation_id
        )
        session._generation += 1
        session.sessions.reset_known_sessions()
        # C15: tear down the old ManagedKernel so its inner kernel process
        # doesn't outlive the dead sandbox (the kernel is a child of the
        # container for gateway backends, but a local subprocess for
        # process backends — we always try to shut down cleanly).
        old_kernel, session._kernel = session._kernel, None
        if old_kernel is not None:
            try:
                await old_kernel.shutdown()
            except Exception:  # noqa: BLE001 — best-effort; the box is already gone
                _LOG.debug("kernel shutdown on recreate failed", exc_info=True)
    # the old box took the 'preview' session down with it — bring it back up
    session._spawn_auto_preview()
    # Rehydrate the fresh (empty) workspace from the last snapshot, if the
    # owner wired a hook. Outside the lock — the hook writes files back
    # through this session, which must be able to _ensure() freely. Failures
    # are logged, never raised: the agent can always rebuild by hand, which
    # is exactly the (worse) status quo this hook exists to avoid.
    if session._on_recreate is not None:
        try:
            await session._on_recreate()
        except Exception:  # noqa: BLE001 — best-effort restore
            _LOG.warning(
                "post-recreate rehydrate failed for %s", session.conversation_id, exc_info=True
            )
    # C5 — read-back: pull `.pmx/MEMORY.md` (the write-through mirror of
    # the in-View KnowledgeEvent channel) off the fresh box and stage
    # the facts for the agent loop to re-emit. The in-View channel is
    # authoritative in-session; the file is the durable copy. A hard
    # reset / box wipe erases the in-memory View but the file persists,
    # so this read-back is the recovery path. Best-effort: a missing
    # file (no prior remember) leaves the cache empty, and any I/O
    # failure is logged but never raised.
    try:
        await session._recover_pmx_memory()
    except Exception:  # noqa: BLE001 — recovery is a convenience, never wedge the box
        _LOG.debug("pmx memory read-back failed", exc_info=True)
    # C3: re-materialize the agent's own dev servers (vite / express /
    # uvicorn / http.server on a non-default USER_PORT, …) on the fresh
    # instance. Until this hook, only the static `python3 -m http.server`
    # preview survived a recreate — a real app the agent launched simply
    # vanished on suspend/wake. The shell-sessions manager records each
    # port-binding command in `exec()` and replays them here, best-effort,
    # skipping ports already bound (the no-duplication guarantee).
    try:
        logs = await session.sessions.rehydrate_persistent_servers()
        for line in logs:
            _LOG.info("post-recreate: %s", line)
    except Exception:  # noqa: BLE001 — rehydrate is a convenience; never wedge the box
        _LOG.warning(
            "post-recreate server rehydrate failed for %s",
            session.conversation_id,
            exc_info=True,
        )
