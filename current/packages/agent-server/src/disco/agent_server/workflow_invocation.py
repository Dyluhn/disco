"""Shared UI/Agent workflow invocation seam.

Callers supply the current owner, MCP snapshot, surface digest, and a host-owned
starter callback.  The service performs the same readiness, exact-input, and
TOCTOU checks for every caller before handing the parent a fresh sealed-run
handoff.  It never composes a loop or creates a conversation itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from disco.core.workflow import (
    PinnedWorkflowInvocationState,
    WorkflowHandoff,
    WorkflowInstance,
    WorkflowParameterIssue,
    WorkflowReadiness,
    WorkflowResumeProjection,
    WorkflowRunBrief,
    WorkflowToolDescriptor,
    evaluate_workflow_readiness,
    project_pinned_workflow_state,
    render_workflow_output_path,
    validate_workflow_params,
    workflow_run_brief,
    workflow_tool_descriptor,
)
from disco.tools.builtin.workflow_tools import WorkflowStore
from pydantic import BaseModel, ConfigDict


class WorkflowInvocationPreparation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    instance_id: str
    readiness: WorkflowReadiness
    parameter_issues: tuple[WorkflowParameterIssue, ...] = ()
    state: PinnedWorkflowInvocationState | None = None
    brief: WorkflowRunBrief | None = None
    handoff: WorkflowHandoff | None = None
    reason: str | None = None


class WorkflowInvocationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    started: bool
    instance_id: str
    run_id: str | None = None
    conversation_id: str | None = None
    readiness: WorkflowReadiness
    parameter_issues: tuple[WorkflowParameterIssue, ...] = ()
    state: PinnedWorkflowInvocationState | None = None
    brief: WorkflowRunBrief | None = None
    handoff: WorkflowHandoff | None = None
    reason: str | None = None


class WorkflowInvocationService:
    """One invocation implementation for clicked Run and dynamic Agent calls."""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        mcp_tool_names_getter: Callable[[], frozenset[str]],
        surface_digest_getter: Callable[[WorkflowInstance], str],
        connector_ids_getter: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._store = store
        self._mcp_tool_names_getter = mcp_tool_names_getter
        self._surface_digest_getter = surface_digest_getter
        self._connector_ids_getter = connector_ids_getter

    def descriptor(
        self,
        instance_id: str,
        *,
        owner_id: str,
    ) -> WorkflowToolDescriptor | None:
        instance = self._store.get_instance(instance_id)
        if instance is None:
            return None
        readiness = self._readiness(instance, owner_id=owner_id)
        if not readiness.ready:
            return None
        return workflow_tool_descriptor(instance_id, instance)

    def ready_descriptors(self, *, owner_id: str) -> tuple[WorkflowToolDescriptor, ...]:
        descriptors: list[WorkflowToolDescriptor] = []
        for row in self._store.list_instances():
            descriptor = self.descriptor(row.instance_id, owner_id=owner_id)
            if descriptor is not None:
                descriptors.append(descriptor)
        return tuple(descriptors)

    def readiness_for(self, instance_id: str, *, owner_id: str) -> WorkflowReadiness:
        """Return the same readiness verdict used by invocation and tools."""

        instance = self._store.get_instance(instance_id)
        if instance is None:
            return WorkflowReadiness(ready=False, reasons=("not_found",))
        return self._readiness(instance, owner_id=owner_id)

    def project(self, state: PinnedWorkflowInvocationState) -> WorkflowResumeProjection:
        """Reconstruct a persisted seal against current resources and digest."""

        current = self._store.get_instance(state.instance_id)
        return project_pinned_workflow_state(
            state,
            available_mcp_tool_names=self._mcp_tool_names_getter(),
            available_connector_bindings=(
                self._connector_ids_getter() if self._connector_ids_getter else None
            ),
            current_surface_digest=(
                self._surface_digest_getter(current) if current is not None else None
            ),
            current_instance=current,
            require_current_instance=True,
        )

    def prepare(
        self,
        instance_id: str,
        *,
        params: Mapping[str, Any],
        owner_id: str,
    ) -> WorkflowInvocationPreparation:
        instance = self._store.get_instance(instance_id)
        if instance is None:
            return WorkflowInvocationPreparation(
                accepted=False,
                instance_id=instance_id,
                readiness=WorkflowReadiness(ready=False, reasons=("not_found",)),
                reason="workflow_not_found",
            )
        readiness = self._readiness(instance, owner_id=owner_id)
        if not readiness.ready:
            return WorkflowInvocationPreparation(
                accepted=False,
                instance_id=instance_id,
                readiness=readiness,
                reason="workflow_not_ready",
            )
        validation = validate_workflow_params(instance.definition.params_model_schema, params)
        if not validation.valid:
            return WorkflowInvocationPreparation(
                accepted=False,
                instance_id=instance_id,
                readiness=readiness,
                parameter_issues=validation.issues,
                reason="invalid_parameters",
            )
        # The store is read again after validation.  This is the TOCTOU gate: a
        # changed definition, approval, or surface cannot be silently pinned.
        current = self._store.get_instance(instance_id)
        if current is None:
            return self._failed_toctou(instance_id, readiness, "workflow_removed_before_start")
        current_readiness = self._readiness(current, owner_id=owner_id)
        surface_digest = self._surface_digest_getter(current)
        surface_changed = (
            current.approval is None or current.approval.surface_shown_digest != surface_digest
        )
        if (
            current.definition_digest != instance.definition_digest
            or current.definition.digest() != instance.definition_digest
            or not current_readiness.ready
            or surface_changed
        ):
            return self._failed_toctou(
                instance_id, current_readiness, "workflow_changed_before_start"
            )
        try:
            render_workflow_output_path(
                current.definition.output_contract.path_template,
                validation.params,
            )
        except ValueError as exc:
            return WorkflowInvocationPreparation(
                accepted=False,
                instance_id=instance_id,
                readiness=current_readiness,
                parameter_issues=(
                    WorkflowParameterIssue(
                        field="$",
                        code="output_path",
                        message=str(exc),
                    ),
                ),
                reason="invalid_parameters",
            )
        state = PinnedWorkflowInvocationState.create(
            instance_id=instance_id,
            instance=current,
            params=validation.params,
            surface_digest=surface_digest,
        )
        brief = workflow_run_brief(current.definition, validation.params)
        try:
            brief.render()
        except ValueError as exc:
            return WorkflowInvocationPreparation(
                accepted=False,
                instance_id=instance_id,
                readiness=current_readiness,
                parameter_issues=(
                    WorkflowParameterIssue(
                        field="$",
                        code="brief_size",
                        message=str(exc),
                    ),
                ),
                reason="invalid_parameters",
            )
        handoff = WorkflowHandoff(
            run_id=state.run_id,
            instance_id=instance_id,
            state=state,
            workflow_run=state_to_run(state),
            brief=brief,
        )
        return WorkflowInvocationPreparation(
            accepted=True,
            instance_id=instance_id,
            readiness=current_readiness,
            state=state,
            brief=brief,
            handoff=handoff,
        )

    async def invoke(
        self,
        instance_id: str,
        *,
        params: Mapping[str, Any],
        owner_id: str,
        start: Callable[[WorkflowHandoff], Awaitable[str | None]],
    ) -> WorkflowInvocationResult:
        prepared = self.prepare(instance_id, params=params, owner_id=owner_id)
        if not prepared.accepted or prepared.handoff is None or prepared.state is None:
            return WorkflowInvocationResult(
                started=False,
                instance_id=instance_id,
                readiness=prepared.readiness,
                parameter_issues=prepared.parameter_issues,
                reason=prepared.reason,
            )
        conversation_id = await start(prepared.handoff)
        if conversation_id is None:
            return WorkflowInvocationResult(
                started=False,
                instance_id=instance_id,
                readiness=prepared.readiness,
                parameter_issues=prepared.parameter_issues,
                reason="workflow_start_conflict",
            )
        return WorkflowInvocationResult(
            started=True,
            instance_id=instance_id,
            run_id=prepared.state.run_id,
            conversation_id=conversation_id,
            readiness=prepared.readiness,
            state=prepared.state,
            brief=prepared.brief,
            handoff=prepared.handoff,
        )

    def _readiness(self, instance: WorkflowInstance, *, owner_id: str) -> WorkflowReadiness:
        return evaluate_workflow_readiness(
            instance,
            owner_id=owner_id,
            current_surface_digest=self._surface_digest_getter(instance),
            available_mcp_tool_names=self._mcp_tool_names_getter(),
            available_connector_bindings=(
                self._connector_ids_getter() if self._connector_ids_getter else None
            ),
        )

    @staticmethod
    def _failed_toctou(
        instance_id: str, readiness: WorkflowReadiness, reason: str
    ) -> WorkflowInvocationPreparation:
        return WorkflowInvocationPreparation(
            accepted=False,
            instance_id=instance_id,
            readiness=readiness,
            reason=reason,
        )


class WorkflowToolAdapter:
    """Small registration adapter for a dynamic descriptor.

    The parent registers ``descriptor`` with its ordinary Agent tool catalog and
    delegates calls to ``invoke``.  No list/read/router turn is required, and
    the adapter intentionally exposes no direct executor or sandbox handle.
    """

    def __init__(
        self,
        service: WorkflowInvocationService,
        descriptor: WorkflowToolDescriptor,
        *,
        owner_id: str,
    ) -> None:
        self.descriptor = descriptor
        self._service = service
        self._owner_id = owner_id

    async def invoke(
        self,
        params: Mapping[str, Any],
        *,
        start: Callable[[WorkflowHandoff], Awaitable[str | None]],
    ) -> WorkflowInvocationResult:
        return await self._service.invoke(
            self.descriptor.instance_id,
            params=params,
            owner_id=self._owner_id,
            start=start,
        )


def state_to_run(state: PinnedWorkflowInvocationState):
    from disco.core.workflow import WorkflowRun

    return WorkflowRun(
        run_id=state.run_id,
        definition=state.definition_snapshot,
        params=state.validated_params,
    )


__all__ = [
    "WorkflowInvocationPreparation",
    "WorkflowInvocationResult",
    "WorkflowInvocationService",
    "WorkflowToolAdapter",
    "state_to_run",
]
