"""BP-02 static auto-preview + W6 subdirectory detection.

`ensure_preview` starts (idempotently) the workspace's `index.html` server as
the tracked 'preview' service; `detect_serve_dir` finds the deepest directory
that actually contains that `index.html` so a subdir app (e.g. `macos-clone/`)
is served correctly instead of the bare workspace root. `spawn_auto_preview`
and `disable_auto_preview` own the fire-and-forget lifecycle around it.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..base import SandboxInstance
    from ..session import SandboxSession

_LOG = logging.getLogger(__name__)


def spawn_auto_preview(session: SandboxSession) -> None:
    """Fire-and-forget the static auto-serve (BP-02): every fresh box comes up with
    the workspace served as session 'preview'. Idempotent and polite — if anything
    already owns the port (e.g. the process backend's host, where :8000 is the
    agent-server itself), ensure_preview() backs off with False. Failures are
    logged, never raised: preview is a convenience, not a dependency of the box."""

    if session._auto_preview_disabled:
        # The platform PreviewManager owns previews for this session — don't spawn
        # the legacy static auto-preview (it would race/collide on a curated port).
        return

    async def _auto() -> None:
        try:
            await session.ensure_preview()
        except Exception:  # noqa: BLE001 — best-effort; the box must not care
            _LOG.debug("auto preview start failed", exc_info=True)

    session._preview_task = asyncio.create_task(_auto())


async def disable_auto_preview(session: SandboxSession) -> None:
    """EPIC F (P1 #2) — stand the legacy static auto-preview DOWN so the platform
    PreviewManager is the SINGLE authority for previews on this session. Cancels a
    still-pending auto-preview task, tears down an already-running static 'preview'
    server + its tracked entry, and latches a flag so a later `spawn_auto_preview`
    (e.g. after a sandbox recreate) does not bring it back. Idempotent and best-
    effort: preview is a convenience, so no failure here is allowed to raise."""
    session._auto_preview_disabled = True
    task, session._preview_task = session._preview_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — swallow cleanly
            pass
    try:
        await session.sessions.kill_foreground("preview")
    except Exception:  # noqa: BLE001 — nothing running / already gone is fine
        _LOG.debug("auto-preview kill on disable failed", exc_info=True)
    # Drop the static auto-preview's tracked entry (it may be registered under the
    # remapped process-safe port, so match by the well-known 'preview' name).
    for p, svc in list(session._tracked_services.items()):
        if svc.name == "preview":
            session._tracked_services.pop(p, None)


async def detect_serve_dir(inst: SandboxInstance, workspace: str) -> str:
    """W6 — detect the subdirectory containing index.html and serve THAT dir.

    Supports subdir apps (e.g. `macos-clone/index.html`): the preview should
    serve the subdir, not the workspace root (which shows raw files instead of
    the app). Falls back to workspace root when no index.html is found or the
    shell command fails.

    Skips `.pmx/` and `node_modules/` — those directories are internal and
    should never be the serve root."""
    try:
        res = await inst.exec_shell(
            # Find first index.html, skipping internal dirs, sort shallowest first
            f"find {shlex.quote(workspace)} -name 'index.html'"
            f" -not -path '*/.pmx/*' -not -path '*/node_modules/*'"
            f" | sort | head -1",
            timeout_s=5,
        )
        if res.exit_code == 0:
            found = res.stdout.strip()
            if found:
                import os as _os

                subdir = _os.path.dirname(found)
                if subdir and subdir != workspace:
                    return subdir
    except Exception:  # noqa: BLE001 — preview is a convenience, never wedge
        pass
    return workspace


async def ensure_preview(session: SandboxSession, port: int) -> bool:
    """Start (idempotently) the static preview as visible session 'preview'.
    Returns False without side effects if :port is already bound (someone — maybe
    the agent's own dev server — owns it; that is fine and not ours to fight).

    W6 — serves the app's index.html SUBDIRECTORY when one is detected (e.g.
    `macos-clone/`) rather than always falling back to the workspace root.
    A root-level index.html gets the original behavior.

    BP-G9 — the static preview is now ONE tracked service among potentially
    many. The entry is registered in `session._tracked_services[port]` so the
    C3 rematerialize hook re-issues it on a fresh box and a UI/runtime can
    ask "what's exposed on this conversation right now?" (see
    `tracked_services`). For multi-service builds use `ensure_service`
    directly (BP-G9 acceptance: API on 3000 + frontend on 5173)."""
    if session._auto_preview_disabled:
        # The platform PreviewManager owns previews here — back off (P1 #2).
        return False

    from disco.core.loop.preview_target import (
        process_safe_preview_port,
        reserved_control_ports,
    )

    from ..port_owner import port_owner
    from ..session import TrackedService

    # Process-backend containment (Bug 7): on a SHARED-host backend (process/
    # local) the default preview port (8000) is the agent-server's own control
    # port — serving the static preview there collides with + crashes the
    # agent-server. Remap a reserved control port to a process-safe preview port
    # (8080, never 8000). Isolated container backends keep 8000 (it's the box's).
    if session._service.name in ("process", "local") and port in reserved_control_ports():
        port = process_safe_preview_port()

    inst = await session._ensure()
    owner = await port_owner(inst, port)
    if owner is not None and owner.pid is not None:
        return False

    res = await inst.exec_shell("pwd", timeout_s=5)
    workspace = res.stdout.strip()
    # W6: serve the deepest index.html directory, not always the workspace root.
    serve_dir = await session._detect_serve_dir(inst, workspace)
    # S-W5 D5: argv-serialize every component. ``serve_dir`` originates in
    # the model-authored workspace and may contain shell metacharacters;
    # interpolating it into a command made preview restart an execution sink.
    preview_argv = ["python3", "-m", "http.server", str(port), "-d", serve_dir]
    cmd = shlex.join(preview_argv)

    await session.sessions.exec("preview", cmd, exec_dir=serve_dir)
    # BP-G9: register the static preview as a tracked service so the wake
    # machinery has a single source of truth for "what to rematerialize on
    # a fresh box" — works alongside the C3 `_persistent_servers` dict that
    # `shell_exec` populates implicitly.
    session._tracked_services[port] = TrackedService(
        name="preview",
        port=port,
        command=cmd,
        exec_dir=serve_dir,
    )
    return True
