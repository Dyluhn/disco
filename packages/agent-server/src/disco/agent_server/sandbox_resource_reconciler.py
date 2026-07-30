"""Reconcile cached sandbox resources after backend selection changes."""

from __future__ import annotations

import asyncio
import contextlib
from typing import cast

from disco.core.store.sqlite import SqliteEventStore
from disco.tools import SandboxSession

from .lifecycle import _GATE_STATES, LifecycleManager
from .run_registry import LoopRegistry, RunRecoveryLedger, RunRegistry, RunResourceRegistry
from .sandbox_runtime_service import SandboxRuntimeService


class SandboxResourceReconciler:
    """Evict only idle, non-gated sessions whose concrete backend is stale."""

    def __init__(
        self,
        sandbox: SandboxRuntimeService,
        runs: RunRegistry,
        loops: LoopRegistry,
        resources: RunResourceRegistry,
        recovery: RunRecoveryLedger,
        store: SqliteEventStore,
        lifecycle: LifecycleManager,
    ) -> None:
        self._sandbox = sandbox
        self._runs = runs
        self._loops = loops
        self._resources = resources
        self._recovery = recovery
        self._store = store
        self._lifecycle = lifecycle

    def evict_stale(self, conversation_id: str) -> None:
        current = self._current_backend()
        if current is None or self._runs.active_task(conversation_id) is not None:
            return
        if self._recovery.last_status(conversation_id) in _GATE_STATES:
            return
        stale = self._pop_mismatched(conversation_id, current)
        if stale:
            self._lifecycle.clear_session_markers(conversation_id)
        for session in stale:
            with contextlib.suppress(RuntimeError):
                asyncio.create_task(self._destroy(session))

    async def reconcile(self) -> int:
        current = self._current_backend()
        if current is None:
            return 0
        reconciled = 0
        for conversation_id in self._resources.conversation_ids():
            if self._runs.active_task(conversation_id) is not None:
                continue
            try:
                state = await self._store.get_state(conversation_id)
            except Exception:
                state = None
            if state is not None and state.execution_status in _GATE_STATES:
                continue
            stale = self._pop_mismatched(conversation_id, current)
            if stale:
                self._lifecycle.clear_session_markers(conversation_id)
            for session in stale:
                await self._destroy(session)
                reconciled += 1
        return reconciled

    def _current_backend(self) -> str | None:
        if self._sandbox.uses_injected_backend():
            return None
        try:
            return self._sandbox.effective_backend_name()
        except Exception:
            return None

    def _pop_mismatched(
        self,
        conversation_id: str,
        current: str,
    ) -> tuple[SandboxSession, ...]:
        stale: list[SandboxSession] = []
        executor = self._resources.executor(conversation_id)
        session = executor.sandbox if executor is not None else None
        if session is not None and getattr(session, "backend_name", current) != current:
            stale.append(cast(SandboxSession, session))
            self._loops.forget(conversation_id)
            self._resources.pop_executor(conversation_id)
        pending = self._resources.pending_session(conversation_id)
        if pending is not None and pending.backend_name != current:
            self._resources.pop_pending_session(conversation_id)
            stale.append(pending)
        return tuple(stale)

    @staticmethod
    async def _destroy(session: SandboxSession) -> None:
        with contextlib.suppress(Exception):
            await session.destroy()
