"""Shell-session reads + upload write-through — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The session-facing
accessors move out of runtime.py into a `SessionsService` collaborator
constructed once in `ConversationRuntime`: the degrade-on-failure session list
(`sessions_snapshot`), the coalesced capture-pane read (`session_view`), and
the upload write-through session (`upload_session`).

The caches + class constants (`_last_sessions`, `_session_view_cache`,
`_session_view_locks`, `_SESSIONS_LIST_RETRIES`, `_SESSIONS_LIST_BACKOFF_S`,
`_SESSION_VIEW_CACHE_TTL`, `_SESSION_VIEW_MAX_CHARS`) stay on
`ConversationRuntime`; the service reaches them — plus `live_session`,
`_executors`, `_pending_sessions`, and the `_sandbox_service_now` /
`_build_sandbox_spec` / `_surface_of` / `_mcp_egress_hosts` /
`_rehydrate_after_recreate` resolvers — via a back-reference. Every method keeps
a one-line delegator on `ConversationRuntime` (routes call each on the runtime;
`sessions_list` stays on the runtime and calls the `sessions_snapshot`
delegator).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from disco.tools import SandboxSession
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

from .runtime_settings import _BUILD_LIKE_SURFACES

_LOG = logging.getLogger(__name__)
_SESSIONS_LIST_RETRIES = 2
_SESSIONS_LIST_BACKOFF_S = 0.25
_SESSION_VIEW_CACHE_TTL = 0.5
_SESSION_VIEW_MAX_CHARS = 100_000


class SessionsService:
    def __init__(self, rt: Any) -> None:
        self._rt = rt

    def upload_session(self, conversation_id: str) -> SandboxSession:
        """Session uploads write through. The executor's live session when a
        loop exists; otherwise a pending session the NEXT build loop adopts."""
        executor = self._rt._run_resources.executor(conversation_id)
        if executor is not None:
            return executor._sandbox
        if not self._rt._run_resources.has_pending_session(conversation_id):
            self._rt._run_resources.set_pending_session(
                conversation_id,
                SandboxSession(
                    self._rt._sandbox._sandbox_service_now(),
                    self._rt._sandbox._build_sandbox_spec(
                        surface=self._rt._settings._surface_of(conversation_id),
                        mcp_egress_hosts=self._rt._mcp._mcp_egress_hosts(),
                    ),
                    conversation_id=conversation_id,
                    on_recreate=lambda: self._rt._lifecycle._rehydrate_after_recreate(
                        conversation_id
                    ),
                    legacy_auto_preview=(
                        self._rt._settings._surface_of(conversation_id)
                        not in _BUILD_LIKE_SURFACES
                    ),
                ),
            )
        session = self._rt._run_resources.pending_session(conversation_id)
        assert session is not None
        return session

    async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
        """Session list + staleness. Fresh on success (cache updated); on
        transport failure retry twice (0.25 s apart), then degrade to the
        last-known list marked stale=True — a read-only listing must never
        500 the UI poll loop (DEFECT-1). No sandbox -> ([], False)."""
        executor = self._rt._run_resources.executor(conversation_id)
        session = executor.sandbox if executor is not None else None
        if session is None:
            return ([], False)
        last_exc: BaseException | None = None
        for attempt in range(_SESSIONS_LIST_RETRIES + 1):
            if attempt > 0:
                await asyncio.sleep(_SESSIONS_LIST_BACKOFF_S)
            try:
                all_sessions = await session.sessions.list()
                filtered = [s for s in all_sessions if not s.name.startswith("__")]
                self._rt._connections.set_last_sessions(conversation_id, filtered)
                return (filtered, False)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        _LOG.warning(
            "sessions_snapshot: %s failed after %d attempts: %s",
            conversation_id,
            _SESSIONS_LIST_RETRIES + 1,
            last_exc,
        )
        return (self._rt._connections.last_sessions_get(conversation_id), True)

    async def session_view(
        self, conversation_id: str, name: str, tail_chars: int
    ) -> SessionView | None:
        """Coalesced capture-pane: at most one in-flight call per (cid, name),
        result cached 0.5s so concurrent polls share one exec_shell round-trip."""
        executor = self._rt._run_resources.executor(conversation_id)
        session = executor.sandbox if executor is not None else None
        if session is None:
            return None
        lock = self._rt._connections.session_view_lock(conversation_id, name)
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            cached = self._rt._connections.session_view_cache_get(conversation_id, name)
            if cached is not None and (now - cached[0]) < _SESSION_VIEW_CACHE_TTL:
                view = cached[1]
            else:
                view = await session.sessions.view(
                    name, tail_chars=_SESSION_VIEW_MAX_CHARS
                )
                self._rt._connections.session_view_cache_set(
                    conversation_id,
                    name,
                    now,
                    view,
                )
        if len(view.output) > tail_chars:
            return SessionView(running=view.running, output=view.output[-tail_chars:])
        return view
