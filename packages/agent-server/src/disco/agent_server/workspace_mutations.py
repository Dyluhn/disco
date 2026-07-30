"""Mutation and fence mechanics for the workspace coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    ObservationEvent,
    WorkspaceMutationEvent,
)

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .run_controller import RunController
    from .workspace_fence import WorkspaceFenceService
    from .workspace_ownership import WorkspaceOwnership


class _WorkspaceMutations:
    """Use the coordinator's one fence registry and event store."""

    _store: SqliteEventStore
    _fences: WorkspaceFenceService
    _ownership: WorkspaceOwnership
    _run_controller: RunController | None

    if TYPE_CHECKING:

        def claim_registered_run_locked(self, conversation_id: str) -> None: ...

    def lock(self, conversation_id: str) -> asyncio.Lock:
        return self._fences.lock(conversation_id)

    @asynccontextmanager
    async def interprocess_mutation_fence(
        self,
        conversation_id: str,
        *,
        wait: bool = True,
    ) -> AsyncIterator[None]:
        """Extend the caller-owned local fence across Agent server processes."""

        async with self._fences.interprocess_mutation_fence(
            conversation_id,
            wait=wait,
        ):
            yield

    def _fence_owned_by_current_task(self, conversation_id: str) -> bool:
        return self._fences.fence_owned_by_current_task(conversation_id)

    def _require_process_fence_locked(self, conversation_id: str) -> None:
        self._fences.require_process_fence_locked(conversation_id)

    @asynccontextmanager
    async def mutation(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths: tuple[str, ...] = (),
    ) -> AsyncIterator[None]:
        """Fence a host edit before yielding the serialized workspace."""

        async with self.fence(conversation_id):
            await self.record_mutation_locked(conversation_id, operation, paths=paths)
            yield

    @asynccontextmanager
    async def fence(self, conversation_id: str) -> AsyncIterator[None]:
        """Serialize a caller that must derive its mutation paths while locked."""

        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                yield

    async def record_mutation_locked(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths: tuple[str, ...] = (),
    ) -> WorkspaceMutationEvent:
        if not self.lock(conversation_id).locked():
            raise RuntimeError("workspace mutation fence requires the conversation lock")
        mutation = WorkspaceMutationEvent(
            operation=operation,
            paths=tuple(sorted(set(paths))),
            run_protocol_version=(1 if operation.startswith("agent.run-intent.") else None),
        )
        state = await self._store.get_state(conversation_id)
        interrupts_running_view = not operation.startswith("agent.") and (
            state.execution_status
            in {
                ConversationStatus.RUNNING,
                ConversationStatus.WAITING_FOR_CONFIRMATION,
                ConversationStatus.AWAITING_PLAN_APPROVAL,
                ConversationStatus.AWAITING_USER_DECISION,
                ConversationStatus.AWAITING_USER_QUESTION,
            }
        )
        if interrupts_running_view:
            history = await self._store.get_events(conversation_id)
            pending: list[Event] = [*self._dangling_action_closures(history), mutation]
            pending.append(
                WorkspaceMutationEvent(
                    operation="agent.run-intent.host-mutation",
                    run_protocol_version=1,
                )
            )
            stored_events = await self._store.append_many(conversation_id, pending)
            stored = next(event for event in stored_events if event.id == mutation.id)
            assert self._run_controller is not None, "run collaborators not yet bound"
            self._run_controller.kick(conversation_id)
            self.claim_registered_run_locked(conversation_id)
        else:
            stored = await self._store.append(conversation_id, mutation)
        if not isinstance(stored, WorkspaceMutationEvent):
            raise RuntimeError("event store returned wrong workspace mutation event type")
        return stored

    @staticmethod
    def _dangling_action_closures(history: list[Event]) -> list[AgentErrorEvent]:
        resolved = {
            event.action_id
            for event in history
            if isinstance(event, (ObservationEvent, AgentErrorEvent))
            and event.action_id is not None
        }
        return [
            AgentErrorEvent(
                error="execution_superseded",
                detail=(
                    "A newer user or host instruction arrived before this proposed action "
                    "crossed the workspace effect boundary. The action was not executed."
                ),
                action_id=action.id,
                tool_call_id=action.tool_call.call_id,
                agent_view_id=action.agent_view_id,
            )
            for action in history
            if isinstance(action, ActionEvent) and action.id not in resolved
        ]
