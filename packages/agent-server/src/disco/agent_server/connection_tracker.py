"""ConnectionTracker — owner of WebSocket-connection tracking, idle-suspend
scheduling, and the per-conversation session-view / wake-lock / last-sessions
caches.

God-file decomposition (pure move, zero behavior change). The connection and
session-view state moves out of ``runtime.py`` into a ``ConnectionTracker``
collaborator constructed once in ``ConversationRuntime``:

  - auto-suspend (lifecycle G): ``on_connect`` / ``on_disconnect`` /
    ``_suspend_after_grace`` — track open WS connections per conversation and
    schedule an idle-suspend when the last one closes.
  - session-view coalescing cache: ``session_view_lock`` / ``session_view_cache_get``
    / ``session_view_cache_set`` — at most one in-flight capture-pane call per
    (cid, name), result cached 0.5 s.
  - wake-lock: ``wake_lock_for`` — serializes suspended-sandbox wake.
  - last-sessions degrade: ``last_sessions_get`` / ``set_last_sessions`` —
    stale-on-failure session-list degradation (DC-04b).
  - per-conversation cleanup: ``clear_conversation`` — drop all caches/locks for
    a conversation (teardown, backend-eviction, forget).

The state (``_connections``, ``_suspend_tasks``, ``_session_view_cache``,
``_session_view_locks``, ``_wake_locks``, ``_preview_capture_locks``,
``_last_sessions``) is owned here.
The suspend action is reached through a **narrow named typed collaborator** —
never through ``ConversationRuntime``, ``Any``, a generic context object, or
an exposed cross-domain dictionary.

Every moved method keeps a one-line delegator on ``ConversationRuntime`` or
``LifecycleManager`` because routes, ``SessionsService``, ``PreviewService``,
and tests reach them directly.  The primary session performs the composition
wiring after inspecting this diff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from disco.core.auth import intent_ttl_s
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

if TYPE_CHECKING:
    from .lifecycle import LifecycleManager

_LOG = logging.getLogger(__name__)


@runtime_checkable
class SuspendCallback(Protocol):
    """Suspend an idle build's sandbox after the grace period elapses."""

    async def suspend(self, conversation_id: str) -> None: ...


class LifecycleSuspender:
    """Narrow adapter for the lifecycle owner's suspend operation."""

    def __init__(self, lifecycle: LifecycleManager) -> None:
        self._lifecycle = lifecycle

    async def suspend(self, conversation_id: str) -> None:
        await self._lifecycle._suspend(conversation_id)


