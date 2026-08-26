"""Typed factories for Build-like executors and conversation loops."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from disco.core import (
    ConversationStatus,
    NoOpCondenser,
    WorkflowInvocationEvent,
)
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    DefaultLLMRouter,
    ModelExecutionPolicy,
    OperatingMode,
    RouterSummarizer,
)
from disco.core.loop import AgentLoop, BuildAgent, NeverConfirm, ResearchAgent, RouterAgent
from disco.core.security import RuleBasedAnalyzer
from disco.core.workflow import WorkflowHandoff, WorkflowRun
from disco.tools import (
    CapabilityBroker,
    DefaultToolExecutor,
    SandboxSession,
)

from .build_loop_components import (
    LoopDeepResearchPort,
    LoopDriverPort,
    LoopEventStorePort,
    LoopSettingsPort,
    LoopWorkspacePort,
    _NoToolExecutor,
)
from .driver_context import ResolvedDriverContext
from .driver_context_state import DriverContextState
from .lifecycle_command_service import LifecycleCommandService
from .run_registry import LoopRegistry, RunResourceRegistry
from .workflow_invocation import WorkflowInvocationService

if TYPE_CHECKING:
    from .build_loop_factory import BuildLoopComposer

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


class _BuildLoopWorkflowMixin:
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


class BuildLoopFactory(_BuildLoopWorkflowMixin):
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
