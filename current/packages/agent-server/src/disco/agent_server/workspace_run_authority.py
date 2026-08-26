"""Run admission and lifecycle delegates for the workspace coordinator."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from disco.core import (
    ConversationStatus,
    Event,
    StatusEvent,
    WorkspaceMutationEvent,
    latest_workspace_run_intent,
)
from disco.core.loop import SealabilityProbeResult

from .runtime_settings import _BUILD_LIKE_SURFACES
from .workspace_commit import WorkspaceRunSuperseded
from .workspace_mutations import _WorkspaceMutations

if TYPE_CHECKING:
    from .build_platform_runtime import BuildPlatformRuntime
    from .lifecycle import LifecycleManager
    from .lifecycle_command_service import LifecycleCommandService
    from .run_controller import RunController
    from .run_supervisor import RunPersistenceSupervisor
    from .runtime_settings import RuntimeSettings
    from .workspace_fence import WorkspaceFenceService
    from .workspace_ownership import WorkspaceOwnership


class _WorkspaceRunAuthority(_WorkspaceMutations):
    """Run-facing methods sharing the coordinator's mutation authority."""

    _settings: RuntimeSettings
    _build_platform: BuildPlatformRuntime
    _lifecycle_commands: LifecycleCommandService
    _run_controller: RunController | None
    _run_execution: RunPersistenceSupervisor | None

    async def run_after_admission(
        self,
        conversation_id: str,
        loop: Any,
        *,
        expected_run_intent_id: str | None = None,
    ) -> Any:
        """Admit a Build run through the same fence as host mutations."""

        build_run = self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES
        assert self._run_execution is not None, "run collaborators not yet bound"
        try:
            if build_run:
                async with self.lock(conversation_id):
                    async with self.interprocess_mutation_fence(conversation_id):
                        if expected_run_intent_id is not None:
                            events = await self._store.get_events(conversation_id)
                            latest_intent = latest_workspace_run_intent(events)
                            if latest_intent is None or latest_intent.id != expected_run_intent_id:
                                raise WorkspaceRunSuperseded(
                                    "registered run intent changed before admission"
                                )
                        await self._build_platform.record_route_locked(conversation_id)
                        await self.record_mutation_locked(
                            conversation_id,
                            "agent.run-claimed",
                        )
                        self._fences.mark_run_admitted(conversation_id)
            return await self._run_execution.run(conversation_id, loop)
        finally:
            if build_run:
                self.clear_run_claim(conversation_id)

    def claim_registered_run_locked(self, conversation_id: str) -> None:
        """Publish an ingress-first run claim before releasing its workspace fence."""

        task = self._ownership.active_task(conversation_id)
        self._fences.mark_registered_run_locked(
            conversation_id,
            active=task is not None,
        )

    def clear_run_claim(self, conversation_id: str) -> None:
        self._fences.clear_run_claim(conversation_id)

    def has_admitted_run(self, conversation_id: str) -> bool:
        return self._fences.has_admitted_run(conversation_id)

    def has_run_claim(self, conversation_id: str) -> bool:
        return self._fences.has_run_claim(conversation_id)

    async def record_run_intent_locked(
        self,
        conversation_id: str,
        source: str,
    ) -> WorkspaceMutationEvent | None:
        """Durably invalidate an old workspace seal before a Build execution ingress."""

        if self._settings._surface_of(conversation_id) not in _BUILD_LIKE_SURFACES:
            return None
        self._require_process_fence_locked(conversation_id)
        return await self.record_mutation_locked(
            conversation_id,
            f"agent.run-intent.{source}",
        )

    async def append_run_ingress_locked(
        self,
        conversation_id: str,
        events: list[Event],
        source: str,
        *,
        intent: WorkspaceMutationEvent | None = None,
    ) -> list[Event]:
        """Atomically append one user ingress and its durable execution intent."""

        if not self.lock(conversation_id).locked():
            raise RuntimeError("run ingress requires the conversation lock")
        history = await self._store.get_events(conversation_id)
        closures = self._dangling_action_closures(history)
        pending = [*closures, *events]
        if self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES:
            self._require_process_fence_locked(conversation_id)
            expected_operation = f"agent.run-intent.{source}"
            if intent is not None and (
                intent.operation != expected_operation
                or intent.run_protocol_version != 1
                or intent.seq is not None
            ):
                raise ValueError("preconstructed run intent does not match ingress")
            pending.append(
                intent
                or WorkspaceMutationEvent(
                    operation=expected_operation,
                    run_protocol_version=1,
                )
            )
        elif intent is not None:
            raise ValueError("preconstructed run intent requires a Build-like surface")
        return await self._append_ingress_events_locked(conversation_id, pending)

    async def _append_ingress_events_locked(
        self,
        conversation_id: str,
        pending: list[Event],
    ) -> list[Event]:
        if not any(isinstance(event, StatusEvent) for event in pending):
            return await self._store.append_many(conversation_id, pending)
        ingress_intent = next(
            (
                event
                for event in pending
                if isinstance(event, WorkspaceMutationEvent)
                and event.run_protocol_version == 1
                and event.operation.startswith("agent.run-intent.")
            ),
            None,
        )
        return await self._lifecycle_commands.append_transition_batch_locked(
            conversation_id,
            pending,
            ingress_intent=ingress_intent,
        )

    async def rekick_stranded_followup(
        self,
        conversation_id: str,
        claimed_user_seq: int,
    ) -> None:
        """Publish a post-terminal re-kick and local claim as one fenced ingress."""

        async with self.lock(conversation_id):
            async with self.interprocess_mutation_fence(conversation_id):
                await self.record_run_intent_locked(conversation_id, "stranded-followup")
                assert self._run_controller is not None, "run collaborators not yet bound"
                self._run_controller.kick(
                    conversation_id,
                    claimed_user_seq=claimed_user_seq,
                )
                self.claim_registered_run_locked(conversation_id)

    async def forget(self, conversation_id: str) -> None:
        await self._ownership.cancel_registered_task(conversation_id)
        async with self.lock(conversation_id):
            await self.forget_locked(conversation_id)

    async def forget_locked(self, conversation_id: str) -> None:
        self.clear_run_claim(conversation_id)
        await self._ownership.dispose(conversation_id)


