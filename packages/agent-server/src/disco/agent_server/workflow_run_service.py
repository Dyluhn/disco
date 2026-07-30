from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationState,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    WorkspaceMutationEvent,
)
from disco.core.loop import AgentLoop, BuildAgent
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import ScheduleSpec, WorkflowRun
from disco.tools import DefaultToolExecutor
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore

from .driver_context import DriverContextResolutionError
from .lifecycle_command_service import LifecycleCommandService
from .schedule_service import (
    WorkflowConversationSettings,
    WorkflowErrorRecorder,
    WorkflowLoopFactory,
    WorkflowModelAccess,
    WorkflowProjectAccess,
    WorkflowRunControl,
)
from .workflow_schedule import WorkflowScheduleRunRecord
from .workspace_commit import WorkspaceRunSuperseded
from .workspace_service import WorkspaceCoordinator


class _OutputPathParams(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class _Setup:
    conversation_id: str
    fired_at: datetime
    workflow_run: WorkflowRun | None
    output_path: str
    message: MessageEvent | None
    failure: WorkflowScheduleRunRecord | None = None


@dataclass(frozen=True)
class _ExecutionPlan:
    schedule_id: str
    spec: ScheduleSpec
    conversation_id: str
    fired_at: datetime
    output_path: str
    message: MessageEvent
    coalesced: bool
    loop: AgentLoop
    previous_executor: DefaultToolExecutor | None
    sealed_executor: DefaultToolExecutor | None
    intent: WorkspaceMutationEvent
    task_context: contextvars.Context


def _render_output_path(template: str, params: dict[str, object]) -> str:
    try:
        return template.format_map(_OutputPathParams(params))
    except (KeyError, IndexError, ValueError):
        return template


def _history_fields(events: list[Event], *, fallback_output_path: str) -> tuple[str, str]:
    for event in reversed(events):
        if not isinstance(event, StatusEvent):
            continue
        detail = event.detail or ""
        if not detail.startswith("workflow_output_contract_"):
            continue
        return (
            str(event.meta.get("workflow_output_path") or fallback_output_path),
            str(event.meta.get("verdict") or "unverified"),
        )
    return fallback_output_path, "unverified"


def _failed_record(
    *,
    schedule_id: str,
    conversation_id: str,
    fired_at: datetime,
    output_path: str,
    coalesced: bool,
    error: str,
) -> WorkflowScheduleRunRecord:
    return WorkflowScheduleRunRecord(
        schedule_id=schedule_id,
        run_cid=conversation_id,
        fired_at=fired_at,
        terminal_state=ConversationStatus.ERROR.value,
        output_path=output_path,
        verify_verdict="error",
        coalesced=coalesced,
        error=error,
    )


def _schedule_message(
    schedule_id: str,
    spec: ScheduleSpec,
    output_path: str,
    params: dict[str, object],
) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(
            role="user",
            content=(
                "Run this sealed workflow schedule.\n"
                f"Schedule id: {schedule_id}\n"
                f"Workflow instance: {spec.instance_id}\n"
                f"Definition digest: {spec.instance_digest}\n"
                f"Output path: {output_path}\n"
                "Parameters:\n"
                f"{json.dumps(params, sort_keys=True)}\n\n"
                "Outcome rules:\n"
                "If the task cannot be completed with the provided tools/params "
                "(missing information, unreachable target, impossible request): "
                "call needs_input with the question, or skip with the reason. "
                "NEVER fabricate results and NEVER finish with a failure narrative "
                "as if the task succeeded."
            ),
        ),
    )


