"""Typed factories for Build-like executors and conversation loops."""

# The mixin is intentionally attached to a concrete factory in another module.
# Its collaborators are therefore typed by the consuming owner, not here.
# pyright: reportAttributeAccessIssue=false, reportArgumentType=false

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    WorkflowInvocationEvent,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    OperatingMode,
)
from disco.core.loop import signals
from disco.core.loop.resource_receipts import (
    canonical_workspace_identifier,
    read_receipt_records,
    rendered_content_matches_receipt,
)
from disco.core.workflow import WorkflowHandoff, WorkflowRun
from disco.tools import (
    AppKitToolExecutor,
    CapabilityBroker,
    DefaultToolExecutor,
    SandboxInstance,
    SandboxSession,
    ScopedPhaseExecutor,
    ToolScope,
    build_default_registry,
    workflow_effective_scope,
)
from disco.tools.builtin.reference_packs import ReferenceInspectTool
from disco.tools.builtin.workflow_dynamic import dynamic_workflow_tool
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore, workflow_router_tools

from .build_loop_components import (
    ComposeContext,
    LoopContractPort,
    LoopEventStorePort,
    LoopProjectStorePort,
    LoopSettingsPort,
    LoopWorkspacePort,
    McpLoopSnapshot,
    _build_appkit_registry,
    _sync_appkit_live_preview,
)
from .lifecycle_command_service import LifecycleCommandService
from .reference_pack_runtime import ReferencePackRuntime
from .run_registry import LoopRegistry
from .security_live_verifier import make_security_live_verifier
from .workflow_events import handle_workflow_tool_event
from .workflow_invocation import WorkflowInvocationService, WorkflowToolAdapter

logger = logging.getLogger(__name__)

ProviderCompletion = Callable[[CompletionRequest], Awaitable[CompletionResponse]]

_BUILD_LIKE_SURFACES = frozenset({"build", "agent"})
_ACTIVE_WORKFLOW_STATUSES = frozenset({"queued", "running", "needs_input"})
_QUIET_WORKFLOW_TARGET_STATUSES = frozenset(
    {
        ConversationStatus.IDLE,
        ConversationStatus.PAUSED,
        ConversationStatus.FINISHED,
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
    }
)


def _grounded_path_for_record(
    record: Any, actions: dict[str, Any], observations: dict[str, Any]
) -> str | None:
    action = actions.get(record.action_id)
    observation = observations.get(record.call_id)
    path = action.tool_call.arguments.get("path") if action is not None else None
    valid = (
        action is not None
        and action.tool_call.tool_name == "file_read"
        and observation is not None
        and isinstance(path, str)
        and record.receipt.complete
        and rendered_content_matches_receipt(record.receipt, observation.tool_result.content)
        and record.receipt.coverage.covers_total()
    )
    return canonical_workspace_identifier(path) if valid else None


def _grounded_reference_paths(events: list[Any]) -> set[str]:
    successful = signals.successful_action_ids(events)
    actions = {
        event.id: event
        for event in events
        if isinstance(event, ActionEvent)
        and event.source == EventSource.AGENT
        and event.id in successful
    }
    observations = {
        event.tool_result.call_id: event
        for event in events
        if isinstance(event, ObservationEvent) and event.source == EventSource.ENVIRONMENT
    }
    observed: set[str] = set()
    for record in read_receipt_records(events):
        path = _grounded_path_for_record(record, actions, observations)
        if path is not None:
            observed.add(path)
    return observed


def _reference_plan_guard_check(
    runtime: ReferencePackRuntime,
    owner_id: str,
    conversation_id: str,
    events: list[Any],
) -> str | None:
    required = runtime.required_pack_md_paths(owner_id, conversation_id)
    if not required:
        return None
    missing = tuple(path for path in required if path not in _grounded_reference_paths(events))
    if not missing:
        return None
    listed = ", ".join(f"`{path}`" for path in missing)
    return (
        "REFUSED: the plan was not accepted because its selected Reference "
        f"Packs have not been grounded. Read {listed} completely with "
        "`file_read`, then resubmit the same plan using those references."
    )


