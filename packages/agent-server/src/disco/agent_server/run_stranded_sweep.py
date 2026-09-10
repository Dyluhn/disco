"""Reconcile durable RUNNING state that has no live task."""

from __future__ import annotations

import contextlib
import logging

from disco.core import ConversationStatus
from disco.core.store.sqlite import SqliteEventStore

from .run_completion import RunCompletionPort
from .run_registry import LoopRegistry, RunRegistry

logger = logging.getLogger(__name__)


class RunStrandedSweep:
    """Own the periodic durable/live run reconciliation scan."""

    def __init__(
        self,
        runs: RunRegistry,
        loops: LoopRegistry,
        store: SqliteEventStore,
        completion: RunCompletionPort,
    ) -> None:
        self._runs = runs
        self._loops = loops
        self._store = store
        self._completion = completion

    async def sweep_once(self) -> int:
        acted = 0
        for conversation_id in self._loops.conversation_ids():
            if self._runs.active_task(conversation_id) is not None:
                continue
            try:
                state = await self._store.get_state(conversation_id)
            except Exception:  # noqa: BLE001
                continue
            if state.execution_status is not ConversationStatus.RUNNING:
                continue
            logger.warning(
                "stranded RUNNING conversation %s (no live task) — reconciling",
                conversation_id,
            )
            with contextlib.suppress(Exception):
                await self._completion.finalize_clean(conversation_id)
            acted += 1
        return acted