class _WorkflowRunExecution:
    def __init__(
        self,
        store: SqliteEventStore,
        workspace: WorkspaceCoordinator,
        model_access: WorkflowModelAccess,
        loop_factory: WorkflowLoopFactory,
        run_control: WorkflowRunControl,
        record_error: WorkflowErrorRecorder,
    ) -> None:
        self._store, self._workspace = store, workspace
        self._model_access, self._loop_factory = model_access, loop_factory
        self._run_control, self._record_error = run_control, record_error

    async def execute(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        conversation_id: str,
        fired_at: datetime,
        workflow_run: WorkflowRun,
        output_path: str,
        message: MessageEvent,
        coalesced: bool,
    ) -> ConversationState | WorkflowScheduleRunRecord:
        failed = self._failure_factory(
            schedule_id, conversation_id, fired_at, output_path, coalesced
        )
        incumbent = self._run_control._run_registry.active_task(conversation_id)
        if incumbent is not None:
            return failed("sealed workflow schedule found an existing live run task")
        plan = await self._compose(
            schedule_id=schedule_id,
            spec=spec,
            conversation_id=conversation_id,
            fired_at=fired_at,
            workflow_run=workflow_run,
            output_path=output_path,
            message=message,
            coalesced=coalesced,
        )
        if isinstance(plan, WorkflowScheduleRunRecord):
            return plan
        registration = await self._register(plan)
        if isinstance(registration, WorkflowScheduleRunRecord):
            return registration
        task, generation = registration
        return await self._await_run(plan, task, generation)

    def _failure_factory(
        self,
        schedule_id: str,
        conversation_id: str,
        fired_at: datetime,
        output_path: str,
        coalesced: bool,
    ) -> Callable[[str], WorkflowScheduleRunRecord]:
        return lambda error: _failed_record(
            schedule_id=schedule_id,
            conversation_id=conversation_id,
            fired_at=fired_at,
            output_path=output_path,
            coalesced=coalesced,
            error=error,
        )

    async def _compose(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        conversation_id: str,
        fired_at: datetime,
        workflow_run: WorkflowRun,
        output_path: str,
        message: MessageEvent,
        coalesced: bool,
    ) -> _ExecutionPlan | WorkflowScheduleRunRecord:
        failed = self._failure_factory(
            schedule_id, conversation_id, fired_at, output_path, coalesced
        )
        try:
            snapshot = await self._model_access._resolve_driver_context(conversation_id)
        except DriverContextResolutionError as exc:
            await self._record_error(
                conversation_id,
                detail="workflow_driver_context_resolution_failed",
                explanation=(
                    f"Workflow schedule {schedule_id} could not resolve its driver "
                    f"context before execution: {exc}"
                ),
            )
            return failed(str(exc))
        self._run_control._driver_contexts.begin_compose(conversation_id, snapshot)
        try:
            router = self._model_access._router_now(
                pick=snapshot.model_key,
                surface="agent",
                autonomous=True,
                conversation_id=conversation_id,
            )
            agent = BuildAgent(
                router,
                conversation_id=conversation_id,
                model_override=snapshot.model_key,
                driver_context_window=snapshot.context_window,
            )
            previous_executor = self._run_control._run_resources.executor(conversation_id)
            try:
                loop = self._loop_factory._compose_build_loop(
                    conversation_id,
                    router,
                    agent,
                    driver_context_window=snapshot.context_window,
                    sealed_workflow_run=workflow_run,
                    sealed_workflow_instance_id=spec.instance_id,
                )
            except ValueError as exc:
                await self._record_error(
                    conversation_id,
                    detail="workflow_scope_compile_failed",
                    explanation=(
                        f"Workflow schedule {schedule_id} could not run because its "
                        f"sealed tool scope failed to compile: {exc}"
                    ),
                )
                return failed(str(exc))
        finally:
            self._run_control._driver_contexts.end_compose(conversation_id, snapshot)
        self._run_control._driver_contexts.bind_resolved(conversation_id, snapshot)
        sealed_executor = self._run_control._run_resources.executor(conversation_id)
        loop.stream_sink = lambda frame: self._store.publish_ephemeral(conversation_id, frame)
        return _ExecutionPlan(
            schedule_id=schedule_id,
            spec=spec,
            conversation_id=conversation_id,
            fired_at=fired_at,
            output_path=output_path,
            message=message,
            coalesced=coalesced,
            loop=loop,
            previous_executor=previous_executor,
            sealed_executor=sealed_executor,
            intent=WorkspaceMutationEvent(
                operation="agent.run-intent.workflow-schedule",
                run_protocol_version=1,
            ),
            task_context=contextvars.copy_context(),
        )

    async def _register(
        self,
        plan: _ExecutionPlan,
    ) -> tuple[asyncio.Task[Any], int] | WorkflowScheduleRunRecord:
        task_slot: list[asyncio.Task[Any] | None] = [None]
        try:
            task, generation = await self._register_under_fence(plan, task_slot)
            return task, generation
        except BaseException as exc:
            await self._clean_failed_registration(plan, task_slot[0])
            if isinstance(exc, asyncio.CancelledError):
                raise
            failed = self._failure_factory(
                plan.schedule_id,
                plan.conversation_id,
                plan.fired_at,
                plan.output_path,
                plan.coalesced,
            )
            if isinstance(exc, WorkspaceRunSuperseded):
                await self._run_control._rekick_unadmitted_superseding_intent(
                    plan.conversation_id
                )
                return failed(str(exc))
            await self._record_error(
                plan.conversation_id,
                detail="workflow_schedule_ingress_failed",
                explanation=f"Workflow schedule {plan.schedule_id} could not start: {exc}",
            )
            return failed(str(exc))

    async def _register_under_fence(
        self,
        plan: _ExecutionPlan,
        task_slot: list[asyncio.Task[Any] | None],
    ) -> tuple[asyncio.Task[Any], int]:
        task: asyncio.Task[Any] | None = None
        previous_loop: AgentLoop | None = None
        cid = plan.conversation_id
        async with self._run_control.workspace_lock(cid):
            async with self._workspace.interprocess_mutation_fence(cid):
                incumbent = self._run_control._run_registry.active_task(cid)
                if incumbent is not None:
                    raise WorkspaceRunSuperseded(
                        "sealed workflow schedule lost task registration authority"
                    )
                if await self._store.get_events(cid):
                    raise WorkspaceRunSuperseded(
                        "sealed workflow schedule lost pristine ingress authority"
                    )
                previous_loop = self._run_control._loop_registry.loop(cid)
                self._run_control._loop_registry.bind(cid, plan.loop)
                try:
                    task, generation = self._run_control._create_run_task(
                        cid,
                        plan.loop,
                        expected_run_intent_id=plan.intent.id,
                        task_context=plan.task_context,
                    )
                    task_slot[0] = task
                    self._workspace.claim_registered_run_locked(cid)
                    stored = await self._workspace.append_run_ingress_locked(
                        cid,
                        [plan.message],
                        "workflow-schedule",
                        intent=plan.intent,
                    )
                    stored_user = next(
                        (
                            event
                            for event in stored
                            if isinstance(event, MessageEvent) and event.id == plan.message.id
                        ),
                        None,
                    )
                    if stored_user is None or stored_user.seq is None:
                        raise RuntimeError("schedule ingress did not return its USER identity")
                    self._run_control._run_ingress.claim_user_seq(cid, stored_user.seq)
                    return task, generation
                except BaseException:
                    if task is not None and self._run_control._run_registry.detach_task_if_owned(
                        cid,
                        task,
                    ):
                        self._workspace.clear_run_claim(cid)
                        task.cancel()
                    if self._run_control._loop_registry.owns(cid, plan.loop):
                        if previous_loop is None:
                            self._run_control._loop_registry.forget(cid)
                        else:
                            self._run_control._loop_registry.bind(cid, previous_loop)
                    raise

    async def _clean_failed_registration(
        self,
        plan: _ExecutionPlan,
        task: asyncio.Task[Any] | None,
    ) -> None:
        cid = plan.conversation_id
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._run_control._run_authorities.discard(task)
        if self._run_control._run_resources.owns_executor(cid, plan.sealed_executor):
            if plan.previous_executor is None:
                self._run_control._run_resources.pop_executor(cid)
            else:
                self._run_control._run_resources.set_executor(cid, plan.previous_executor)
        if (
            plan.sealed_executor is not None
            and plan.sealed_executor is not plan.previous_executor
        ):
            with contextlib.suppress(Exception):
                await plan.sealed_executor.kill()

    async def _await_run(
        self,
        plan: _ExecutionPlan,
        task: asyncio.Task[Any],
        generation: int,
    ) -> ConversationState | WorkflowScheduleRunRecord:
        run_error: Exception | None = None
        state: ConversationState | None = None
        try:
            state = cast(ConversationState, await task)
        except Exception as exc:
            run_error = exc
        finally:
            cid = plan.conversation_id
            authority = self._run_control._run_authorities.discard(task)
            owned = self._run_control._run_registry.complete_task(cid, task)
            if owned:
                self._workspace.clear_run_claim(cid)
            agent_view_id, run_intent_id = authority
            if run_intent_id is None:
                run_intent_id = plan.intent.id
        if run_error is not None:
            return await self._run_failure(
                plan,
                run_error,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            )
        assert state is not None
        await self._run_control._finalize_clean_return(
            plan.conversation_id,
            generation,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )
        return state

    async def _run_failure(
        self,
        plan: _ExecutionPlan,
        error: Exception,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> WorkflowScheduleRunRecord:
        failed = self._failure_factory(
            plan.schedule_id,
            plan.conversation_id,
            plan.fired_at,
            plan.output_path,
            plan.coalesced,
        )
        if isinstance(error, WorkspaceRunSuperseded):
            await self._run_control._rekick_unadmitted_superseding_intent(
                plan.conversation_id
            )
            return failed(str(error))
        recorded = await self._record_error(
            plan.conversation_id,
            detail="workflow_schedule_run_failed",
            explanation=(
                f"Workflow schedule {plan.schedule_id} failed while running: {error}"
            ),
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )
        if not recorded:
            await self._run_control._rekick_unadmitted_superseding_intent(
                plan.conversation_id
            )
        return failed(str(error))


class WorkflowRunService:
    def __init__(
        self,
        store: SqliteEventStore,
        *,
        lifecycle_commands: LifecycleCommandService,
        workspace: WorkspaceCoordinator,
        project_access: WorkflowProjectAccess,
        settings: WorkflowConversationSettings,
        model_access: WorkflowModelAccess,
        loop_factory: WorkflowLoopFactory,
        run_control: WorkflowRunControl,
    ) -> None:
        self._store, self._lifecycle_commands = store, lifecycle_commands
        self._project_access, self._settings = project_access, settings
        self._execution = _WorkflowRunExecution(
            store,
            workspace,
            model_access,
            loop_factory,
            run_control,
            self._record_error,
        )

    async def run(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str = DEFAULT_OWNER_ID,
        coalesced: bool = False,
    ) -> WorkflowScheduleRunRecord:
        setup = await self._conversation_setup(
            schedule_id=schedule_id,
            spec=spec,
            coalesced=coalesced,
            owner_id=owner_id,
        )
        if setup.failure is not None:
            return setup.failure
        assert setup.workflow_run is not None and setup.message is not None
        result = await self._execution.execute(
            schedule_id=schedule_id,
            spec=spec,
            conversation_id=setup.conversation_id,
            fired_at=setup.fired_at,
            workflow_run=setup.workflow_run,
            output_path=setup.output_path,
            message=setup.message,
            coalesced=coalesced,
        )
        if isinstance(result, WorkflowScheduleRunRecord):
            return result
        return await self._record_history(
            schedule_id=schedule_id,
            setup=setup,
            state=result,
            coalesced=coalesced,
        )

    async def _record_error(
        self,
        conversation_id: str,
        *,
        detail: str,
        explanation: str,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> bool:
        events: list[Event] = [
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=explanation),
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=explanation),
            ),
            LifecycleCommandService.build_status(ConversationStatus.ERROR, detail=detail),
        ]
        stored = await self._lifecycle_commands.append_task_status_if_current(
            conversation_id,
            cast(StatusEvent, events[-1]),
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
            prefix_events=events[:-1],
        )
        return stored is not None

    async def _conversation_setup(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        coalesced: bool,
        owner_id: str,
    ) -> _Setup:
        conversation_id = f"conv_{uuid.uuid4().hex}"
        self._store.create_conversation(
            conversation_id,
            owner_id=owner_id,
            surface="agent",
            title=f"Workflow schedule {spec.instance_id}",
        )
        self._settings.set_surface(conversation_id, "agent")
        self._settings.set_autonomous(conversation_id, True)
        fired_at = datetime.now(UTC)
        workflow_store = JsonDirWorkflowStore(
            self._project_access._project_store_now().root or "",
            owner_id=owner_id,
        )
        try:
            instance = workflow_store.get_instance(spec.instance_id)
        except ValueError as exc:
            return await self._setup_failure(
                schedule_id=schedule_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_instance_invalid_id",
                explanation=f"Workflow schedule {schedule_id} could not run: {exc}",
                error=str(exc),
            )
        if instance is None:
            message = (
                f"Workflow schedule {schedule_id} could not run: instance "
                f"{spec.instance_id!r} was not found."
            )
            return await self._setup_failure(
                schedule_id=schedule_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_instance_not_found",
                explanation=message,
                error=message,
            )
        if instance.definition_digest != spec.instance_digest:
            message = (
                f"Workflow schedule {schedule_id} could not run: pinned digest "
                f"{spec.instance_digest!r} does not match current instance digest "
                f"{instance.definition_digest!r}."
            )
            return await self._setup_failure(
                schedule_id=schedule_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_instance_digest_mismatch",
                explanation=message,
                error=message,
            )
        if not instance.enabled or instance.approval is None:
            message = (
                f"Workflow schedule {schedule_id} could not run: instance "
                f"{spec.instance_id!r} is not enabled and approved."
            )
            return await self._setup_failure(
                schedule_id=schedule_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail=(
                    "workflow_instance_not_enabled"
                    if not instance.enabled
                    else "workflow_instance_not_approved"
                ),
                explanation=message,
                error=message,
            )
        output_path = _render_output_path(
            instance.definition.output_contract.path_template,
            instance.params,
        )
        return _Setup(
            conversation_id=conversation_id,
            fired_at=fired_at,
            workflow_run=WorkflowRun(
                run_id=f"wfrun_{uuid.uuid4().hex}",
                definition=instance.definition,
                params=instance.params,
            ),
            output_path=output_path,
            message=_schedule_message(schedule_id, spec, output_path, instance.params),
        )

    async def _setup_failure(
        self,
        *,
        schedule_id: str,
        conversation_id: str,
        fired_at: datetime,
        coalesced: bool,
        detail: str,
        explanation: str,
        error: str,
    ) -> _Setup:
        await self._record_error(
            conversation_id,
            detail=detail,
            explanation=explanation,
        )
        return _Setup(
            conversation_id=conversation_id,
            fired_at=fired_at,
            workflow_run=None,
            output_path="",
            message=None,
            failure=_failed_record(
                schedule_id=schedule_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                output_path="",
                coalesced=coalesced,
                error=error,
            ),
        )

    async def _record_history(
        self,
        *,
        schedule_id: str,
        setup: _Setup,
        state: ConversationState,
        coalesced: bool,
    ) -> WorkflowScheduleRunRecord:
        authoritative = await self._store.get_state(setup.conversation_id)
        events = await self._store.get_events(setup.conversation_id)
        output_path, verify_verdict = _history_fields(
            events,
            fallback_output_path=setup.output_path,
        )
        terminal_state = authoritative.execution_status or state.execution_status
        return WorkflowScheduleRunRecord(
            schedule_id=schedule_id,
            run_cid=setup.conversation_id,
            fired_at=setup.fired_at,
            terminal_state=terminal_state.value,
            output_path=output_path,
            verify_verdict=verify_verdict,
            coalesced=coalesced,
        )