class _BuildExecutorWorkflowMixin:
    def _dynamic_workflow_tools(
        self,
        conversation_id: str,
        *,
        snapshot: McpLoopSnapshot | None,
    ) -> list[Any]:
        root = self._project.current_project_store().root
        if not root:
            return []
        owner_id = self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID
        if snapshot is None:
            return []
        service = self._workflow_service(conversation_id, snapshot)
        if service is None:
            return []
        result: list[Any] = []
        for descriptor in service.ready_descriptors(owner_id=owner_id):
            adapter = WorkflowToolAdapter(service, descriptor, owner_id=owner_id)

            async def invoke(
                params: dict[str, Any],
                _ctx: Any,
                *,
                _adapter: WorkflowToolAdapter = adapter,
                _descriptor: Any = descriptor,
            ) -> Any:
                async def start(handoff: WorkflowHandoff) -> str | None:
                    return await self._start_workflow_handoff(conversation_id, handoff)

                outcome = await _adapter.invoke(params, start=start)
                from disco.tools import ToolOutcome

                if not outcome.started:
                    details = ", ".join(
                        f"{issue.field}: {issue.message}" for issue in outcome.parameter_issues
                    )
                    return ToolOutcome(
                        success=False,
                        content=outcome.reason or "workflow invocation was rejected",
                        error=details or outcome.reason,
                    )
                return ToolOutcome(
                    success=True,
                    content=(
                        f"Workflow {_descriptor.instance_id!r} accepted. "
                        "The host is handing this turn to its sealed workflow run."
                    ),
                    structured={
                        "workflow_instance_id": _descriptor.instance_id,
                        "run_id": outcome.run_id,
                        "conversation_id": outcome.conversation_id,
                        "handoff": True,
                    },
                )

            result.append(dynamic_workflow_tool(descriptor, invoke))
        return result

    def _start_workflow_handoff(
        self,
        conversation_id: str,
        handoff: WorkflowHandoff,
        *,
        require_active_loop: bool = True,
    ) -> Awaitable[str | None]:
        async def start() -> str | None:
            # Resolve every fallible value and the active-loop authority before
            # the first write.  The three durable facts then land atomically,
            # so restart can never observe a queued workflow without its brief
            # and explicit handoff boundary.
            rendered_brief = handoff.brief.render()
            loop = self._loops.loop(conversation_id)
            if require_active_loop and loop is None:
                raise RuntimeError("workflow handoff has no active Agent loop")
            invocation = WorkflowInvocationEvent(
                state=handoff.state,
                status=handoff.state.status,
            )
            # One existing per-conversation lock serializes clicked Run and
            # dynamic Agent-tool entry. Re-check durable state under that lock
            # so two callers cannot both append competing pinned workflows.
            status = LifecycleCommandService.build_status(
                ConversationStatus.PAUSED,
                detail="workflow_handoff",
            )
            async with self._workspace.fence(conversation_id):
                # The local lock orders callers in one worker; the process
                # fence makes the read+append compare-and-set atomic across
                # workers sharing the same workspace.
                events = await self._event_store.get_events(conversation_id)
                latest = next(
                    (
                        event
                        for event in reversed(events)
                        if isinstance(event, WorkflowInvocationEvent)
                    ),
                    None,
                )
                if latest is not None and latest.status in _ACTIVE_WORKFLOW_STATUSES:
                    return None
                if not require_active_loop:
                    state = await self._event_store.get_state(conversation_id)
                    if state.execution_status not in _QUIET_WORKFLOW_TARGET_STATUSES:
                        return None
                await self._workspace.append_transition_batch_locked(
                    conversation_id,
                    [
                        invocation,
                        MessageEvent(
                            source=EventSource.ENVIRONMENT,
                            message=LLMMessage(role="user", content=rendered_brief),
                        ),
                        status,
                    ],
                    bind_current=True,
                )
            # The durable PAUSED event is the boundary authority.  Clear any
            # cooperative flag so the next step does not append a second,
            # discriminator-free PAUSED event over it.
            if loop is not None:
                loop._pause_requested.clear()
            return conversation_id

        return start()

    def _common_kwargs(
        self,
        conversation_id: str,
        session: SandboxSession,
        broker: CapabilityBroker,
        context: ComposeContext,
        provider_completion: ProviderCompletion | None,
        *,
        enable_reference_packs: bool,
    ) -> dict[str, Any]:
        async def execution_admission(agent_view_id: str | None) -> str | None:
            events = agent_view_consistent_events(
                await self._event_store.get_events(conversation_id)
            )
            if latest_workspace_run_intent(events) is None:
                return None
            if (
                agent_view_id is not None
                and current_workspace_agent_view_id(events) == agent_view_id
            ):
                return None
            return (
                "A newer user instruction or model view superseded this action before "
                "execution. The action was not run; continue from the latest instruction."
            )

        policy = context.policy
        return {
            "sandbox": cast(SandboxInstance, session),
            "broker": broker,
            "conversation_id": conversation_id,
            "model_policy": policy.model_policy,
            "driver_llm": self._settings._effective_driver_endpoint(conversation_id),
            "provider_completion": provider_completion,
            "source_report": self._source_reports.get(conversation_id),
            "read_char_budget": policy.read_char_budget,
            "scope_guard": policy.scope_guard,
            "on_tool_success": policy.on_tool_success,
            "starter_kit": self._contract._starter_kit_for(conversation_id),
            "workflow_events": (
                self._workflow_event_sink(conversation_id)
                if context.modes.workflow_router_mode
                else None
            ),
            "primitive_live_verifier": make_security_live_verifier(),
            "workspace_lock": self._workspace.lock(conversation_id),
            "workspace_fence": (
                lambda: self._workspace.interprocess_mutation_fence(conversation_id)
            ),
            "execution_admission": execution_admission,
            "release_intent_writer": self._project.write_release_intent,
            "prepare_for_events": (
                self._reference_prepare(conversation_id, session)
                if enable_reference_packs
                else None
            ),
            "plan_submission_guard": (
                self._reference_plan_guard(conversation_id) if enable_reference_packs else None
            ),
        }


