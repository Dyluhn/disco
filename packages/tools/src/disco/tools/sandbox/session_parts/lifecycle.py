"""Session teardown (`SandboxSession.destroy`).

Closes the session at task end: no further use, and the live box torn down.
Owns the full shutdown order — kernel, platform PreviewManager, the legacy
auto-preview task, then the instance itself — so every caller gets the same
teardown regardless of how far the session got before destroy() was called.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session import SandboxSession

_LOG = logging.getLogger(__name__)


async def _shutdown_session_kernel(session: SandboxSession) -> None:
    """Release the managed kernel before sandbox-owned teardown."""

    kernel, session._kernel = session._kernel, None
    if kernel is not None:
        try:
            await kernel.shutdown()
        except Exception:  # noqa: BLE001 — teardown remains best-effort
            _LOG.debug("kernel shutdown on session destroy failed", exc_info=True)


async def destroy(session: SandboxSession) -> None:
    """Close the session at task end: no further use, and the live box torn down.

    Cancels any in-flight auto-preview task before tearing down the instance so
    teardown never races a half-started preview (TOCTOU fix — the task is now
    tracked as session._preview_task and cancelled here).

    P2 #4: also closes a cached platform `PreviewManager` (set on `_preview_manager`
    by the preview_* tools) — its supervisor task sleeps forever otherwise, leaking
    past the sandbox it supervised.
    """
    async with session._destroy_lock:
        session._closed, session._destroy_task = True, asyncio.current_task()
        try:
            # The process backend's managed Jupyter kernel owns a child process
            # and multiple ZMQ channel sockets. Tear it down while its sandbox
            # still exists; simply dropping the reference leaves those resources
            # alive until interpreter shutdown.
            await _shutdown_session_kernel(session)
            # PreviewManager stops foreground servers through this session. The
            # scoped task owner can reach the live instance while unrelated calls
            # continue to observe a closed session.
            mgr = session._preview_manager
            if mgr is not None:
                await mgr.aclose()
                session._preview_manager = None
            task, session._preview_task = session._preview_task, None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Retain the instance owner until backend destruction is confirmed.
            inst = session._instance
            if inst is not None:
                await inst.destroy()
                session._instance = None
        finally:
            session._destroy_task = None
            session._closed = True
