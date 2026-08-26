"""Typed factories for Build-like executors and conversation loops."""

from __future__ import annotations

import contextlib
import hashlib
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
    NoOpCondenser,
    ObservationEvent,
    SecurityRisk,
    WorkflowInvocationEvent,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    DefaultLLMRouter,
    ModelExecutionPolicy,
    OperatingMode,
    RouterSummarizer,
)
from disco.core.loop import AgentLoop, BuildAgent, NeverConfirm, ResearchAgent, RouterAgent, signals
from disco.core.loop.resource_receipts import (
    canonical_workspace_identifier,
    read_receipt_records,
    rendered_content_matches_receipt,
)
from disco.core.security import RuleBasedAnalyzer
from disco.core.workflow import WorkflowHandoff, WorkflowRun, compile_workflow_scope
from disco.tools import (
    AppKitToolExecutor,
    Capability,
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

from .build_loop_assembler import BuildLoopAssembler
from .build_loop_components import (
    ComposeContext,
    LoopContractPort,
    LoopDeepResearchPort,
    LoopDriverPort,
    LoopEjectionPort,
    LoopEventStorePort,
    LoopMcpPort,
    LoopModeResolver,
    LoopProjectStorePort,
    LoopRehydrationPort,
    LoopRetrievalPort,
    LoopSandboxPort,
    LoopSettingsPort,
    LoopWorkspacePort,
    McpLoopSnapshot,
    _apply_mcp_scope,
    _bind_ejection,
    _build_appkit_registry,
    _mcp_base_risk_by_tool,
    _NoToolExecutor,
    _sync_appkit_live_preview,
)
from .driver_context import ResolvedDriverContext
from .driver_context_state import DriverContextState
from .lifecycle_command_service import LifecycleCommandService
from .reference_pack_runtime import ReferencePackRuntime
from .run_registry import LoopRegistry, RunResourceRegistry
from .security_live_verifier import make_security_live_verifier
from .vision_inspection import VisionInspectionCapability
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


def _workflow_resolution_needed(
    invocation: WorkflowInvocationEvent | None,
    loop: AgentLoop | None,
) -> bool:
    """Return whether this kick must pass through the sealed-run resolver.

    Active durable invocations always require resolution.  A terminal
    invocation requires one final transition only while the currently bound
    loop is still sealed; once a normal Agent loop is bound, historical
    workflow events no longer perturb later turns.
    """

    if invocation is None:
        return False
    if invocation.status in _ACTIVE_WORKFLOW_STATUSES:
        return True
    return loop is not None and getattr(loop, "_workflow_run", None) is not None


class BuildCapabilityBroker:
    """Create revocable Build capabilities over typed retrieval operations."""

    def __init__(self, retrieval: LoopRetrievalPort) -> None:
        self._retrieval = retrieval

    def build(
        self,
        *,
        router: DefaultLLMRouter | None = None,
        conversation_id: str | None = None,
    ) -> CapabilityBroker:
        broker = CapabilityBroker()
        broker.register("search", self._retrieval.capability_search)
        broker.register("extract", self._retrieval.capability_extract)
        if router is not None and conversation_id is not None:
            visual = VisionInspectionCapability(router, conversation_id=conversation_id)
            broker.register("visual_inspection", visual.inspect)
        return broker


class BuildSessionFactory:
    """Adopt an uploaded session or create one under the resolved sandbox spec."""

    def __init__(
        self,
        settings: LoopSettingsPort,
        sandbox: LoopSandboxPort,
        rehydration: LoopRehydrationPort,
        resources: RunResourceRegistry,
    ) -> None:
        self._settings = settings
        self._sandbox = sandbox
        self._rehydration = rehydration
        self._resources = resources

    def create(
        self,
        conversation_id: str,
        snapshot: McpLoopSnapshot,
        sealed_workflow_run: WorkflowRun | None,
    ) -> SandboxSession:
        session = self._resources.pop_pending_session(conversation_id)
        if session is not None:
            return session
        egress_surface = self._settings._surface_of(conversation_id)
        if self._settings._effective_artifact_mode(conversation_id):
            egress_surface = "build"
        sandbox_spec = self._sandbox._build_sandbox_spec(
            surface=egress_surface,
            mcp_egress_hosts=snapshot.egress_hosts,
        )
        if sealed_workflow_run is not None:
            sandbox_spec = sandbox_spec.model_copy(
                update={
                    "permitted": sandbox_spec.permitted - {Capability.NETWORK},
                    "egress_allow": frozenset(sealed_workflow_run.definition.policies.egress_allow),
                    "public_web": False,
                }
            )
        return SandboxSession(
            self._sandbox._sandbox_service_now(),
            sandbox_spec,
            conversation_id=conversation_id,
            on_recreate=lambda: self._rehydration._rehydrate_after_recreate(conversation_id),
            legacy_auto_preview=False,
        )

    def create_preview(
        self,
        conversation_id: str,
        snapshot: McpLoopSnapshot,
    ) -> SandboxSession:
        """Create an isolated session for immutable Preview replay.

        It intentionally does not enter the executor registry: the canonical
        Preview resource owns its lifetime, while the conversation session
        remains the mutable build workspace.
        """

        surface = self._settings._surface_of(conversation_id)
        sandbox_spec = self._sandbox._build_sandbox_spec(
            surface=surface,
            mcp_egress_hosts=snapshot.egress_hosts,
        )
        preview_id = "pv_" + hashlib.sha256(conversation_id.encode()).hexdigest()[:29]
        return SandboxSession(
            self._sandbox._sandbox_service_now(),
            sandbox_spec,
            conversation_id=preview_id,
            legacy_auto_preview=False,
        )


class McpLoopConfigurator:
    """Apply one immutable MCP snapshot and bind the AppKit ejection owner."""

    def __init__(
        self,
        event_store: LoopEventStorePort,
        ejection: LoopEjectionPort,
    ) -> None:
        self._event_store = event_store
        self._ejection = ejection

    def configure(
        self,
        conversation_id: str,
        executor: DefaultToolExecutor,
        context: ComposeContext,
        snapshot: McpLoopSnapshot,
        sealed_workflow_run: WorkflowRun | None,
    ) -> dict[str, SecurityRisk]:
        self._wire_tools(conversation_id, executor, context, snapshot)
        phase = context.modes.workflow_phase
        if sealed_workflow_run is not None:
            assert phase is not None
            phase.compiled_run_scope = compile_workflow_scope(
                sealed_workflow_run.definition,
                frozenset(tool.name for tool in snapshot.tools),
            )
        self._publish_approvals(conversation_id, snapshot)
        return _mcp_base_risk_by_tool(snapshot.tools)

    def _wire_tools(
        self,
        conversation_id: str,
        executor: DefaultToolExecutor,
        context: ComposeContext,
        snapshot: McpLoopSnapshot,
    ) -> None:
        modes = context.modes
        if snapshot.tools:
            if modes.appkit_mode and isinstance(executor, AppKitToolExecutor):
                executor.set_widen_callback(
                    lambda: _apply_mcp_scope(
                        executor,
                        snapshot.tools,
                        snapshot.call_target,
                        max_active_schemas=snapshot.max_active_schemas,
                    )
                )
            else:
                _apply_mcp_scope(
                    executor,
                    snapshot.tools,
                    snapshot.call_target,
                    max_active_schemas=snapshot.max_active_schemas,
                )
        if modes.appkit_mode and isinstance(executor, AppKitToolExecutor):
            _bind_ejection(
                self._ejection,
                conversation_id,
                executor,
                context.policy.scope,
                snapshot.tools,
                snapshot.call_target,
                snapshot.max_active_schemas,
            )

    def _publish_approvals(
        self,
        conversation_id: str,
        snapshot: McpLoopSnapshot,
    ) -> None:
        for notice in snapshot.approvals:
            try:
                self._event_store.publish_ephemeral(
                    conversation_id,
                    {
                        "type": "mcp_approval_required",
                        "server": notice.server,
                        "description_hash": notice.description_hash,
                        "old_description_hash": notice.old_description_hash,
                    },
                )
            except Exception:
                pass


class BuildExecutorFactory:
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
                instance.approval.surface_shown_digest
                if instance.approval is not None
                else ""
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
            enable_reference_packs=(
                sealed_workflow_run is None and not modes.workflow_router_mode
            ),
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
                self._reference_plan_guard(conversation_id)
                if enable_reference_packs
                else None
            ),
        }

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
            registry.register(
                ReferenceInspectTool(inspector=runtime.reference_inspect_callback())
            )
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
            required = runtime.required_pack_md_paths(owner_id, conversation_id)
            if not required:
                return None
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
                if isinstance(event, ObservationEvent)
                and event.source == EventSource.ENVIRONMENT
            }
            observed: set[str] = set()
            # A complete host-issued ObservationReceipt paired to the exact
            # successful file_read is the only grounding proof. A model-authored
            # observation or a no-range call whose result was truncated fails
            # closed here.
            for record in read_receipt_records(events):
                action = actions.get(record.action_id)
                if action is None or action.tool_call.tool_name != "file_read":
                    continue
                observation = observations.get(record.call_id)
                path = action.tool_call.arguments.get("path")
                if (
                    observation is None
                    or not isinstance(path, str)
                    or not record.receipt.complete
                    or not rendered_content_matches_receipt(
                        record.receipt, observation.tool_result.content
                    )
                ):
                    continue
                if not record.receipt.coverage.covers_total():
                    continue
                observed.add(canonical_workspace_identifier(path))
            missing = tuple(path for path in required if path not in observed)
            if not missing:
                return None
            listed = ", ".join(f"`{path}`" for path in missing)
            return (
                "REFUSED: the plan was not accepted because its selected Reference "
                f"Packs have not been grounded. Read {listed} completely with "
                "`file_read`, then resubmit the same plan using those references."
            )

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