class BuildExecutorFactory(_BuildExecutorWorkflowMixin):
    """Construct a scoped executor without owning its lifecycle."""

    def __init__(
        self,
        settings: LoopSettingsPort,
        workspace: LoopWorkspacePort,
        project: LoopProjectStorePort,
        contract: LoopContractPort,
        loops: LoopRegistry,
        event_store: LoopEventStorePort,
        reference_packs: ReferencePackRuntime | None = None,
        workflow_surface_digest: Callable[[Any], str] | None = None,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._project = project
        self._contract = contract
        self._loops = loops
        self._event_store = event_store
        self._reference_packs = reference_packs
        # The route review surface is the authority for approval digests.  Keep
        # this callback injectable so composition and tests can use the exact
        # same compiled surface without making the tool layer import routes.
        self._workflow_surface_digest = workflow_surface_digest or (
            lambda instance: (
                instance.approval.surface_shown_digest if instance.approval is not None else ""
            )
        )
        # Host-only, bounded report projections for direct artifact jobs. The
        # typed source remains owned by the handoff service; this cache carries
        # only the serialized prompt projection into the executor context.
        self._source_reports: dict[str, str] = {}

    def set_source_report(self, conversation_id: str, source_report: str) -> None:
        self._source_reports[conversation_id] = source_report

    def clear_source_report(self, conversation_id: str) -> None:
        self._source_reports.pop(conversation_id, None)

    def create(
        self,
        conversation_id: str,
        session: SandboxSession,
        broker: CapabilityBroker,
        context: ComposeContext,
        sealed_workflow_run: WorkflowRun | None,
        provider_completion: ProviderCompletion | None = None,
        workflow_snapshot: McpLoopSnapshot | None = None,
    ) -> DefaultToolExecutor:
        modes = context.modes
        common = self._common_kwargs(
            conversation_id,
            session,
            broker,
            context,
            provider_completion,
            enable_reference_packs=(sealed_workflow_run is None and not modes.workflow_router_mode),
        )
        if modes.appkit_mode:
            return self._appkit_executor(conversation_id, session, context, common)
        if modes.workflow_router_mode or sealed_workflow_run is not None:
            return self._workflow_executor(conversation_id, context, common)
        if self._settings._surface_of(conversation_id) == "agent":
            return self._agent_executor_with_workflows(
                conversation_id,
                session,
                context,
                common,
                workflow_snapshot=workflow_snapshot,
            )
        return DefaultToolExecutor(
            self._registry_with_reference_tools(
                build_default_registry(),
                conversation_id,
                session,
                include_create=False,
            ),
            context.policy.scope,
            **common,
        )

    def _agent_executor_with_workflows(
        self,
        conversation_id: str,
        session: SandboxSession,
        context: ComposeContext,
        common: dict[str, Any],
        *,
        workflow_snapshot: McpLoopSnapshot | None,
    ) -> DefaultToolExecutor:
        """Compose the ordinary Agent catalog plus only Ready workflows.

        Workflow tools are capabilities, not a router protocol: no list/enter
        turn is necessary and the dynamic schema is derived from the approved
        definition.  The host callback below records the immutable handoff and
        cooperatively pauses the broad Agent loop before a fresh sealed loop is
        composed by the parent runtime.
        """
        registry = self._registry_with_reference_tools(
            build_default_registry(),
            conversation_id,
            session,
            include_create=True,
        )
        dynamic = self._dynamic_workflow_tools(conversation_id, snapshot=workflow_snapshot)
        for tool in dynamic:
            registry.register(tool)
        if not dynamic:
            return DefaultToolExecutor(registry, context.policy.scope, **common)
        dynamic_names = frozenset(tool.definition.name for tool in dynamic)
        scope = context.policy.scope.model_copy(
            update={
                "allowed_tools": context.policy.scope.allowed_tools | dynamic_names,
                "advertised_tools": (
                    None
                    if context.policy.scope.advertised_tools is None
                    else context.policy.scope.advertised_tools | dynamic_names
                ),
            }
        )
        return DefaultToolExecutor(registry, scope, **common)

    def workflow_projection(
        self,
        conversation_id: str,
        state: Any,
        snapshot: McpLoopSnapshot,
    ) -> Any:
        service = self._workflow_service(conversation_id, snapshot)
        if service is None:
            return None
        return service.project(state)

    def _workflow_service(
        self,
        conversation_id: str,
        snapshot: McpLoopSnapshot,
    ) -> WorkflowInvocationService | None:
        root = self._project.current_project_store().root
        if not root:
            return None
        owner_id = self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID
        return self._workflow_service_for_owner(owner_id, snapshot)

    def _workflow_service_for_owner(
        self,
        owner_id: str,
        snapshot: McpLoopSnapshot,
    ) -> WorkflowInvocationService | None:
        root = self._project.current_project_store().root
        if not root:
            return None
        return WorkflowInvocationService(
            JsonDirWorkflowStore(root, owner_id=owner_id),
            mcp_tool_names_getter=lambda: frozenset(tool.name for tool in snapshot.tools),
            surface_digest_getter=self._workflow_surface_digest,
            connector_ids_getter=lambda: snapshot.connector_ids,
        )

    def _registry_with_reference_tools(
        self,
        registry: Any,
        conversation_id: str,
        session: SandboxSession,
        *,
        include_create: bool,
    ) -> Any:
        runtime = self._reference_packs
        if runtime is None:
            return registry
        if include_create:
            registry.register(runtime.create_agent_tool(session))
        owner_id = self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID
        if runtime.bindings.get(owner_id, conversation_id) is not None:
            registry.register(ReferenceInspectTool(inspector=runtime.reference_inspect_callback()))
        return registry

    def _reference_prepare(
        self,
        conversation_id: str,
        session: SandboxSession,
    ) -> Callable[[list[Any]], Awaitable[None]] | None:
        runtime = self._reference_packs
        if runtime is None:
            return None
        owner_id = self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID

        async def prepare(_events: list[Any]) -> None:
            binding = runtime.bindings.get(owner_id, conversation_id)
            if binding is None:
                return
            # A sandbox can be recreated without changing the host run
            # generation. Re-check the idempotent pinned tree before every
            # model turn so the current workspace cannot miss PACK.md.
            await runtime.materialize_before_model(owner_id, conversation_id, session)

        return prepare

    def _reference_plan_guard(
        self,
        conversation_id: str,
    ) -> Callable[[list[Any]], str | None] | None:
        runtime = self._reference_packs
        if runtime is None:
            return None
        owner_id = self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID

        def guard(events: list[Any]) -> str | None:
            return _reference_plan_guard_check(runtime, owner_id, conversation_id, events)

        return guard

    def _workflow_event_sink(self, conversation_id: str) -> Any:
        async def sink(kind: str, payload: dict[str, Any]) -> None:
            await handle_workflow_tool_event(
                loop=self._loops.loop(conversation_id),
                kind=kind,
                payload=payload,
            )

        return sink

    def _appkit_executor(
        self,
        conversation_id: str,
        session: SandboxSession,
        context: ComposeContext,
        common: dict[str, Any],
    ) -> AppKitToolExecutor:
        modes = context.modes
        assert modes.appkit_phase is not None
        registry = self._registry_with_reference_tools(
            _build_appkit_registry(),
            conversation_id,
            session,
            include_create=False,
        )
        return AppKitToolExecutor(
            registry,
            context.policy.scope,
            appkit_phase=modes.appkit_phase,
            base_scope=context.policy.scope,
            autonomous=modes.appkit_autonomous,
            mode_getter=lambda: (
                current.mode
                if (current := self._loops.loop(conversation_id)) is not None
                else OperatingMode.PLANNING
            ),
            on_preview_sync=lambda: _sync_appkit_live_preview(session),
            **common,
        )

    def _workflow_executor(
        self,
        conversation_id: str,
        context: ComposeContext,
        common: dict[str, Any],
    ) -> ScopedPhaseExecutor:
        phase = context.modes.workflow_phase
        assert phase is not None
        registry = build_default_registry()
        workflow_store = JsonDirWorkflowStore(
            self._project.current_project_store().root or "",
            owner_id=(
                self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID
            ),
        )

        def mcp_tool_names() -> frozenset[str]:
            return frozenset(name for name in registry.names() if name.startswith("mcp__"))

        if context.modes.workflow_router_mode:
            for tool in workflow_router_tools(
                store=workflow_store,
                phase_state=phase,
                mcp_tool_names_getter=mcp_tool_names,
            ):
                registry.register(tool)
        executor: ScopedPhaseExecutor | None = None

        def scope_resolver() -> ToolScope:
            assert executor is not None
            return workflow_effective_scope(
                phase=phase.phase,
                compiled_run_scope=phase.compiled_run_scope,
                base_scope=executor.widened_scope,
                output_path_template=phase.output_path_template,
                allow_abort=context.modes.workflow_router_mode,
            )

        executor = ScopedPhaseExecutor(
            registry,
            context.policy.scope,
            scope_resolver=scope_resolver,
            **common,
        )
        return executor
