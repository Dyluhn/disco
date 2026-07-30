"""Shell-session reads + upload write-through — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The session-facing
accessors move out of runtime.py into a `SessionsService` collaborator
constructed once in `ConversationRuntime`: the degrade-on-failure session list
(`sessions_snapshot`), the coalesced capture-pane read (`session_view`), and
the upload write-through session (`upload_session`).

The service receives its actual collaborators directly — no runtime back-ref,
no ``rt: Any``, no multi-domain locator. Every method keeps a one-line
delegator on `ConversationRuntime` (routes call each on the runtime;
`sessions_list` stays on the runtime and calls the `sessions_snapshot`
delegator).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from disco.tools import SandboxSession
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView

from .runtime_settings import _BUILD_LIKE_SURFACES

if TYPE_CHECKING:
    from .connection_tracker import ConnectionTracker
    from .lifecycle import LifecycleManager
    from .mcp_manager import McpManager
    from .run_registry import RunResourceRegistry
    from .runtime_settings import RuntimeSettings
    from .sandbox_runtime_service import SandboxRuntimeService

_LOG = logging.getLogger(__name__)
_SESSIONS_LIST_RETRIES = 2
_SESSIONS_LIST_BACKOFF_S = 0.25
_SESSION_VIEW_CACHE_TTL = 0.5
_SESSION_VIEW_MAX_CHARS = 100_000


class SessionsService:
    def __init__(
        self,
        run_resources: RunResourceRegistry,
        sandbox: SandboxRuntimeService,
        settings: RuntimeSettings,
        mcp: McpManager,
        lifecycle: LifecycleManager,
        connections: ConnectionTracker,
    ) -> None:
        self._run_resources = run_resources
        self._sandbox = sandbox
        self._settings = settings
        self._mcp = mcp
        self._lifecycle = lifecycle
        self._connections = connections

    def upload_session(self, conversation_id: str) -> SandboxSession:
        """Session uploads write through. The executor's live session when a
        loop exists; otherwise a pending session the NEXT build loop adopts."""
        executor = self._run_resources.executor(conversation_id)
        if executor is not None:
            return cast(SandboxSession, executor._sandbox)
        if not self._run_resources.has_pending_session(conversation_id):
            self._run_resources.set_pending_session(
                conversation_id,
                SandboxSession(
                    self._sandbox._sandbox_service_now(),
                    self._sandbox._build_sandbox_spec(
                        surface=self._settings._surface_of(conversation_id),
                        mcp_egress_hosts=self._mcp._mcp_egress_hosts(),
                    ),
                    conversation_id=conversation_id,
                    on_recreate=lambda: self._lifecycle._rehydrate_after_recreate(
                        conversation_id
                    ),
                    legacy_auto_preview=(
                        self._settings._surface_of(conversation_id)
                        not in _BUILD_LIKE_SURFACES
                    ),
                ),
            )
        session = self._run_resources.pending_session(conversation_id)
        assert session is not None
        return session

    async def sessions_snapshot(self, conversation_id: str) -> tuple[list[SessionInfo], bool]:
        """Session list + staleness. Fresh on success (cache updated); on
        transport failure retry twice (0.25 s apart), then degrade to the
        last-known list marked stale=True — a read-only listing must never
        500 the UI poll loop (DEFECT-1). No sandbox -> ([], False)."""
        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return ([], False)
        last_exc: BaseException | None = None
        for attempt in range(_SESSIONS_LIST_RETRIES + 1):
            if attempt > 0:
                await asyncio.sleep(_SESSIONS_LIST_BACKOFF_S)
            try:
                all_sessions = await session.sessions.list()
                filtered = [s for s in all_sessions if not s.name.startswith("__")]
                self._connections.set_last_sessions(conversation_id, filtered)
                return (filtered, False)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        _LOG.warning(
            "sessions_snapshot: %s failed after %d attempts: %s",
            conversation_id,
            _SESSIONS_LIST_RETRIES + 1,
            last_exc,
        )
        return (self._connections.last_sessions_get(conversation_id), True)

    async def session_view(
        self, conversation_id: str, name: str, tail_chars: int
    ) -> SessionView | None:
        """Coalesced capture-pane: at most one in-flight call per (cid, name),
        result cached 0.5s so concurrent polls share one exec_shell round-trip."""
        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return None
        lock = self._connections.session_view_lock(conversation_id, name)
        async with lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            cached = self._connections.session_view_cache_get(conversation_id, name)
            if cached is not None and (now - cached[0]) < _SESSION_VIEW_CACHE_TTL:
                view = cached[1]
            else:
                view = await session.sessions.view(
                    name, tail_chars=_SESSION_VIEW_MAX_CHARS
                )
                self._connections.session_view_cache_set(
                    conversation_id,
                    name,
                    now,
                    view,
                )
        if len(view.output) > tail_chars:
            return SessionView(running=view.running, output=view.output[-tail_chars:])
        return view
