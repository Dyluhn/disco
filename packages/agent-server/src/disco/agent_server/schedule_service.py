"""Typed schedule ownership and the legacy manager compatibility boundary."""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING, Any, Protocol, cast

from disco.core import DEFAULT_OWNER_ID, MessageEvent
from disco.core.llm import DefaultLLMRouter
from disco.core.loop import AgentLoop, RouterAgent
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import ScheduleSpec, WorkflowRun
from disco.tools.projects import ProjectStore

from .driver_context import ResolvedDriverContext
from .run_registry import (
    LoopRegistry,
    RunAuthorityLedger,
    RunIngressLedger,
    RunRegistry,
    RunResourceRegistry,
)
from .workflow_schedule import WorkflowScheduleRunRecord

if TYPE_CHECKING:
    from .schedule import ScheduleRuntime


class WorkflowConversationSettings(Protocol):
    def set_surface(self, conversation_id: str, surface: str) -> None: ...

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None: ...


class WorkflowModelAccess(Protocol):
    async def _resolve_driver_context(self, conversation_id: str) -> ResolvedDriverContext: ...

    def _router_now(
        self,
        pick: str | None = None,
        *,
        surface: str | None = None,
        autonomous: bool = False,
        conversation_id: str | None = None,
    ) -> DefaultLLMRouter: ...


class WorkflowLoopFactory(Protocol):
    def _compose_build_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
    ) -> AgentLoop: ...