class ConnectionState:
    """Own connection counts and session-facing caches without lifecycle effects."""

    def __init__(self) -> None:
        self._connections: dict[str, int] = {}
        self._session_view_cache: dict[tuple[str, str], tuple[float, SessionView]] = {}
        self._session_view_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._wake_locks: dict[str, asyncio.Lock] = {}
        self._preview_capture_locks: dict[str, asyncio.Lock] = {}
        self._preview_capture_deadlines: dict[str, float] = {}
        self._preview_capture_owners: dict[str, dict[int, float]] = {}
        self._next_preview_capture_generation = 0
        self._last_sessions: dict[str, list[SessionInfo]] = {}

    def has_connections(self, conversation_id: str) -> bool:
        return self._connections.get(conversation_id, 0) > 0

    def preview_capture_lock_for(self, conversation_id: str) -> asyncio.Lock:
        """Get the stable lifecycle/preview ownership lock for a conversation."""
        return self._preview_capture_locks.setdefault(
            conversation_id,
            asyncio.Lock(),
        )

    def clear_session_state(self, conversation_id: str) -> None:
        for key in [k for k in self._session_view_cache if k[0] == conversation_id]:
            del self._session_view_cache[key]
        for key in [k for k in self._session_view_locks if k[0] == conversation_id]:
            del self._session_view_locks[key]
        self._wake_locks.pop(conversation_id, None)
        self._preview_capture_deadlines.pop(conversation_id, None)
        self._preview_capture_owners.pop(conversation_id, None)
        self._last_sessions.pop(conversation_id, None)

    def begin_preview_capture(self, conversation_id: str) -> int:
        """Retain ownership across metadata → capability → redemption."""
        self._next_preview_capture_generation += 1
        generation = self._next_preview_capture_generation
        deadline = time.monotonic() + intent_ttl_s()
        self._preview_capture_owners.setdefault(conversation_id, {})[generation] = deadline
        self._preview_capture_deadlines[conversation_id] = max(
            deadline,
            self._preview_capture_deadlines.get(conversation_id, 0.0),
        )
        return generation

    def complete_preview_capture(
        self,
        conversation_id: str,
        generation: int | None = None,
    ) -> None:
        owners = self._preview_capture_owners.get(conversation_id)
        if owners:
            if generation is None:
                generation = max(owners)
            owners.pop(generation, None)
            if owners:
                self._preview_capture_deadlines[conversation_id] = max(owners.values())
            else:
                self._preview_capture_owners.pop(conversation_id, None)
                self._preview_capture_deadlines.pop(conversation_id, None)
            return
        self._preview_capture_deadlines.pop(conversation_id, None)

    def preview_capture_active(self, conversation_id: str) -> bool:
        remaining = self.preview_capture_remaining(conversation_id)
        if remaining <= 0:
            return False
        return True

    def preview_capture_remaining(self, conversation_id: str) -> float:
        now = time.monotonic()
        owners = self._preview_capture_owners.get(conversation_id)
        if owners:
            for generation, deadline in list(owners.items()):
                if deadline <= now:
                    owners.pop(generation, None)
            if owners:
                remaining = max(owners.values()) - now
                self._preview_capture_deadlines[conversation_id] = max(owners.values())
                return max(remaining, 0.0)
            self._preview_capture_owners.pop(conversation_id, None)

        deadline = self._preview_capture_deadlines.get(conversation_id)
        if deadline is None:
            return 0.0
        remaining = deadline - now
        if remaining <= 0:
            self._preview_capture_deadlines.pop(conversation_id, None)
            return 0.0
        return remaining