class _WorkspaceLifecycleAuthority:
    """Lifecycle delegates sharing the coordinator's existing authority."""

    _lifecycle_commands: LifecycleCommandService
    _lifecycle: LifecycleManager
    _ownership: WorkspaceOwnership
    _fences: WorkspaceFenceService

    @asynccontextmanager
    async def _lifecycle_fence(self, conversation_id: str) -> AsyncIterator[None]:
        async with self._fences._lifecycle_fence(conversation_id):
            yield

    @asynccontextmanager
    async def _lifecycle_locked_fence(self, conversation_id: str) -> AsyncIterator[None]:
        async with self._fences._lifecycle_locked_fence(conversation_id):
            yield

    def _require_lifecycle_fence(self, conversation_id: str) -> None:
        self._fences._require_lifecycle_fence(conversation_id)

    async def resolve_current_run_authority(
        self,
        conversation_id: str,
    ) -> tuple[str | None, str | None]:
        agent_view_id, run_intent_id, _strict = (
            await self._lifecycle_commands.resolve_current_authority(conversation_id)
        )
        return agent_view_id, run_intent_id

    async def append_status(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        return await self._lifecycle_commands.append_status(conversation_id, event)

    async def append_status_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        return await self._lifecycle_commands.append_status_locked(
            conversation_id,
            event,
        )

    async def run_authority_is_current(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        return await self._lifecycle_commands.authority_is_current(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def _run_authority_is_current_locked(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        return await self._lifecycle_commands._authority_is_current_locked(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def append_run_status_if_current(
        self,
        conversation_id: str,
        event: StatusEvent,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> StatusEvent | None:
        return await self._lifecycle_commands.append_task_status_if_current(
            conversation_id,
            event,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def append_current_run_transition(
        self,
        conversation_id: str,
        events: list[Event],
        status: StatusEvent,
        *,
        expected_statuses: frozenset[ConversationStatus],
    ) -> list[Event] | None:
        return await self._lifecycle_commands.append_current_run_transition(
            conversation_id,
            events,
            status,
            expected_statuses=expected_statuses,
        )

    async def append_transition_batch_locked(
        self,
        conversation_id: str,
        events: list[Event],
        *,
        bind_current: bool = False,
    ) -> list[Event]:
        """Append domain facts with one lifecycle-authorized status event."""

        return await self._lifecycle_commands.append_transition_batch_locked(
            conversation_id,
            events,
            bind_current=bind_current,
        )

    def finish_sealability_probe(
        self,
        conversation_id: str,
    ) -> Callable[[], Awaitable[SealabilityProbeResult]]:
        async def probe() -> SealabilityProbeResult:
            return await self._lifecycle.probe_finish_sealability(conversation_id)

        return probe

    def terminal_commit_hook(
        self,
        conversation_id: str,
    ) -> Callable[[StatusEvent], Awaitable[StatusEvent]]:
        return self._lifecycle_commands.terminal_commit_hook(
            conversation_id,
            authority_provider=lambda: self._ownership.current_lifecycle_task_authority(
                conversation_id
            ),
        )