class WorkflowRunControl(Protocol):
    _run_registry: RunRegistry
    _run_authorities: RunAuthorityLedger
    _run_ingress: RunIngressLedger
    _loop_registry: LoopRegistry
    _run_resources: RunResourceRegistry
    _driver_contexts: WorkflowDriverContexts

    def workspace_lock(self, conversation_id: str) -> asyncio.Lock: ...

    def _create_run_task(
        self,
        conversation_id: str,
        loop: AgentLoop | None = None,
        *,
        expected_run_intent_id: str | None = None,
        task_context: contextvars.Context | None = None,
    ) -> tuple[asyncio.Task[Any], int]: ...

    async def _finalize_clean_return(
        self,
        conversation_id: str,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None: ...

    async def _rekick_unadmitted_superseding_intent(self, conversation_id: str) -> None: ...


class WorkflowDriverContexts(Protocol):
    def begin_compose(self, conversation_id: str, snapshot: ResolvedDriverContext) -> None: ...

    def end_compose(self, conversation_id: str, snapshot: ResolvedDriverContext) -> None: ...

    def bind_resolved(self, conversation_id: str, snapshot: ResolvedDriverContext) -> None: ...


class WorkflowRunner(Protocol):
    async def run(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str,
        coalesced: bool,
    ) -> WorkflowScheduleRunRecord: ...


class WorkflowProjectAccess(Protocol):
    def _project_store_now(self) -> ProjectStore: ...


class RecurringScheduleControl(Protocol):
    def set_model_override(self, conversation_id: str, model_id: str | None) -> None: ...

    def set_depth(self, conversation_id: str, tier: str | None) -> None: ...

    async def send_user_turn(self, conversation_id: str, text: str) -> MessageEvent: ...


class WorkflowErrorRecorder(Protocol):
    async def __call__(
        self,
        conversation_id: str,
        *,
        detail: str,
        explanation: str,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> bool: ...


class _ScheduleRuntimeAdapter:
    """Static compatibility port for ScheduleManager pending its later package."""

    def __init__(
        self,
        workflow_runs: WorkflowRunner,
        project_access: WorkflowProjectAccess,
        recurring_control: RecurringScheduleControl,
    ) -> None:
        self._workflow_runs = workflow_runs
        self._project_access = project_access
        self._recurring_control = recurring_control

    def _project_store_now(self) -> ProjectStore:
        return self._project_access._project_store_now()

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        self._recurring_control.set_model_override(conversation_id, model_id)

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        self._recurring_control.set_depth(conversation_id, tier)

    async def send_user_turn(self, conversation_id: str, text: str) -> MessageEvent:
        return await self._recurring_control.send_user_turn(conversation_id, text)

    async def run_sealed_workflow_schedule(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str,
        coalesced: bool,
    ) -> WorkflowScheduleRunRecord:
        return await self._workflow_runs.run(
            schedule_id=schedule_id,
            spec=spec,
            owner_id=owner_id,
            coalesced=coalesced,
        )


class ScheduleService:
    def __init__(
        self,
        store: SqliteEventStore,
        *,
        workflow_runs: WorkflowRunner,
        project_access: WorkflowProjectAccess,
        recurring_control: RecurringScheduleControl,
    ) -> None:
        self._store = store
        self._workflow_runs = workflow_runs
        self._runtime_port = _ScheduleRuntimeAdapter(
            workflow_runs,
            project_access,
            recurring_control,
        )
        self._sched_manager: Any | None = None

    def _schedule_manager(self) -> Any:
        """Create the legacy manager lazily without giving it the runtime."""
        if self._sched_manager is None:
            from .schedule import ScheduleManager

            self._sched_manager = ScheduleManager(
                self._store,
                cast("ScheduleRuntime", self._runtime_port),
            )
        return self._sched_manager

    async def run_sealed_workflow_schedule(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str = DEFAULT_OWNER_ID,
        coalesced: bool = False,
    ) -> WorkflowScheduleRunRecord:
        """Compatibility entrypoint retained until PKG-13 facade deletion."""

        return await self._workflow_runs.run(
            schedule_id=schedule_id,
            spec=spec,
            owner_id=owner_id,
            coalesced=coalesced,
        )

    async def _schedule_manager_loop(self) -> None:
        """Thin trampoline: lifespan task → ScheduleManager.run().  Mirrors the
        _idle_sweep_loop pattern so the app lifespan owns the Task."""
        await self._schedule_manager().run()

    def create_schedule(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        rrule: str,
        description: str,
        timezone: str = "UTC",
        depth: str | None = None,
        model_override: str | None = None,
    ) -> dict:
        """Create a new schedule and return it as a JSON-safe dict.
        Raises ValueError for invalid cron expressions (caller surfaces to user)."""
        sched = self._schedule_manager().create_schedule(
            conversation_id=conversation_id,
            owner_id=owner_id,
            rrule=rrule,
            description=description,
            timezone=timezone,
            depth=depth,
            model_override=model_override,
        )
        return sched.model_dump(mode="json")

    def list_schedules(self, *, owner_id: str, conversation_id: str | None = None) -> list[dict]:
        """List schedules, optionally filtered to one conversation."""
        return [
            s.model_dump(mode="json")
            for s in self._schedule_manager().list_schedules(
                owner_id=owner_id, conversation_id=conversation_id
            )
        ]

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        """Delete a schedule. OWNER-SCOPED. Returns True if a row was removed."""
        return self._schedule_manager().delete_schedule(schedule_id, owner_id=owner_id)

    async def fire_now(self, schedule_id: str, *, owner_id: str) -> bool:
        """Run a schedule immediately, out of band. False if not found."""
        return await self._schedule_manager().fire_now(schedule_id, owner_id=owner_id)

    def create_workflow_schedule(
        self,
        spec: ScheduleSpec,
        *,
        owner_id: str,
    ) -> dict:
        row = self._schedule_manager().create_workflow_schedule(
            spec,
            owner_id=owner_id,
        )
        return row.model_dump(mode="json")

    def list_workflow_schedules(
        self,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> list[dict]:
        return [
            row.model_dump(mode="json")
            for row in self._schedule_manager().list_workflow_schedules(
                owner_id=owner_id,
                include_unclaimed_legacy=include_unclaimed_legacy,
            )
        ]

    def list_workflow_schedule_runs(
        self,
        *,
        schedule_id: str | None = None,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        return [
            row.model_dump(mode="json")
            for row in self._schedule_manager().list_workflow_schedule_runs(
                schedule_id=schedule_id,
                owner_id=owner_id,
                include_unclaimed_legacy=include_unclaimed_legacy,
                limit=limit,
            )
        ]

    async def fire_workflow_schedule_now(
        self,
        schedule_id: str,
        *,
        owner_id: str,
        include_unclaimed_legacy: bool = False,
    ) -> dict | None:
        row = await self._schedule_manager().fire_workflow_schedule_now(
            schedule_id,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )
        if row is None:
            return None
        return row.model_dump(mode="json")

    def preview_schedule_runs(
        self,
        rrule: str,
        n: int = 3,
        *,
        timezone: str = "UTC",
    ) -> list[str]:
        """Preview next N run times for a cron expression (ISO-8601 strings).
        Returns [] for invalid expressions."""
        return [
            dt.isoformat()
            for dt in self._schedule_manager().preview_next_runs(
                rrule,
                n,
                timezone=timezone,
            )
        ]

    def list_recent_schedule_runs(self, *, owner_id: str, limit: int = 50) -> list[dict]:
        """Owner-scoped recent scheduled-run history for the activity dashboard
        (delegates to the store; newest first, with schedule description + title)."""
        return self._store.list_recent_schedule_runs(owner_id, limit)
