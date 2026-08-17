"""Single authorization boundary for Agent Server lifecycle transitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    EventStore,
    StatusEvent,
    WorkspaceMutationEvent,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)

LifecycleAuthority = tuple[str | None, str | None, bool]


class LifecycleTransitionRejected(RuntimeError):
    """The requested transition does not own the durable run head."""


def _authority_already_killed(
    events: Iterable[Event],
    authority: LifecycleAuthority,
) -> bool:
    latest_status = next(
        (event for event in reversed(list(events)) if isinstance(event, StatusEvent)),
        None,
    )
    if (
        latest_status is None
        or latest_status.status is not ConversationStatus.IDLE
        or latest_status.detail != "killed"
    ):
        return False
    agent_view_id, run_intent_id, strict = authority
    if not strict:
        return True
    if agent_view_id is not None:
        return latest_status.agent_view_id == agent_view_id
    return run_intent_id is not None and latest_status.run_intent_id == run_intent_id


def _authority_is_current_for_events(
    events: Iterable[Event],
    authority: LifecycleAuthority,
) -> bool:
    history = list(events)
    agent_view_id, run_intent_id, strict = authority
    if not strict:
        return True
    if agent_view_id is None and run_intent_id is None:
        return False
    latest_intent = latest_workspace_run_intent(history)
    if run_intent_id is not None and (latest_intent is None or latest_intent.id != run_intent_id):
        return False
    current_view_id = current_workspace_agent_view_id(history)
    if agent_view_id is not None:
        return current_view_id == agent_view_id
    return current_view_id is None


class LifecycleFence(Protocol):
    """The lock/process-fence port; it contains no transition policy."""

    def _lifecycle_fence(
        self,
        conversation_id: str,
    ) -> AbstractAsyncContextManager[None]: ...

    def _lifecycle_locked_fence(
        self,
        conversation_id: str,
    ) -> AbstractAsyncContextManager[None]: ...

    def _require_lifecycle_fence(self, conversation_id: str) -> None: ...


class LifecycleTerminalEffectPort(Protocol):
    """Workspace effects performed only after lifecycle authorization."""

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
    ) -> StatusEvent: ...

    async def commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent: ...


class LifecyclePersistence(Protocol):
    """Narrow already-fenced workspace persistence operation."""

    async def _commit_finished_workspace_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool,
        host_mirror: bool,
        snapshot_fn: Callable[..., object] | None,
    ) -> StatusEvent: ...


class WorkspaceLifecycleTerminalEffects:
    """Adapt workspace persistence after the command service authorizes."""

    def __init__(
        self,
        persistence: LifecyclePersistence,
        snapshot_provider: Callable[[], Callable[..., object]],
    ) -> None:
        self._persistence = persistence
        self._snapshot_provider = snapshot_provider

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
    ) -> StatusEvent:
        return await self._persistence._commit_finished_workspace_locked(
            conversation_id,
            terminal_event,
            require_inactive_finished_head=require_inactive_finished_head,
            host_mirror=False,
            snapshot_fn=self._snapshot_provider(),
        )

    async def commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent:
        return await self._persistence._commit_finished_workspace_locked(
            conversation_id,
            terminal_event,
            require_inactive_finished_head=True,
            host_mirror=True,
            snapshot_fn=None,
        )


class LifecycleCommandService:
    """Authorize, sequence, and persist every Agent Server status transition."""

    def __init__(
        self,
        *,
        store: EventStore,
        fence: LifecycleFence,
        terminal_effects: LifecycleTerminalEffectPort,
    ) -> None:
        self._store = store
        self._fence = fence
        self._terminal_effects = terminal_effects

    @staticmethod
    def build_status(
        status: ConversationStatus,
        *,
        detail: str | None = None,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
        host_mutation_id: str | None = None,
        source: EventSource = EventSource.SYSTEM,
    ) -> StatusEvent:
        """Construct the one lifecycle event shape used by Agent Server."""

        return StatusEvent(
            source=source,
            status=status,
            detail=detail,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
            host_mutation_id=host_mutation_id,
        )

    @staticmethod
    def _resolved_authority(events: Iterable[Event]) -> LifecycleAuthority:
        history = list(events)
        latest_intent = latest_workspace_run_intent(history)
        strict = latest_intent is not None and latest_intent.run_protocol_version == 1
        if strict and latest_intent is not None:
            return current_workspace_agent_view_id(history), latest_intent.id, True
        return None, None, False

    async def resolve_current_authority(
        self,
        conversation_id: str,
    ) -> LifecycleAuthority:
        """Capture the durable run head under the final workspace fence."""

        async with self._fence._lifecycle_fence(conversation_id):
            return self._resolved_authority(await self._store.get_events(conversation_id))

    def authority_already_killed(
        self,
        events: Iterable[Event],
        authority: LifecycleAuthority,
    ) -> bool:
        """Return whether this exact durable run already landed IDLE/killed."""

        return _authority_already_killed(events, authority)

    def authority_is_current_for_events(
        self,
        events: Iterable[Event],
        authority: LifecycleAuthority,
    ) -> bool:
        """Validate captured authority against one fenced event head."""

        return _authority_is_current_for_events(events, authority)

    async def _authority_is_current_locked(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        self._fence._require_lifecycle_fence(conversation_id)
        events = await self._store.get_events(conversation_id)
        return self.authority_is_current_for_events(
            events,
            (agent_view_id, run_intent_id, True),
        )

    async def authority_is_current(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        """Atomically validate a durable task target."""

        if agent_view_id is None and run_intent_id is None:
            return True
        async with self._fence._lifecycle_fence(conversation_id):
            return await self._authority_is_current_locked(
                conversation_id,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            )

    @staticmethod
    def _authorize_status(
        events: list[Event],
        event: StatusEvent,
        *,
        bind_current: bool,
    ) -> StatusEvent:
        """Bind or validate one status against the exact fenced durable head."""

        if event.host_mutation_id is not None:
            return event
        current_view, current_intent, strict = LifecycleCommandService._resolved_authority(events)
        if not strict:
            return event
        if event.agent_view_id is not None:
            if event.agent_view_id != current_view:
                raise LifecycleTransitionRejected("stale agent view lifecycle transition")
            return event
        if event.run_intent_id is not None:
            if event.run_intent_id != current_intent or current_view is not None:
                raise LifecycleTransitionRejected("stale run intent lifecycle transition")
            return event
        if not bind_current:
            raise LifecycleTransitionRejected(
                "strict lifecycle transition requires durable run authority"
            )
        return event.model_copy(
            update={
                "agent_view_id": current_view,
                "run_intent_id": None if current_view is not None else current_intent,
            }
        )

    async def _append_authorized_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
        *,
        bind_current: bool,
    ) -> StatusEvent:
        self._fence._require_lifecycle_fence(conversation_id)
        authorized = self._authorize_status(
            await self._store.get_events(conversation_id),
            event,
            bind_current=bind_current,
        )
        stored = await self._store.append(conversation_id, authorized)
        if not isinstance(stored, StatusEvent):
            raise RuntimeError("event store returned wrong status event type")
        return stored

    async def append_status(
        self,
        conversation_id: str,
        event: StatusEvent | ConversationStatus,
        *,
        detail: str | None = None,
    ) -> StatusEvent:
        """Append a pre-authorized transition under one final fence."""

        if isinstance(event, ConversationStatus):
            event = self.build_status(event, detail=detail)
        elif detail is not None:
            raise ValueError("detail is valid only when constructing a status")
        async with self._fence._lifecycle_fence(conversation_id):
            return await self._append_authorized_locked(
                conversation_id,
                event,
                bind_current=False,
            )

    async def append_status_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Append a pre-authorized transition while the caller owns the fence."""

        return await self._append_authorized_locked(
            conversation_id,
            event,
            bind_current=False,
        )

    async def append_task_status_if_current(
        self,
        conversation_id: str,
        event: StatusEvent,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
        prefix_events: list[Event] | None = None,
    ) -> StatusEvent | None:
        """Validate and append a task transition in one indivisible fence."""

        async with self._fence._lifecycle_fence(conversation_id):
            if (
                agent_view_id is not None or run_intent_id is not None
            ) and not await self._authority_is_current_locked(
                conversation_id,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            ):
                return None
            history = await self._store.get_events(conversation_id)
            owned = event.model_copy(
                update={
                    "agent_view_id": agent_view_id,
                    "run_intent_id": None if agent_view_id is not None else run_intent_id,
                }
            )
            authorized = self._authorize_status(history, owned, bind_current=False)
            if prefix_events:
                stored_events = await self._store.append_many(
                    conversation_id,
                    [*prefix_events, authorized],
                )
                stored = stored_events[-1]
            else:
                stored = await self._store.append(conversation_id, authorized)
            if not isinstance(stored, StatusEvent):
                raise RuntimeError("event store returned wrong status event type")
            return stored

    async def append_transition_batch_locked(
        self,
        conversation_id: str,
        events: list[Event],
        *,
        bind_current: bool = False,
        ingress_intent: WorkspaceMutationEvent | None = None,
    ) -> list[Event]:
        """Atomically persist domain facts with their one authorized transition."""

        self._fence._require_lifecycle_fence(conversation_id)
        statuses = [event for event in events if isinstance(event, StatusEvent)]
        if len(statuses) != 1:
            raise ValueError("lifecycle transition batch requires exactly one status event")
        status = statuses[0]
        if ingress_intent is not None:
            if ingress_intent.run_protocol_version != 1 or not ingress_intent.operation.startswith(
                "agent.run-intent."
            ):
                raise ValueError("lifecycle ingress requires a v1 run intent")
            if status.agent_view_id is not None or (
                status.run_intent_id is not None and status.run_intent_id != ingress_intent.id
            ):
                raise LifecycleTransitionRejected("ingress status has conflicting authority")
            authorized = status.model_copy(
                update={"agent_view_id": None, "run_intent_id": ingress_intent.id}
            )
        else:
            authorized = self._authorize_status(
                await self._store.get_events(conversation_id),
                status,
                bind_current=bind_current,
            )
        pending = [authorized if event is status else event for event in events]
        return await self._store.append_many(conversation_id, pending)

    async def append_current_run_transition(
        self,
        conversation_id: str,
        events: list[Event],
        status: StatusEvent,
        *,
        expected_statuses: frozenset[ConversationStatus],
    ) -> list[Event] | None:
        """Apply a host lifecycle decision only to its fenced current state."""

        async with self._fence._lifecycle_fence(conversation_id):
            state = await self._store.get_state(conversation_id)
            if state.execution_status not in expected_statuses:
                return None
            return await self.append_transition_batch_locked(
                conversation_id,
                [*events, status],
                bind_current=True,
            )

    def terminal_commit_hook(
        self,
        conversation_id: str,
        *,
        authority_provider: Callable[[], tuple[str | None, str | None]] | None = None,
    ) -> Callable[[StatusEvent], Awaitable[StatusEvent]]:
        """Bind Core's existing private status-emission boundary."""

        async def commit(event: StatusEvent) -> StatusEvent:
            if (
                event.agent_view_id is None
                and event.run_intent_id is None
                and authority_provider is not None
            ):
                agent_view_id, run_intent_id = authority_provider()
                event = event.model_copy(
                    update={
                        "agent_view_id": agent_view_id,
                        "run_intent_id": (None if agent_view_id is not None else run_intent_id),
                    }
                )
            if event.status is not ConversationStatus.FINISHED:
                return await self.append_status(conversation_id, event)
            async with self._fence._lifecycle_fence(conversation_id):
                authorized = self._authorize_status(
                    await self._store.get_events(conversation_id),
                    event,
                    bind_current=False,
                )
                return await self._terminal_effects.commit_finished_workspace(
                    conversation_id,
                    authorized,
                )

        return commit

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
    ) -> StatusEvent:
        """Authorize a FINISHED command before invoking workspace effects."""

        if terminal_event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal workspace commit requires FINISHED")
        async with self._fence._lifecycle_fence(conversation_id):
            authorized = self._authorize_status(
                await self._store.get_events(conversation_id),
                terminal_event,
                bind_current=False,
            )
            return await self._terminal_effects.commit_finished_workspace(
                conversation_id,
                authorized,
                require_inactive_finished_head=require_inactive_finished_head,
            )

    async def _commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent:
        if terminal_event.status is not ConversationStatus.FINISHED:
            raise ValueError("terminal workspace commit requires FINISHED")
        async with self._fence._lifecycle_locked_fence(conversation_id):
            authorized = self._authorize_status(
                await self._store.get_events(conversation_id),
                terminal_event,
                bind_current=False,
            )
            return await self._terminal_effects.commit_finished_host_mirror_locked(
                conversation_id,
                authorized,
            )
