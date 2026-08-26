"""Typed factories for Build-like executors and conversation loops."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from disco.core import (
    ConversationStatus,
    SecurityRisk,
    WorkflowInvocationEvent,
)
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    DefaultLLMRouter,
)
from disco.core.loop import AgentLoop, RouterAgent
from disco.core.workflow import WorkflowHandoff, WorkflowRun, compile_workflow_scope
from disco.tools import (
    AppKitToolExecutor,
    Capability,
    CapabilityBroker,
    DefaultToolExecutor,
    SandboxSession,
)

from ._build_executor_factory import BuildExecutorFactory  # noqa: F401
from ._build_loop_factory import BuildLoopFactory  # noqa: F401
from .build_loop_assembler import BuildLoopAssembler
from .build_loop_components import (
    ComposeContext,
    LoopEjectionPort,
    LoopEventStorePort,
    LoopMcpPort,
    LoopModeResolver,
    LoopRehydrationPort,
    LoopRetrievalPort,
    LoopSandboxPort,
    LoopSettingsPort,
    McpLoopSnapshot,
    _apply_mcp_scope,
    _bind_ejection,
    _mcp_base_risk_by_tool,
)
from .run_registry import RunResourceRegistry
from .vision_inspection import VisionInspectionCapability
from .workflow_invocation import WorkflowInvocationService

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
