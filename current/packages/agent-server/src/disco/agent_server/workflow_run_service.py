from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable, Mapping
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
from disco.core.loop import AgentLoop
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import (
    PinnedWorkflowInvocationState,
    ScheduleSpec,
    WorkflowRun,
    WorkflowRunBrief,
)
from disco.tools import DefaultToolExecutor
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore

from ._workflow_run_execution import _WorkflowRunExecution
from .lifecycle_command_service import LifecycleCommandService
from .schedule_service import (
    WorkflowConversationSettings,
    WorkflowLoopFactory,
    WorkflowModelAccess,
    WorkflowProjectAccess,
    WorkflowRunControl,
)
from .workflow_schedule import WorkflowScheduleRunRecord
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
    invocation_state: PinnedWorkflowInvocationState | None = None
    failure: WorkflowScheduleRunRecord | None = None


@dataclass(frozen=True)
class _ExecutionPlan:
    schedule_id: str
    spec: ScheduleSpec
    conversation_id: str
    fired_at: datetime
    output_path: str
    message: MessageEvent
    invocation_state: PinnedWorkflowInvocationState | None
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
    state: PinnedWorkflowInvocationState,
    brief: WorkflowRunBrief,
) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(
            role="user",
            content=(
                "Run this sealed workflow schedule.\n"
                f"Schedule id: {schedule_id}\n"
                f"Workflow instance: {state.instance_id}\n"
                f"Definition digest: {state.definition_digest}\n"
                f"Surface digest: {state.surface_digest}\n\n"
                f"{brief.render()}"
            ),
        ),
    )


def _failure_factory(
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
        prepare_workflow: Callable[[str, Mapping[str, Any], str], Any],
    ) -> None:
        self._store, self._lifecycle_commands = store, lifecycle_commands
        self._project_access, self._settings = project_access, settings
        self._prepare_workflow = prepare_workflow
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
        assert (
            setup.workflow_run is not None
            and setup.message is not None
            and setup.invocation_state is not None
        )
        result = await self._execution.execute(
            schedule_id=schedule_id,
            spec=spec,
            conversation_id=setup.conversation_id,
            fired_at=setup.fired_at,
            workflow_run=setup.workflow_run,
            output_path=setup.output_path,
            message=setup.message,
            invocation_state=setup.invocation_state,
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

    def _ensure_conversation(self, conversation_id: str, owner_id: str, instance_id: str) -> None:
        self._store.create_conversation(
            conversation_id,
            owner_id=owner_id,
            surface="agent",
            title=f"Workflow schedule {instance_id}",
        )
        self._settings.set_surface(conversation_id, "agent")
        self._settings.set_autonomous(conversation_id, True)

    async def _setup_failure_for(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str,
        conversation_id: str,
        fired_at: datetime,
        coalesced: bool,
        detail: str,
        message: str,
    ) -> _Setup:
        self._ensure_conversation(conversation_id, owner_id, spec.instance_id)
        return await self._setup_failure(
            schedule_id=schedule_id,
            conversation_id=conversation_id,
            fired_at=fired_at,
            coalesced=coalesced,
            detail=detail,
            explanation=message,
            error=message,
        )

    @staticmethod
    def _digest_mismatch_message(
        schedule_id: str, spec: ScheduleSpec, instance: Any
    ) -> str:
        return (
            f"Workflow schedule {schedule_id} could not run: pinned digest "
            f"{spec.instance_digest!r} does not match current instance digest "
            f"{instance.definition_digest!r}."
        )

    async def _conversation_setup(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        coalesced: bool,
        owner_id: str,
    ) -> _Setup:
        conversation_id = f"conv_{uuid.uuid4().hex}"
        fired_at = datetime.now(UTC)

        workflow_store = JsonDirWorkflowStore(
            self._project_access._project_store_now().root or "",
            owner_id=owner_id,
        )
        try:
            instance = workflow_store.get_instance(spec.instance_id)
        except ValueError as exc:
            return await self._setup_failure_for(
                schedule_id=schedule_id,
                spec=spec,
                owner_id=owner_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_instance_invalid_id",
                message=f"Workflow schedule {schedule_id} could not run: {exc}",
            )
        if instance is None:
            message = (
                f"Workflow schedule {schedule_id} could not run: instance "
                f"{spec.instance_id!r} was not found."
            )
            return await self._setup_failure_for(
                schedule_id=schedule_id,
                spec=spec,
                owner_id=owner_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_instance_not_found",
                message=message,
            )
        if instance.definition_digest != spec.instance_digest:
            message = self._digest_mismatch_message(schedule_id, spec, instance)
            return await self._setup_failure_for(
                schedule_id=schedule_id,
                spec=spec,
                owner_id=owner_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_instance_digest_mismatch",
                message=message,
            )
        # Reuse the same invocation service used by clicked Run.  This is the
        # fire-time TOCTOU gate: the exact pinned params, current definition,
        # approval, MCP surface, and surface digest must still be valid.
        prepared = self._prepare_workflow(
            spec.instance_id,
            spec.params,
            owner_id,
        )
        if not prepared.accepted:
            issues = ", ".join(issue.message for issue in prepared.parameter_issues)
            message = (
                f"Workflow schedule {schedule_id} could not run: "
                f"{prepared.reason or 'workflow is not ready'}" + (f" ({issues})" if issues else "")
            )
            return await self._setup_failure_for(
                schedule_id=schedule_id,
                spec=spec,
                owner_id=owner_id,
                conversation_id=conversation_id,
                fired_at=fired_at,
                coalesced=coalesced,
                detail="workflow_schedule_preflight_failed",
                message=message,
            )
        assert prepared.state is not None and prepared.brief is not None
        invocation_state = prepared.state.model_copy(update={"status": "running"})
        workflow_run = WorkflowRun(
            run_id=prepared.state.run_id,
            definition=prepared.state.definition_snapshot,
            params=prepared.state.validated_params,
        )
        output_path = _render_output_path(
            instance.definition.output_contract.path_template,
            prepared.state.validated_params,
        )
        self._ensure_conversation(conversation_id, owner_id, spec.instance_id)
        return _Setup(
            conversation_id=conversation_id,
            fired_at=fired_at,
            workflow_run=workflow_run,
            output_path=output_path,
            message=_schedule_message(
                schedule_id,
                invocation_state,
                prepared.brief,
            ),
            invocation_state=invocation_state,
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