class BuildLoopComposer:
    """Coordinate bounded factories for one Build-like loop."""

    def __init__(
        self,
        sessions: BuildSessionFactory,
        modes: LoopModeResolver,
        brokers: BuildCapabilityBroker,
        executors: BuildExecutorFactory,
        mcp: LoopMcpPort,
        mcp_configurator: McpLoopConfigurator,
        resources: RunResourceRegistry,
        assembler: BuildLoopAssembler,
    ) -> None:
        self._sessions = sessions
        self._modes = modes
        self._brokers = brokers
        self._executors = executors
        self._mcp = mcp
        self._mcp_configurator = mcp_configurator
        self._resources = resources
        self._assembler = assembler

    def set_source_report(self, conversation_id: str, source_report: str) -> None:
        self._executors.set_source_report(conversation_id, source_report)

    def clear_source_report(self, conversation_id: str) -> None:
        self._executors.clear_source_report(conversation_id)

    def workflow_projection(
        self,
        conversation_id: str,
        state: Any,
        snapshot: McpLoopSnapshot,
    ) -> Any:
        return self._executors.workflow_projection(conversation_id, state, snapshot)

    def workflow_invocation_service(
        self,
        conversation_id: str,
    ) -> WorkflowInvocationService | None:
        """Return the shared invocation service over the current MCP surface."""

        return self._executors._workflow_service(
            conversation_id,
            self._mcp._loop_snapshot(),
        )

    def workflow_invocation_service_for_owner(
        self,
        owner_id: str,
    ) -> WorkflowInvocationService | None:
        """Preflight a clicked run before allocating its conversation."""

        return self._executors._workflow_service_for_owner(
            owner_id,
            self._mcp._loop_snapshot(),
        )

    def workflow_surface_digest(self, instance: Any) -> str:
        """Return the route-authoritative digest for one compiled workflow surface."""

        return self._executors._workflow_surface_digest(instance)

    async def start_workflow_handoff(
        self,
        conversation_id: str,
        handoff: WorkflowHandoff,
        *,
        require_active_loop: bool,
    ) -> str | None:
        """Persist one canonical workflow boundary for Agent or UI entry."""

        return await self._executors._start_workflow_handoff(
            conversation_id,
            handoff,
            require_active_loop=require_active_loop,
        )

    def build_broker(self) -> CapabilityBroker:
        return self._brokers.build()

    def create_preview_session(self, conversation_id: str) -> SandboxSession:
        """Compose the isolated sandbox owned by one sealed Preview resource."""

        return self._sessions.create_preview(
            conversation_id,
            self._mcp._loop_snapshot(),
        )

    def compose_build_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
    ) -> AgentLoop:
        snapshot = self._mcp._loop_snapshot()
        session = self._sessions.create(
            conversation_id,
            snapshot,
            sealed_workflow_run,
        )
        context = self._modes.resolve(
            conversation_id,
            driver_context_window,
            sealed_workflow_run,
            sealed_workflow_instance_id,
        )
        executor = self._executors.create(
            conversation_id,
            session,
            self._brokers.build(router=router, conversation_id=conversation_id),
            context,
            sealed_workflow_run,
            provider_completion=router.complete,
            workflow_snapshot=snapshot,
        )
        risks = self._mcp_configurator.configure(
            conversation_id,
            executor,
            context,
            snapshot,
            sealed_workflow_run,
        )
        self._resources.set_executor(conversation_id, executor)
        return self._assembler.assemble(
            conversation_id,
            router,
            agent,
            executor,
            context,
            sealed_workflow_run,
            risks,
        )


