"""Typed factories for Build-like executors and conversation loops."""

from __future__ import annotations

import logging
from typing import Any, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    NoOpCondenser,
    SecurityRisk,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)
from disco.core.llm import DefaultLLMRouter, ModelExecutionPolicy, OperatingMode, RouterSummarizer
from disco.core.loop import AgentLoop, BuildAgent, NeverConfirm, ResearchAgent, RouterAgent
from disco.core.security import RuleBasedAnalyzer
from disco.core.workflow import WorkflowRun, compile_workflow_scope
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
from .run_registry import LoopRegistry, RunResourceRegistry
from .security_live_verifier import make_security_live_verifier
from .workflow_events import handle_workflow_tool_event

logger = logging.getLogger(__name__)

_BUILD_LIKE_SURFACES = frozenset({"build", "agent"})


class BuildCapabilityBroker:
    """Create revocable Build capabilities over typed retrieval operations."""

    def __init__(self, retrieval: LoopRetrievalPort) -> None:
        self._retrieval = retrieval

    def build(self) -> CapabilityBroker:
        broker = CapabilityBroker()
        broker.register("search", self._retrieval.capability_search)
        broker.register("extract", self._retrieval.capability_extract)
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
                    "egress_allow": frozenset(
                        sealed_workflow_run.definition.policies.egress_allow
                    ),
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
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._project = project
        self._contract = contract
        self._loops = loops
        self._event_store = event_store

    def create(
        self,
        conversation_id: str,
        session: SandboxSession,
        broker: CapabilityBroker,
        context: ComposeContext,
        sealed_workflow_run: WorkflowRun | None,
    ) -> DefaultToolExecutor:
        common = self._common_kwargs(
            conversation_id,
            session,
            broker,
            context,
        )
        modes = context.modes
        if modes.appkit_mode:
            return self._appkit_executor(conversation_id, session, context, common)
        if modes.workflow_router_mode or sealed_workflow_run is not None:
            return self._workflow_executor(conversation_id, context, common)
        return DefaultToolExecutor(
            build_default_registry(),
            context.policy.scope,
            **common,
        )

    def _common_kwargs(
        self,
        conversation_id: str,
        session: SandboxSession,
        broker: CapabilityBroker,
        context: ComposeContext,
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
        }

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
        return AppKitToolExecutor(
            _build_appkit_registry(),
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
                self._event_store.conversation_owner_id_sync(conversation_id)
                or DEFAULT_OWNER_ID
            ),
        )

        def mcp_tool_names() -> frozenset[str]:
            return frozenset(
                name for name in registry.names() if name.startswith("mcp__")
            )

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

    def build_broker(self) -> CapabilityBroker:
        return self._brokers.build()

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
            self._brokers.build(),
            context,
            sealed_workflow_run,
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

    def build_broker(self) -> CapabilityBroker:
        return self._composer.build_broker()

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
        loop.stream_sink = (
            lambda frame: self._event_store.publish_ephemeral(conversation_id, frame)
        )
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
    ) -> AgentLoop:
        if surface in _BUILD_LIKE_SURFACES:
            return self.compose_build_loop(
                conversation_id,
                router,
                agent,
                driver_context_window=driver_context_window,
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
    ) -> AgentLoop:
        existing = self._loops.loop(conversation_id)
        if self._workspace.has_admitted_run(conversation_id):
            if existing is None:
                raise RuntimeError("admitted conversation has no composed loop")
            return existing
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
