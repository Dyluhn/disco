"""Commit research boundaries through the run's existing event emitter."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ._agent_state import _AgentState
from ._recovery_state import AgentCheckpoint, LoopCursor, RecoveryCheckpoint
from ._writer_checkpoint import WriterCheckpoint
from .depth import DepthBound
from .recovery import RECOVERY_ACTION, write_checkpoint

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class RunCheckpoints:
    def __init__(
        self,
        conversation_id: str,
        query: str,
        depth: str,
        recency: Literal["month", "week"] | None,
        recovery: RecoveryCheckpoint | None,
    ) -> None:
        self.conversation_id, self.query, self.depth = conversation_id, query, depth
        self.recency: Literal["month", "week"] | None = recency
        self.run_id = recovery.run_id if recovery else uuid.uuid4().hex
        self.latest = recovery
        self.reference: dict[str, Any] | None = None

    async def commit(self, checkpoint: RecoveryCheckpoint, emit: Emit) -> None:
        pending = asyncio.create_task(asyncio.to_thread(write_checkpoint, checkpoint))
        try:
            reference = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Deletion drains the run before removing files. A detached writer
            # thread could otherwise recreate evidence after deletion returned.
            await pending
            raise
        # The event is the commit point; a file alone never means work was committed.
        await emit(RECOVERY_ACTION, reference)
        self.latest, self.reference = checkpoint, reference

    async def record(
        self,
        state: _AgentState,
        cursor: LoopCursor,
        bound: DepthBound,
        emit: Emit,
    ) -> None:
        checkpoint = RecoveryCheckpoint(
            conversation_id=self.conversation_id,
            run_id=self.run_id,
            query=self.query,
            depth_tier=self.depth,
            recency_window=self.recency,
            stage="research",
            bound=bound,
            state=AgentCheckpoint.capture(state),
            cursor=cursor.model_copy(),
        )
        await self.commit(checkpoint, emit)

    async def writing(self, bounded_by: str | None, emit: Emit) -> None:
        if self.latest is not None:
            await self.commit(
                self.latest.model_copy(
                    update={
                        "stage": "writing",
                        "bounded_by": bounded_by,
                        "query": self.query,
                    }
                ),
                emit,
            )

    async def writer(self, state: WriterCheckpoint, emit: Emit) -> None:
        if self.latest is not None:
            await self.commit(self.latest.model_copy(update={"writer": state}), emit)