class BuildLoopFactory:
    """Route loop construction while registries retain all mutable ownership."""

    def __init__(
        self,
        settings: LoopSettingsPort,
        drivers: LoopDriverPort,
        workspace: LoopWorkspacePort,
        event_store: LoopEventStorePort,
        deep_research: LoopDeepResearchPort,
        composer: BuildLoopComposer,
        loops: LoopRegistry,
        resources: RunResourceRegistry,
        contexts: DriverContextState,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
    ) -> None:
        self._settings = settings
        self._drivers = drivers
        self._workspace = workspace
        self._event_store = event_store
        self._deep_research = deep_research
        self._composer = composer
        self._loops = loops
        self._resources = resources
        self._contexts = contexts
        self._mode = mode

    def set_source_report(self, conversation_id: str, source_report: str) -> None:
        self._composer.set_source_report(conversation_id, source_report)

    def clear_source_report(self, conversation_id: str) -> None:
        self._composer.clear_source_report(conversation_id)

    def build_broker(self) -> CapabilityBroker:
        return self._composer.build_broker()

    def create_preview_session(self, conversation_id: str) -> SandboxSession:
        """Return a Preview-owned sandbox without composing an executor."""

        return self._composer.create_preview_session(conversation_id)

    def workflow_invocation_service(
        self,
        conversation_id: str,
    ) -> WorkflowInvocationService | None:
        """Expose workflow invocation without leaking the composition graph."""

        return self._composer.workflow_invocation_service(conversation_id)

    def workflow_invocation_service_for_owner(
        self,
        owner_id: str,
    ) -> WorkflowInvocationService | None:
        return self._composer.workflow_invocation_service_for_owner(owner_id)

    def workflow_surface_digest(self, instance: Any) -> str:
        """Expose the same compiled-surface digest used by approval/readiness."""

        return self._composer.workflow_surface_digest(instance)

    async def start_workflow_handoff(
        self,
        conversation_id: str,
        handoff: WorkflowHandoff,
        *,
        require_active_loop: bool,
    ) -> str | None:
        return await self._composer.start_workflow_handoff(
            conversation_id,
            handoff,
            require_active_loop=require_active_loop,
        )

    def compose_build_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
    ) -> AgentLoop:
        return self._composer.compose_build_loop(
            conversation_id,
            router,
            agent,
            driver_context_window=driver_context_window,
            sealed_workflow_run=sealed_workflow_run,
            sealed_workflow_instance_id=sealed_workflow_instance_id,
        )

    async def loop_for_workflow_resolved(
        self,
        conversation_id: str,
        snapshot: ResolvedDriverContext,
    ) -> AgentLoop:
        """Resolve the durable workflow head before composing a loop.

        Workflow invocation is a host-owned mode switch.  The broad Agent
        executor is detached and killed before the sealed executor is created;
        no already-created sandbox is narrowed in place.  The same
        conversation/event log remains authoritative throughout.
        """
        events = await self._event_store.get_events(conversation_id)
        invocation = next(
            (event for event in reversed(events) if isinstance(event, WorkflowInvocationEvent)),
            None,
        )
        if invocation is None or invocation.status not in _ACTIVE_WORKFLOW_STATUSES:
            current_loop = self._loops.loop(conversation_id)
            if _workflow_resolution_needed(invocation, current_loop):
                await self._discard_for_recompose(conversation_id)
                return self.loop_for_resolved(conversation_id, snapshot, force_recompose=True)
            return self.loop_for_resolved(conversation_id, snapshot)

        mcp_snapshot = self._composer._mcp._loop_snapshot()
        projection = self._composer.workflow_projection(
            conversation_id,
            invocation.state,
            mcp_snapshot,
        )
        if projection is None or projection.fail_closed or projection.workflow_run is None:
            await self._mark_workflow_error(conversation_id, invocation)
            await self._discard_for_recompose(conversation_id)
            return self.loop_for_resolved(conversation_id, snapshot, force_recompose=True)

        state = invocation.state
        if state.status in {"queued", "needs_input"}:
            state = state.model_copy(update={"status": "running"})
            status = LifecycleCommandService.build_status(
                ConversationStatus.RUNNING,
                detail="workflow_started",
            )
            async with self._workspace.fence(conversation_id):
                await self._workspace.append_transition_batch_locked(
                    conversation_id,
                    [WorkflowInvocationEvent(state=state, status="running"), status],
                    bind_current=True,
                )

        await self._discard_for_recompose(conversation_id)
        self._contexts.begin_compose(conversation_id, snapshot)
        try:
            surface = self._settings._surface_of(conversation_id)
            router = self._drivers.router(
                pick=snapshot.model_key,
                surface=surface,
                autonomous=self._settings._effective_autonomous(conversation_id),
                conversation_id=conversation_id,
                appkit_mode=False,
            )
            agent = self._agent(
                surface,
                router,
                conversation_id,
                snapshot.model_key,
                snapshot.context_window,
            )
            loop = self._compose_surface(
                surface,
                conversation_id,
                router,
                agent,
                snapshot.context_window,
                sealed_workflow_run=projection.workflow_run,
                sealed_workflow_instance_id=invocation.state.instance_id,
            )
            loop.stream_sink = lambda frame: self._event_store.publish_ephemeral(
                conversation_id, frame
            )
            self._loops.bind(conversation_id, loop)
            self._contexts.bind_resolved(conversation_id, snapshot)
            return loop
        finally:
            self._contexts.end_compose(conversation_id, snapshot)

    async def workflow_resolution_required(self, conversation_id: str) -> bool:
        events = await self._event_store.get_events(conversation_id)
        invocation = next(
            (event for event in reversed(events) if isinstance(event, WorkflowInvocationEvent)),
            None,
        )
        return _workflow_resolution_needed(invocation, self._loops.loop(conversation_id))

    async def _discard_for_recompose(self, conversation_id: str) -> None:
        self._loops.pop(conversation_id)
        executor = self._resources.pop_executor(conversation_id)
        pending = self._resources.pop_pending_session(conversation_id)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()

    async def _mark_workflow_error(
        self,
        conversation_id: str,
        invocation: WorkflowInvocationEvent,
    ) -> None:
        state = invocation.state.model_copy(update={"status": "error"})
        status = LifecycleCommandService.build_status(
            ConversationStatus.ERROR,
            detail="workflow_projection_failed",
        )
        async with self._workspace.fence(conversation_id):
            await self._workspace.append_transition_batch_locked(
                conversation_id,
                [WorkflowInvocationEvent(state=state, status="error"), status],
                bind_current=True,
            )

    def loop_for(
        self,
        conversation_id: str,
        *,
        driver_context_window: int | None = None,
    ) -> AgentLoop:
        compose_snapshot = self._contexts.compose_snapshot(conversation_id)
        if driver_context_window is None and compose_snapshot is not None:
            driver_context_window = compose_snapshot.context_window
        existing = self._loops.loop(conversation_id)
        if driver_context_window is None and existing is not None:
            return existing
        override = (
            compose_snapshot.model_key
            if compose_snapshot is not None
            else self._settings._get_model_override(conversation_id)
        )
        surface = self._settings._surface_of(conversation_id)
        autonomous = self._settings._effective_autonomous(conversation_id)
        strict_appkit = (
            surface in _BUILD_LIKE_SURFACES
            and self._settings._effective_appkit_mode(conversation_id)
            and not self._settings._effective_artifact_mode(conversation_id)
        )
        router = self._drivers.router(
            pick=override,
            surface=surface,
            autonomous=autonomous,
            conversation_id=conversation_id,
            appkit_mode=strict_appkit,
        )
        agent = self._agent(
            surface,
            router,
            conversation_id,
            override,
            driver_context_window,
        )
        loop = self._compose_surface(
            surface,
            conversation_id,
            router,
            agent,
            driver_context_window,
        )
        loop.stream_sink = lambda frame: self._event_store.publish_ephemeral(conversation_id, frame)
        self._loops.bind(conversation_id, loop)
        return loop

    @staticmethod
    def _agent(
        surface: str,
        router: DefaultLLMRouter,
        conversation_id: str,
        override: str | None,
        driver_context_window: int | None,
    ) -> RouterAgent:
        agent_type = BuildAgent if surface in _BUILD_LIKE_SURFACES else ResearchAgent
        return agent_type(
            router,
            conversation_id=conversation_id,
            model_override=override,
            driver_context_window=driver_context_window,
        )

    def _compose_surface(
        self,
        surface: str,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        driver_context_window: int | None,
        *,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
    ) -> AgentLoop:
        if surface in _BUILD_LIKE_SURFACES:
            return self.compose_build_loop(
                conversation_id,
                router,
                agent,
                driver_context_window=driver_context_window,
                sealed_workflow_run=sealed_workflow_run,
                sealed_workflow_instance_id=sealed_workflow_instance_id,
            )
        if surface == "deep_research":
            return self._deep_research._compose_deep_research_loop(
                conversation_id,
                router,
                agent,
                driver_context_window=driver_context_window,
            )
        return AgentLoop(
            conversation_id,
            self._event_store,
            agent,
            _NoToolExecutor(),
            router,
            RuleBasedAnalyzer(),
            NeverConfirm(),
            NoOpCondenser(),
            RouterSummarizer(router),
            mode=self._mode,
            model_policy=ModelExecutionPolicy.standard(),
            driver_context_window=driver_context_window,
        )

    def loop_for_resolved(
        self,
        conversation_id: str,
        snapshot: ResolvedDriverContext,
        *,
        force_recompose: bool = False,
    ) -> AgentLoop:
        existing = self._loops.loop(conversation_id)
        if self._workspace.has_admitted_run(conversation_id):
            if existing is None:
                raise RuntimeError("admitted conversation has no composed loop")
            return existing
        if force_recompose:
            return self._replace_binding(conversation_id, snapshot)
        if self._binding_unchanged(conversation_id, snapshot, existing):
            assert existing is not None
            return existing
        return self._replace_binding(conversation_id, snapshot)

    def _binding_unchanged(
        self,
        conversation_id: str,
        snapshot: ResolvedDriverContext,
        existing: AgentLoop | None,
    ) -> bool:
        previous = self._contexts.resolved_snapshot(conversation_id)
        if existing is None or previous is None:
            return False
        return (
            previous.model_key,
            previous.provider,
            previous.model_id,
            previous.context_window,
        ) == (
            snapshot.model_key,
            snapshot.provider,
            snapshot.model_id,
            snapshot.context_window,
        )

    def _replace_binding(
        self,
        conversation_id: str,
        snapshot: ResolvedDriverContext,
    ) -> AgentLoop:
        old_loop = self._loops.pop(conversation_id)
        old_executor = self._resources.pop_executor(conversation_id)
        old_sandbox = old_executor.sandbox if old_executor is not None else None
        old_pending = self._resources.pop_pending_session(conversation_id)
        if old_sandbox is not None:
            self._resources.set_pending_session(
                conversation_id,
                cast(SandboxSession, old_sandbox),
            )
        self._contexts.begin_compose(conversation_id, snapshot)
        try:
            loop = self.loop_for(conversation_id)
        except BaseException:
            self._restore_binding(
                conversation_id,
                old_loop,
                old_executor,
                old_pending,
            )
            raise
        finally:
            self._contexts.end_compose(conversation_id, snapshot)
        self._contexts.bind_resolved(conversation_id, snapshot)
        return loop

    def _restore_binding(
        self,
        conversation_id: str,
        old_loop: AgentLoop | None,
        old_executor: DefaultToolExecutor | None,
        old_pending: SandboxSession | None,
    ) -> None:
        self._loops.forget(conversation_id)
        self._resources.pop_executor(conversation_id)
        self._resources.pop_pending_session(conversation_id)
        if old_loop is not None:
            self._loops.bind(conversation_id, old_loop)
        if old_executor is not None:
            self._resources.set_executor(conversation_id, old_executor)
        if old_pending is not None:
            self._resources.set_pending_session(conversation_id, old_pending)

    def executor_for(self, conversation_id: str) -> DefaultToolExecutor:
        executor = self._resources.executor(conversation_id)
        if executor is None:
            self.loop_for(conversation_id)
            executor = self._resources.executor(conversation_id)
        if executor is None:
            raise LookupError(
                f"no tool executor for conversation {conversation_id!r} "
                "(its surface has no executable tools)"
            )
        return executor
