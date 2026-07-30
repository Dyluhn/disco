"""Build-loop assembly factory and executor composer.

``BuildLoopFactory`` owns per-conversation loop routing, executor state, and
the resolved-driver-context binding cache.  ``BuildLoopComposer`` owns the
heavyweight executor/loop construction for Build-like surfaces.  Neither class
references ``ConversationRuntime``; every runtime concern is reached through a
typed port defined in :mod:`build_loop_components`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from disco.core import (
    DEFAULT_OWNER_ID,
    LLMSummarizingCondenser,
    NoOpCondenser,
    SecurityRisk,
    agent_view_consistent_events,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)
from disco.core.llm import (
    DefaultLLMRouter,
    ModelExecutionPolicy,
    OperatingMode,
    RouterSummarizer,
)
from disco.core.loop import (
    AgentLoop,
    BlastRadiusConfirm,
    BuildAgent,
    NeverConfirm,
    ResearchAgent,
    RouterAgent,
    host_verify_authoritative_enabled,
)
from disco.core.security import RuleBasedAnalyzer
from disco.core.workflow import WorkflowRun, compile_workflow_scope
from disco.tools import (
    WORKFLOW_ROUTER_ALLOWED_TOOLS,
    AppKitPhase,
    AppKitToolExecutor,
    Capability,
    CapabilityBroker,
    DefaultToolExecutor,
    SandboxInstance,
    SandboxSession,
    ScopedPhaseExecutor,
    ToolDef,
    ToolRegistry,
    ToolScope,
    WorkflowPhaseState,
    build_default_registry,
    workflow_effective_scope,
)
from disco.tools.builtin.workflow_tools import JsonDirWorkflowStore, workflow_router_tools

from .build_loop_components import (
    ComposeContext,
    LoopBuildPlatformPort,
    LoopContractPort,
    LoopDeepResearchPort,
    LoopEventStorePort,
    LoopMcpPort,
    LoopProjectStorePort,
    LoopRetrievalPort,
    LoopRouterPort,
    LoopSandboxPort,
    LoopSettingsPort,
    LoopWorkspacePort,
    _apply_mcp_scope,
    _bind_ejection,
    _build_appkit_registry,
    _mcp_base_risk_by_tool,
    _mcp_composition_inputs,
    _NoToolExecutor,
    _sync_appkit_live_preview,
    build_platform_shadow_enabled,
    observe_legacy_build,
    resolve_compose_context,
)
from .driver_context import ResolvedDriverContext
from .verify.dispatcher import HostVerifierDispatcher
from .verify.host import HostWebAppVerifier
from .verify.model_verifier import ModelVerifier
from .workflow_events import handle_workflow_tool_event

logger = logging.getLogger(__name__)

_BUILD_LIKE_SURFACES = frozenset({"build", "agent"})


@dataclass(frozen=True, slots=True)
class LoopAssemblyInputs:
    """Common inputs shared by all three AgentLoop assembly variants."""

    conversation_id: str
    router: DefaultLLMRouter
    agent: RouterAgent
    executor: DefaultToolExecutor
    ctx: ComposeContext
    mcp_risks: dict[str, SecurityRisk]
    finish_alias: str | None
    quiet: bool
    autonomous: bool
    dcw: int | None
    store: LoopEventStorePort
    host_verifier: HostVerifierDispatcher
    verifier_judge: ModelVerifier
    host_verify_canary_hook: Any
    host_verify_auth: bool
    terminal_hook: Any
    seal_probe: Any
    fence: Any


class BuildLoopFactory:
    """Own per-conversation loop routing, executor state, and context binding."""

    def __init__(
        self,
        *,
        settings: LoopSettingsPort,
        workspace: LoopWorkspacePort,
        event_store: LoopEventStorePort,
        deep_research: LoopDeepResearchPort,
        composer: BuildLoopComposer,
        router: LoopRouterPort,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._event_store = event_store
        self._deep_research = deep_research
        self._composer = composer
        self._router = router
        self._mode = mode
        self._loops: dict[str, AgentLoop] = {}
        self._executors: dict[str, DefaultToolExecutor] = {}
        self._pending_sessions: dict[str, SandboxSession] = {}
        self._resolved_context_for_compose: dict[str, ResolvedDriverContext] = {}
        self._resolved_driver_contexts: dict[str, ResolvedDriverContext] = {}

    def build_broker(self) -> CapabilityBroker:
        """The orchestrator-side capability broker for a Build conversation."""
        return self._composer.build_broker()

    def loop_for(
        self,
        conversation_id: str,
        *,
        driver_context_window: int | None = None,
    ) -> AgentLoop:
        compose_snapshot = self._resolved_context_for_compose.get(conversation_id)
        if driver_context_window is None and compose_snapshot is not None:
            driver_context_window = compose_snapshot.context_window
        if driver_context_window is None:
            loop = self._loops.get(conversation_id)
            if loop is not None:
                return loop
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
        router = self._router._router_now(
            pick=override, surface=surface, autonomous=autonomous,
            conversation_id=conversation_id, appkit_mode=strict_appkit,
        )
        if surface in _BUILD_LIKE_SURFACES:
            agent: RouterAgent = BuildAgent(
                router, conversation_id=conversation_id,
                model_override=override, driver_context_window=driver_context_window,
            )
        else:
            agent = ResearchAgent(
                router, conversation_id=conversation_id,
                model_override=override, driver_context_window=driver_context_window,
            )
        if surface in _BUILD_LIKE_SURFACES:
            loop = self._composer.compose_build_loop(
                conversation_id, router, agent,
                driver_context_window=driver_context_window,
                loops=self._loops, executors=self._executors,
                pending_sessions=self._pending_sessions,
            )
        elif surface == "deep_research":
            loop = self._deep_research._compose_deep_research_loop(
                conversation_id, router, agent,
                driver_context_window=driver_context_window,
            )
        else:
            loop = AgentLoop(
                conversation_id, self._event_store, agent, _NoToolExecutor(),
                router, RuleBasedAnalyzer(), NeverConfirm(), NoOpCondenser(),
                RouterSummarizer(router), mode=self._mode,
                model_policy=ModelExecutionPolicy.standard(),
                driver_context_window=driver_context_window,
            )
        loop.stream_sink = lambda frame, _cid=conversation_id: self._event_store.publish_ephemeral(
            _cid, frame
        )
        self._loops[conversation_id] = loop
        return loop

    def loop_for_resolved(
        self, conversation_id: str, snapshot: ResolvedDriverContext
    ) -> AgentLoop:
        existing_loop = self._loops.get(conversation_id)
        if self._workspace.has_admitted_run(conversation_id):
            if existing_loop is None:
                raise RuntimeError("admitted conversation has no composed loop")
            return existing_loop
        previous_snapshot = self._resolved_driver_contexts.get(conversation_id)
        if existing_loop is not None and previous_snapshot is not None:
            previous_binding = (
                previous_snapshot.model_key,
                previous_snapshot.provider,
                previous_snapshot.model_id,
                previous_snapshot.context_window,
            )
            next_binding = (
                snapshot.model_key,
                snapshot.provider,
                snapshot.model_id,
                snapshot.context_window,
            )
            if previous_binding == next_binding:
                return existing_loop
        old_loop = self._loops.pop(conversation_id, None)
        old_executor = self._executors.pop(conversation_id, None)
        old_sandbox = getattr(old_executor, "_sandbox", None) if old_executor is not None else None
        had_pending = conversation_id in self._pending_sessions
        old_pending = self._pending_sessions.get(conversation_id)
        if old_sandbox is not None:
            self._pending_sessions[conversation_id] = old_sandbox
        self._resolved_context_for_compose[conversation_id] = snapshot
        try:
            loop = self.loop_for(conversation_id)
        except BaseException:
            self._loops.pop(conversation_id, None)
            self._executors.pop(conversation_id, None)
            if old_loop is not None:
                self._loops[conversation_id] = old_loop
            if old_executor is not None:
                self._executors[conversation_id] = old_executor
            if had_pending and old_pending is not None:
                self._pending_sessions[conversation_id] = old_pending
            else:
                self._pending_sessions.pop(conversation_id, None)
            raise
        finally:
            if self._resolved_context_for_compose.get(conversation_id) is snapshot:
                self._resolved_context_for_compose.pop(conversation_id, None)
        self._resolved_driver_contexts[conversation_id] = snapshot
        return loop

    def executor_for(self, conversation_id: str) -> DefaultToolExecutor:
        executor = self._executors.get(conversation_id)
        if executor is None:
            self.loop_for(conversation_id)
            executor = self._executors.get(conversation_id)
        if executor is None:
            raise LookupError(
                f"no tool executor for conversation {conversation_id!r} "
                "(its surface has no executable tools)"
            )
        return executor


class BuildLoopAssembler:
    """Construct the final AgentLoop for a Build-like surface."""

    def __init__(
        self,
        *,
        settings: LoopSettingsPort,
        workspace: LoopWorkspacePort,
        event_store: LoopEventStorePort,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._event_store = event_store

    def assemble(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        executor: DefaultToolExecutor,
        ctx: ComposeContext,
        sealed_workflow_run: WorkflowRun | None,
        mcp_risks: dict[str, SecurityRisk],
        finish_alias: str | None,
        host_verifier: HostVerifierDispatcher,
        verifier_judge: ModelVerifier,
        host_verify_canary_hook: Any,
    ) -> AgentLoop:
        def fence() -> Any:
            return self._workspace.fence(conversation_id)

        inputs = LoopAssemblyInputs(
            conversation_id=conversation_id, router=router, agent=agent,
            executor=executor, ctx=ctx, mcp_risks=mcp_risks,
            finish_alias=finish_alias, quiet=self._settings._effective_quiet(conversation_id),
            autonomous=self._settings._effective_autonomous(conversation_id),
            dcw=ctx.driver_context_window, store=self._event_store,
            host_verifier=host_verifier, verifier_judge=verifier_judge,
            host_verify_canary_hook=host_verify_canary_hook,
            host_verify_auth=host_verify_authoritative_enabled(),
            terminal_hook=self._workspace.terminal_commit_hook(conversation_id),
            seal_probe=self._workspace.finish_sealability_probe(conversation_id),
            fence=fence,
        )
        if sealed_workflow_run is not None:
            return self._sealed_workflow_loop(inputs, sealed_workflow_run)
        if ctx.art_mode:
            return self._artifact_loop(inputs)
        return self._standard_loop(inputs)

    def _sealed_workflow_loop(
        self, inp: LoopAssemblyInputs, sealed_workflow_run: WorkflowRun
    ) -> AgentLoop:
        ctx = inp.ctx
        assert ctx.workflow_phase is not None
        assert ctx.workflow_phase.compiled_run_scope is not None
        plan_gated = "submit_plan" in ctx.workflow_phase.compiled_run_scope.allowed_tools
        planning_tools = (
            frozenset({"submit_plan", "file_list", "file_read", "search", "extract", "think"})
            & ctx.workflow_phase.compiled_run_scope.allowed_tools
        )
        return AgentLoop(
            inp.conversation_id, inp.store, inp.agent, inp.executor, inp.router,
            RuleBasedAnalyzer(inp.mcp_risks), BlastRadiusConfirm(),
            LLMSummarizingCondenser(context_window=inp.dcw), RouterSummarizer(inp.router),
            mode=OperatingMode.PLANNING if plan_gated else OperatingMode.INTERACTIVE,
            planning_tools=planning_tools if plan_gated else frozenset(),
            autonomous=True, model_policy=ctx.model_policy,
            driver_context_window=inp.dcw, quiet=inp.quiet,
            workflow_run=sealed_workflow_run, finish_alias=inp.finish_alias,
            host_verifier=inp.host_verifier, verifier_judge=inp.verifier_judge,
            host_verifier_verdict_hook=inp.host_verify_canary_hook,
            host_verify_authoritative=inp.host_verify_auth,
            terminal_commit_hook=inp.terminal_hook,
            finish_sealability_probe=inp.seal_probe, control_fence=inp.fence,
        )

    def _artifact_loop(self, inp: LoopAssemblyInputs) -> AgentLoop:
        ctx = inp.ctx
        return AgentLoop(
            inp.conversation_id, inp.store, inp.agent, inp.executor, inp.router,
            RuleBasedAnalyzer(inp.mcp_risks), BlastRadiusConfirm(),
            LLMSummarizingCondenser(context_window=inp.dcw), RouterSummarizer(inp.router),
            mode=OperatingMode.INTERACTIVE, autonomous=inp.autonomous,
            model_policy=ctx.model_policy, driver_context_window=inp.dcw,
            quiet=inp.quiet, finish_alias=inp.finish_alias,
            host_verifier=inp.host_verifier, verifier_judge=inp.verifier_judge,
            host_verifier_verdict_hook=inp.host_verify_canary_hook,
            host_verify_authoritative=inp.host_verify_auth,
            terminal_commit_hook=inp.terminal_hook,
            finish_sealability_probe=inp.seal_probe, control_fence=inp.fence,
        )

    def _standard_loop(self, inp: LoopAssemblyInputs) -> AgentLoop:
        ctx = inp.ctx
        if ctx.appkit_mode:
            inp.mcp_risks["request_custom_build"] = SecurityRisk.HIGH
        planning_tools = frozenset(
            {"submit_plan", "file_list", "file_read", "search", "extract", "think"}
        )
        if ctx.workflow_router_mode:
            planning_tools = planning_tools | WORKFLOW_ROUTER_ALLOWED_TOOLS
        return AgentLoop(
            inp.conversation_id, inp.store, inp.agent, inp.executor, inp.router,
            RuleBasedAnalyzer(inp.mcp_risks), BlastRadiusConfirm(),
            LLMSummarizingCondenser(context_window=inp.dcw), RouterSummarizer(inp.router),
            mode=OperatingMode.PLANNING, planning_tools=planning_tools,
            autonomous=inp.autonomous, model_policy=ctx.model_policy,
            driver_context_window=inp.dcw, quiet=inp.quiet,
            finish_alias=inp.finish_alias,
            host_verifier=inp.host_verifier, verifier_judge=inp.verifier_judge,
            host_verifier_verdict_hook=inp.host_verify_canary_hook,
            host_verify_authoritative=inp.host_verify_auth,
            terminal_commit_hook=inp.terminal_hook,
            finish_sealability_probe=inp.seal_probe, control_fence=inp.fence,
            strict_appkit_active=(
                (lambda phase=ctx.appkit_phase: phase.phase != AppKitPhase.CUSTOM_BUILD)
                if ctx.appkit_phase is not None
                else None
            ),
        )


class BuildLoopComposer:
    """Own Build-like executor and AgentLoop construction."""

    def __init__(
        self,
        *,
        settings: LoopSettingsPort,
        sandbox: LoopSandboxPort,
        mcp: LoopMcpPort,
        contract: LoopContractPort,
        workspace: LoopWorkspacePort,
        build_platform: LoopBuildPlatformPort,
        retrieval: LoopRetrievalPort,
        event_store: LoopEventStorePort,
        project_store: LoopProjectStorePort,
    ) -> None:
        self._settings = settings
        self._sandbox = sandbox
        self._mcp = mcp
        self._contract = contract
        self._workspace = workspace
        self._build_platform = build_platform
        self._retrieval = retrieval
        self._event_store = event_store
        self._project_store = project_store
        self._shadow_records: dict[str, Any] = {}
        self._assembler = BuildLoopAssembler(
            settings=settings,
            workspace=workspace,
            event_store=event_store,
        )

    def build_broker(self) -> CapabilityBroker:
        broker = CapabilityBroker()
        handlers = self._retrieval._retrieval_handlers()

        async def _search(*, query: str, limit: int = 8) -> Any:
            return await handlers["search"](query=query, limit=limit)

        async def _extract(*, url: str) -> Any:
            return await handlers["extract"](url=url)

        broker.register("search", _search)
        broker.register("extract", _extract)
        return broker

    def compose_build_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
        loops: dict[str, AgentLoop],
        executors: dict[str, DefaultToolExecutor],
        pending_sessions: dict[str, SandboxSession],
    ) -> AgentLoop:
        broker = self.build_broker()
        session = self._adopt_or_create_session(
            conversation_id, pending_sessions, sealed_workflow_run
        )
        ctx = resolve_compose_context(
            self._settings,
            self._contract,
            conversation_id,
            driver_context_window,
            sealed_workflow_run,
            sealed_workflow_instance_id,
        )

        async def _workflow_events(kind: str, payload: dict[str, Any]) -> None:
            await handle_workflow_tool_event(
                conversation_id=conversation_id, loops=loops, kind=kind, payload=payload,
            )

        common_kwargs = self._build_common_exec_kwargs(
            session, broker, conversation_id, ctx, _workflow_events
        )
        executor = self._construct_executor(
            conversation_id, ctx, common_kwargs, loops, sealed_workflow_run
        )
        all_mcp_tools, mcp_max_schemas = _mcp_composition_inputs(
            self._mcp._mcp_pool,
            self._mcp._mcp_http_tools,
            self._mcp._config_store(),
        )
        self._wire_mcp_tools(executor, all_mcp_tools, mcp_max_schemas, ctx, conversation_id)
        if sealed_workflow_run is not None:
            assert ctx.workflow_phase is not None
            ctx.workflow_phase.compiled_run_scope = compile_workflow_scope(
                sealed_workflow_run.definition,
                frozenset(t.name for t in all_mcp_tools),
            )
        for server, info in self._mcp._mcp_approval_pending.items():
            try:
                self._event_store.publish_ephemeral(conversation_id, {
                    "type": "mcp_approval_required", "server": server,
                    "description_hash": info.get("new_hash", ""),
                    "old_description_hash": info.get("old_hash", ""),
                })
            except Exception:
                pass
        executors[conversation_id] = executor
        mcp_risks = _mcp_base_risk_by_tool(all_mcp_tools)
        finish_alias = (
            sealed_workflow_run.definition.verify.finalizer
            if sealed_workflow_run is not None
            else self._contract._finalizer_alias_for(conversation_id)
        )
        web_verifier = HostWebAppVerifier(executor)
        host_verifier = HostVerifierDispatcher(
            {
                (
                    "disco.host_web_verifier@1",
                    "disco.web_functional@1",
                    "host.verify_deliverable",
                ): web_verifier,
            },
            legacy_adapter=web_verifier,
        )
        verifier_judge = ModelVerifier(router, conversation_id=conversation_id)
        host_verify_canary_hook = self._contract._host_verify_canary_hook_for(conversation_id)
        _set_alias = getattr(agent, "set_finish_alias", None)
        if callable(_set_alias):
            _set_alias(finish_alias)
        if build_platform_shadow_enabled() and sealed_workflow_run is None:
            try:
                self._shadow_records[conversation_id] = observe_legacy_build(
                    appkit_mode=ctx.appkit_mode,
                    tool_specs=executor.available_tools(),
                )
            except Exception:
                logger.warning(
                    "build-platform shadow observer failed; legacy authority is unchanged",
                    exc_info=True,
                )
        self._build_platform.select_builtin(
            conversation_id,
            appkit=ctx.appkit_mode,
            eligible=sealed_workflow_run is None
            and not any((ctx.art_mode, ctx.workflow_router_mode)),
            tool_specs=executor.available_tools(),
        )
        return self._assembler.assemble(
            conversation_id, router, agent, executor, ctx,
            sealed_workflow_run, mcp_risks, finish_alias,
            host_verifier, verifier_judge, host_verify_canary_hook,
        )

    def _build_common_exec_kwargs(
        self,
        session: SandboxSession,
        broker: CapabilityBroker,
        conversation_id: str,
        ctx: ComposeContext,
        workflow_events: Any,
    ) -> dict[str, Any]:
        from .security_live_verifier import make_security_live_verifier

        event_store = self._event_store

        async def execution_admission(agent_view_id: str | None) -> str | None:
            events = agent_view_consistent_events(await event_store.get_events(conversation_id))
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

        return dict(
            sandbox=cast(SandboxInstance, session), broker=broker,
            conversation_id=conversation_id, model_policy=ctx.model_policy,
            driver_llm=self._settings._effective_driver_endpoint(conversation_id),
            read_char_budget=ctx.read_char_budget, scope_guard=ctx.scope_guard,
            on_tool_success=ctx.on_tool_success,
            starter_kit=self._contract._starter_kit_for(conversation_id),
            workflow_events=workflow_events if ctx.workflow_router_mode else None,
            primitive_live_verifier=make_security_live_verifier(),
            workspace_lock=self._workspace.lock(conversation_id),
            workspace_fence=lambda: self._workspace.interprocess_mutation_fence(conversation_id),
            execution_admission=execution_admission,
            release_intent_writer=self._project_store._write_release_intent,
        )

    def _adopt_or_create_session(
        self, conversation_id: str,
        pending_sessions: dict[str, SandboxSession],
        sealed_workflow_run: WorkflowRun | None,
    ) -> SandboxSession:
        session = pending_sessions.pop(conversation_id, None)
        if session is not None:
            return session
        egress_surface = self._settings._surface_of(conversation_id)
        if self._settings._effective_artifact_mode(conversation_id):
            egress_surface = "build"
        sandbox_spec = self._sandbox._build_sandbox_spec(
            surface=egress_surface, mcp_egress_hosts=self._mcp._mcp_egress_hosts(),
        )
        if sealed_workflow_run is not None:
            sandbox_spec = sandbox_spec.model_copy(update={
                "permitted": sandbox_spec.permitted - {Capability.NETWORK},
                "egress_allow": frozenset(sealed_workflow_run.definition.policies.egress_allow),
                "public_web": False,
            })
        return SandboxSession(
            self._sandbox._sandbox_service_now(), sandbox_spec,
            conversation_id=conversation_id,
            on_recreate=lambda: self._sandbox._rehydrate_after_recreate(conversation_id),
            legacy_auto_preview=False,
        )

    def _construct_executor(
        self, conversation_id: str, ctx: ComposeContext,
        common_kwargs: dict[str, Any], loops: dict[str, AgentLoop],
        sealed_workflow_run: WorkflowRun | None,
    ) -> DefaultToolExecutor:
        if ctx.appkit_mode:
            assert ctx.appkit_phase is not None
            return AppKitToolExecutor(
                _build_appkit_registry(), ctx.scope,
                appkit_phase=ctx.appkit_phase, base_scope=ctx.scope,
                autonomous=ctx.appkit_autonomous,
                mode_getter=lambda cid=conversation_id: (
                    loops[cid].mode if cid in loops else OperatingMode.PLANNING
                ),
                on_preview_sync=lambda s=common_kwargs["sandbox"]: _sync_appkit_live_preview(s),
                **common_kwargs,
            )
        if ctx.workflow_router_mode or sealed_workflow_run is not None:
            return self._construct_workflow_executor(conversation_id, ctx, common_kwargs)
        return DefaultToolExecutor(build_default_registry(), ctx.scope, **common_kwargs)

    def _construct_workflow_executor(
        self, conversation_id: str, ctx: ComposeContext, common_kwargs: dict[str, Any],
    ) -> ScopedPhaseExecutor:
        assert ctx.workflow_phase is not None
        workflow_registry = build_default_registry()
        project_root = self._project_store._project_store_now().root
        owner_id = self._event_store.conversation_owner_id_sync(conversation_id) or DEFAULT_OWNER_ID
        workflow_store = JsonDirWorkflowStore(project_root or "", owner_id=owner_id)

        def _mcp_tool_names(registry: ToolRegistry = workflow_registry) -> frozenset[str]:
            return frozenset(n for n in registry.names() if n.startswith("mcp__"))

        if ctx.workflow_router_mode:
            for tool in workflow_router_tools(
                store=workflow_store, phase_state=ctx.workflow_phase,
                mcp_tool_names_getter=_mcp_tool_names,
            ):
                workflow_registry.register(tool)
        ref: dict[str, ScopedPhaseExecutor] = {}

        def _scope_resolver(ps: WorkflowPhaseState = ctx.workflow_phase) -> ToolScope:
            ex = ref["executor"]
            return workflow_effective_scope(
                phase=ps.phase, compiled_run_scope=ps.compiled_run_scope,
                base_scope=ex.widened_scope, output_path_template=ps.output_path_template,
            )

        executor = ScopedPhaseExecutor(
            workflow_registry, ctx.scope, scope_resolver=_scope_resolver, **common_kwargs
        )
        ref["executor"] = executor
        return executor

    def _wire_mcp_tools(
        self, executor: DefaultToolExecutor, all_mcp_tools: list[ToolDef],
        mcp_max_schemas: int, ctx: ComposeContext, conversation_id: str,
    ) -> None:
        if not all_mcp_tools:
            return
        if ctx.appkit_mode and isinstance(executor, AppKitToolExecutor):
            executor.set_widen_callback(
                lambda ex=executor, tools=all_mcp_tools, ms=mcp_max_schemas: _apply_mcp_scope(
                    ex, tools, self._mcp._mcp_call_target, max_active_schemas=ms
                )
            )
        else:
            _apply_mcp_scope(
                executor, all_mcp_tools, self._mcp._mcp_call_target,
                max_active_schemas=mcp_max_schemas,
            )
        if ctx.appkit_mode and isinstance(executor, AppKitToolExecutor):
            _bind_ejection(
                self._mcp, conversation_id, executor, executor._scope,
                all_mcp_tools, self._mcp._mcp_call_target, mcp_max_schemas,
            )