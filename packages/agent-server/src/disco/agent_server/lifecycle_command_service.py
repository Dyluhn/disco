"""LifecycleCommandService — the sole agent-server lifecycle transition service.

PKG-06-LIFECYCLE: one bounded, typed application service that decides
lifecycle transition legality and idempotency, constructs lifecycle
``StatusEvent`` values, validates durable ``agent_view_id``/``run_intent_id``
under the final workspace fence, and orders transition persistence with
lifecycle effects.

EventStore remains the persistence/sequence port and WorkspaceCoordinator
remains the lock/process-fence port; neither decides transitions.  The
service reaches them through narrow Protocol types — no ``Any`` runtime
back-references, service locators, dynamic imports, compatibility aliases,
or a generic dependency bag.

The existing status-transition sites (ControlOps, ResumeService,
LifecycleManager collaborators, ConversationRuntime supervisors/preflight,
DeepResearchService, WorkspaceCoordinator terminal paths, and DiscoKernel)
route through this service.  Domain facts continue through their existing
ports; this service does not centralize non-status events.

Routes and public runtime/BuildKernel method signatures remain exact.  Core
remains target-neutral: the injected private status sink/store adapter is
consumed at Core's existing emit boundary
(``engine_contracts._route_event`` via ``terminal_commit_hook``) without
changing the accepted AgentLoop public surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    StatusEvent,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


@runtime_checkable
class LifecycleEventSink(Protocol):
    """Persistence/sequence port for lifecycle status transitions.

    EventStore remains the persistence/sequence port; it does not decide
    transitions.  The service appends constructed ``StatusEvent`` values
    through this narrow seam.
    """

    async def append_status(self, conversation_id: str, event: StatusEvent) -> StatusEvent:
        """Append one status event and return the stored envelope."""
        ...


@runtime_checkable
class LifecycleFence(Protocol):
    """Lock/process-fence port for lifecycle transitions.

    WorkspaceCoordinator remains the lock/process-fence port; it does not
    decide transitions.  The service validates durable authority under this
    fence and appends through it when the caller does not already hold it.
    """

    def lock(self, conversation_id: str) -> object:
        """Return the per-conversation lock (the coordinator's lock)."""
        ...

    async def run_authority_is_current(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        """Check one task's durable authority under the cross-process fence."""
        ...

    async def append_status_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Append after a caller-owned fence and its last-moment policy checks."""
        ...


@runtime_checkable
class LifecycleEventReader(Protocol):
    """Read-only event history port for durable authority validation."""

    async def get_events(self, conversation_id: str) -> list[Event]:
        """Return the current event history for a conversation."""
        ...


class LifecycleCommandService:
    """Sole agent-server application service for lifecycle transitions.

    Decides transition legality and idempotency, constructs ``StatusEvent``
    values, validates durable ``agent_view_id``/``run_intent_id`` under the
    final workspace fence, and orders transition persistence with lifecycle
    effects.  EventStore remains the persistence/sequence port and
    WorkspaceCoordinator remains the lock/process-fence port; neither decides
    transitions.
    """

    def __init__(
        self,
        sink: LifecycleEventSink,
        fence: LifecycleFence,
        reader: LifecycleEventReader,
    ) -> None:
        self._sink = sink
        self._fence = fence
        self._reader = reader

    # ------------------------------------------------------------------
    # StatusEvent construction — the sole construction site for lifecycle
    # status values that route through this service.
    # ------------------------------------------------------------------

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
        """Construct a lifecycle ``StatusEvent`` with one terminal authority.

        A status event carries at most one of agent-view, host-mutation, or
        run-intent authority (enforced by ``StatusEvent._one_terminal_authority``).
        This constructor is the sole site that builds status values for
        transitions routed through the service.
        """
        return StatusEvent(
            source=source,
            status=status,
            detail=detail,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
            host_mutation_id=host_mutation_id,
        )

    # ------------------------------------------------------------------
    # Durable authority validation under the final workspace fence.
    # ------------------------------------------------------------------

    async def authority_is_current(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        """Validate durable authority under the cross-process fence.

        Local generation is diagnostic only.  Durable
        ``{agent_view_id, run_intent_id}``, re-read under the final fence, is
        the authorization decision and must accept a current durable target
        even when local generation is absent/stale.  A stale durable target
        must fail.
        """
        if agent_view_id is None and run_intent_id is None:
            return True
        return await self._fence.run_authority_is_current(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def resolve_current_authority(
        self,
        conversation_id: str,
    ) -> tuple[str | None, str | None, bool]:
        """Resolve the durable authority at the current fenced head.

        Returns ``(agent_view_id, run_intent_id, strict)`` where ``strict`` is
        True when a v1 run intent exists.  A strict authority binds the
        transition to the exact admitted view (or its pre-admission intent).
        """
        events = await self._reader.get_events(conversation_id)
        latest_intent = latest_workspace_run_intent(events)
        strict = latest_intent is not None and latest_intent.run_protocol_version == 1
        if strict and latest_intent is not None:
            return (
                current_workspace_agent_view_id(events),
                latest_intent.id,
                True,
            )
        return None, None, False

    @staticmethod
    def authority_already_killed(
        events: Iterable[Event],
        authority: tuple[str | None, str | None, bool],
    ) -> bool:
        """Return whether this exact durable run already landed IDLE/killed."""
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

    @staticmethod
    def authority_is_current_for_events(
        events: Iterable[Event],
        authority: tuple[str | None, str | None, bool],
    ) -> bool:
        """Revalidate a captured authority against the final fenced head."""
        agent_view_id, run_intent_id, strict = authority
        if not strict:
            return True
        if agent_view_id is None and run_intent_id is None:
            return False
        latest_intent = latest_workspace_run_intent(events)
        if run_intent_id is not None and (
            latest_intent is None or latest_intent.id != run_intent_id
        ):
            return False
        current_view_id = current_workspace_agent_view_id(events)
        if agent_view_id is not None:
            return current_view_id == agent_view_id
        # A post-capture winning view may belong to a peer and was never cancelled.
        return current_view_id is None

    # ------------------------------------------------------------------
    # Transition persistence — ordered with lifecycle effects.
    # ------------------------------------------------------------------

    async def append_status(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Append one lifecycle status through the persistence port.

        The caller may or may not already hold the workspace fence; the sink
        serializes Build status transitions with host mutations.
        """
        return await self._sink.append_status(conversation_id, event)

    async def append_status_locked(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        """Append one lifecycle status after a caller-owned fence.

        The caller MUST already hold the workspace lock and (for Build-like
        surfaces) the interprocess mutation fence.  The fence port performs
        the last-moment policy checks.
        """
        return await self._fence.append_status_locked(conversation_id, event)

    async def append_task_status_if_current(
        self,
        conversation_id: str,
        event: StatusEvent,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> StatusEvent | None:
        """Append a task-caused status only for its durable run.

        Historical and non-Build callers do not have strict run authority and
        retain the legacy behavior.  Registered Build tasks always capture at
        least the ingress intent; after materialization they prefer the exact
        view id.  The fence performs the last-moment recheck and append under
        the local plus process workspace fence.
        """
        if agent_view_id is None and run_intent_id is None:
            return await self._sink.append_status(conversation_id, event)
        if not await self.authority_is_current(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        ):
            return None
        owned = event.model_copy(
            update={
                "agent_view_id": agent_view_id,
                "run_intent_id": None if agent_view_id is not None else run_intent_id,
            }
        )
        return await self._fence.append_status_locked(conversation_id, owned)