class ConnectionTracker:
    """Owner of connection tracking, suspend scheduling, and session caches.

    The state (``_connections``, ``_suspend_tasks``, ``_session_view_cache``,
    ``_session_view_locks``, ``_wake_locks``, ``_last_sessions``) is declared
    and owned here.  The suspend action is reached through the narrow typed
    collaborator ``SuspendCallback`` — never through the runtime or a generic
    context object.
    """

    def __init__(
        self,
        suspend: SuspendCallback,
        *,
        state: ConnectionState | None = None,
    ) -> None:
        self._suspend = suspend
        self._state = state or ConnectionState()
        # Auto-suspend (lifecycle G): a build session is live only while a UI is
        # watching it. Track open WS connections per conversation; when the last
        # one closes, free the idle sandbox after a grace period.
        self._suspend_tasks: dict[str, asyncio.Task] = {}

    # ---- auto-suspend (lifecycle G) ----------------------------------------

    def on_connect(self, conversation_id: str) -> None:
        """A UI WebSocket connected — track it and cancel any pending idle-suspend
        (the user is back before the grace elapsed, or the WS reconnected)."""
        connections = self._state._connections
        connections[conversation_id] = connections.get(conversation_id, 0) + 1
        task = self._suspend_tasks.pop(conversation_id, None)
        if task is not None:
            task.cancel()

    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
        """A UI WebSocket closed. When the LAST connection for a conversation goes,
        schedule an idle-suspend after `grace_s` — long enough that a brief blip (the
        WS-reconnect backoff) reconnects and cancels it before it fires."""
        connections = self._state._connections
        n = connections.get(conversation_id, 0) - 1
        if n > 0:
            connections[conversation_id] = n
            return
        connections.pop(conversation_id, None)
        old = self._suspend_tasks.pop(conversation_id, None)
        if old is not None:
            old.cancel()
        self._suspend_tasks[conversation_id] = asyncio.create_task(
            self._suspend_after_grace(conversation_id, grace_s)
        )

    async def _suspend_after_grace(self, conversation_id: str, grace_s: float) -> None:
        try:
            await asyncio.sleep(grace_s)
            if self._state.has_connections(conversation_id):
                return
            await self._suspend.suspend(conversation_id)
            # A FINISHED metadata request can retain ownership across the
            # capability/redemption gap.  Re-admit suspension exactly at that
            # bounded deadline; reconnect cancellation still wins.
            remaining = self._state.preview_capture_remaining(conversation_id)
            while remaining > 0:
                await asyncio.sleep(remaining)
                if self._state.has_connections(conversation_id):
                    return
                await self._suspend.suspend(conversation_id)
                remaining = self._state.preview_capture_remaining(conversation_id)
        except asyncio.CancelledError:
            return
        finally:
            if self._suspend_tasks.get(conversation_id) is asyncio.current_task():
                self._suspend_tasks.pop(conversation_id, None)

    def has_connections(self, conversation_id: str) -> bool:
        """True when at least one WS connection is open for this conversation."""
        return self._state.has_connections(conversation_id)

    def cancel_suspension(self, conversation_id: str) -> None:
        """Cancel a pending idle-suspend without touching the connection count.

        Used by the idle-sweep and gate-reaper paths which need to short-circuit
        a scheduled suspension when a conversation turns out to still be active.
        """
        task = self._suspend_tasks.pop(conversation_id, None)
        if task is not None:
            task.cancel()

    # ---- session-view coalescing cache (BP-14) ----------------------------

    def session_view_lock(self, conversation_id: str, name: str) -> asyncio.Lock:
        """Get or create the coalescing lock for a (cid, name) capture-pane call."""
        return self._state._session_view_locks.setdefault(
            (conversation_id, name),
            asyncio.Lock(),
        )

    def session_view_cache_get(
        self, conversation_id: str, name: str
    ) -> tuple[float, SessionView] | None:
        """Read the cached capture-pane view for (cid, name), or ``None``."""
        return self._state._session_view_cache.get((conversation_id, name))

    def session_view_cache_set(
        self,
        conversation_id: str,
        name: str,
        timestamp: float,
        view: SessionView,
    ) -> None:
        """Write/overwrite the cached capture-pane view for (cid, name)."""
        self._state._session_view_cache[(conversation_id, name)] = (timestamp, view)

    # ---- wake-lock (suspended-sandbox wake serialization) -----------------

    def wake_lock_for(self, conversation_id: str) -> asyncio.Lock:
        """Get or create the wake lock for a conversation."""
        return self._state._wake_locks.setdefault(conversation_id, asyncio.Lock())

    def preview_capture_lock_for(self, conversation_id: str) -> asyncio.Lock:
        """Serialize finished-preview capture with auto-suspend teardown."""
        return self._state.preview_capture_lock_for(conversation_id)

    def begin_preview_capture(self, conversation_id: str) -> int:
        return self._state.begin_preview_capture(conversation_id)

    def complete_preview_capture(
        self,
        conversation_id: str,
        generation: int | None = None,
    ) -> None:
        self._state.complete_preview_capture(conversation_id, generation)

    def preview_capture_active(self, conversation_id: str) -> bool:
        return self._state.preview_capture_active(conversation_id)

    def preview_capture_remaining(self, conversation_id: str) -> float:
        return self._state.preview_capture_remaining(conversation_id)

    # ---- last-sessions degrade (DC-04b) ------------------------------------

    def last_sessions_get(self, conversation_id: str) -> list[SessionInfo]:
        """Read the last-known session list, or ``[]`` when none recorded."""
        return self._state._last_sessions.get(conversation_id, [])

    def set_last_sessions(self, conversation_id: str, sessions: list[SessionInfo]) -> None:
        """Record the last-known session list for stale-on-failure degradation."""
        self._state._last_sessions[conversation_id] = sessions

    # ---- per-conversation cleanup -----------------------------------------

    def clear_session_state(self, conversation_id: str) -> None:
        """Drop session caches without changing live connection ownership."""
        self._state.clear_session_state(conversation_id)

    def clear_conversation(self, conversation_id: str) -> None:
        """Forget all session and connection state for a deleted conversation."""
        self.clear_session_state(conversation_id)
        self._state._preview_capture_locks.pop(conversation_id, None)
        self._state._connections.pop(conversation_id, None)
        task = self._suspend_tasks.pop(conversation_id, None)
        if task is not None:
            task.cancel()
