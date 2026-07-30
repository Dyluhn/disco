"""Concrete narrow adapters for workflow/schedule construction.

Replace the whole-runtime casts in ``runtime_composition`` with concrete
adapter instances that delegate to the actual named owners. Each adapter
holds only the narrow collaborators its protocol requires — never the whole
``ConversationRuntime``.

The protocols in ``schedule_service`` remain (they are the contract the
workflow run service and schedule service consume), but the wiring in
``runtime_composition`` now passes concrete adapter instances instead of
casting the runtime.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING, Any

from disco.core import MessageEvent
from disco.core.llm import DefaultLLMRouter
from disco.core.loop import AgentLoop, RouterAgent
from disco.core.workflow import WorkflowRun
from disco.tools.projects import ProjectStore

from .driver_context import ResolvedDriverContext
from .run_registry import (
    LoopRegistry,
    RunAuthorityLedger,
    RunIngressLedger,
    RunRegistry,
    RunResourceRegistry,
)
from .schedule_service import WorkflowDriverContexts
from .workspace_service import WorkspaceCoordinator

if TYPE_CHECKING:
    from .build_loop_factory import BuildLoopFactory
    from .driver_runtime import DriverRuntime
    from .project_runtime_service import ProjectRuntimeService
    from .run_supervisor import RunFinalizer, RunSupervisor
    from .runtime_settings import RuntimeSettings

    # DeepResearchService is imported lazily to avoid a cycle.
    # ConversationControlService is imported lazily for send_user_turn.


class WorkflowProjectAccessAdapter:
    """Concrete ``WorkflowProjectAccess`` over ``ProjectRuntimeService``."""

    def __init__(self, projects: ProjectRuntimeService) -> None:
        self._projects = projects

    def _project_store_now(self) -> ProjectStore:
        return self._projects.current_project_store()


class WorkflowConversationSettingsAdapter:
    """Concrete ``WorkflowConversationSettings`` over ``RuntimeSettings``.

    ``set_surface`` delegates to ``RuntimeSettings._set_surface`` (the public
    ``set_surface`` was a stale facade method on the runtime that never
    existed — this adapter provides it by delegating to the real private
    setter).
    """

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def set_surface(self, conversation_id: str, surface: str) -> None:
        self._settings._set_surface(conversation_id, surface)

    def set_autonomous(self, conversation_id: str, value: bool = True) -> None:
        self._settings.set_autonomous(conversation_id, value)


class WorkflowModelAccessAdapter:
    """Concrete ``WorkflowModelAccess`` over ``DriverRuntime``."""

    def __init__(self, drivers: DriverRuntime) -> None:
        self._drivers = drivers

    async def _resolve_driver_context(
        self,
        conversation_id: str,
    ) -> ResolvedDriverContext:
        return await self._drivers.resolve_context(conversation_id)

    def _router_now(
        self,
        pick: str | None = None,
        *,
        surface: str | None = None,
        autonomous: bool = False,
        conversation_id: str | None = None,
    ) -> DefaultLLMRouter:
        return self._drivers.router(
            pick,
            surface=surface,
            autonomous=autonomous,
            conversation_id=conversation_id,
        )


class WorkflowLoopFactoryAdapter:
    """Concrete ``WorkflowLoopFactory`` over ``BuildLoopFactory``."""

    def __init__(self, loop_factory: BuildLoopFactory) -> None:
        self._loop_factory = loop_factory

    def _compose_build_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
        sealed_workflow_run: WorkflowRun | None = None,
        sealed_workflow_instance_id: str | None = None,
    ) -> AgentLoop:
        return self._loop_factory.compose_build_loop(
            conversation_id,
            router,
            agent,
            driver_context_window=driver_context_window,
            sealed_workflow_run=sealed_workflow_run,
            sealed_workflow_instance_id=sealed_workflow_instance_id,
        )


class WorkflowRunControlAdapter:
    """Concrete ``WorkflowRunControl`` over the actual named run owners.

    Exposes the registries the workflow run service accesses directly
    (``_run_registry``, ``_run_authorities``, ``_run_ingress``,
    ``_loop_registry``, ``_run_resources``, ``_driver_contexts``) and
    delegates ``workspace_lock``, ``_create_run_task``,
    ``_finalize_clean_return``, and ``_rekick_unadmitted_superseding_intent``
    to the real owners.
    """

    def __init__(
        self,
        run_registry: RunRegistry,
        run_authorities: RunAuthorityLedger,
        run_ingress: RunIngressLedger,
        loop_registry: LoopRegistry,
        run_resources: RunResourceRegistry,
        driver_contexts: WorkflowDriverContexts,
        workspace: WorkspaceCoordinator,
        run_supervisor: RunSupervisor,
        run_finalizer: RunFinalizer,
    ) -> None:
        self._run_registry = run_registry
        self._run_authorities = run_authorities
        self._run_ingress = run_ingress
        self._loop_registry = loop_registry
        self._run_resources = run_resources
        self._driver_contexts = driver_contexts
        self._workspace = workspace
        self._run_supervisor = run_supervisor
        self._run_finalizer = run_finalizer

    def workspace_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._workspace.lock(conversation_id)

    def _create_run_task(
        self,
        conversation_id: str,
        loop: AgentLoop | None = None,
        *,
        expected_run_intent_id: str | None = None,
        task_context: contextvars.Context | None = None,
    ) -> tuple[asyncio.Task[Any], int]:
        assert loop is not None  # workflow run always composes a loop first
        return self._run_supervisor.create_task(
            conversation_id,
            loop,
            expected_run_intent_id=expected_run_intent_id,
            task_context=task_context,
        )

    async def _finalize_clean_return(
        self,
        conversation_id: str,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None:
        await self._run_finalizer.finalize_clean(
            conversation_id,
            generation,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def _rekick_unadmitted_superseding_intent(
        self,
        conversation_id: str,
    ) -> None:
        await self._run_finalizer.rekick_unadmitted(conversation_id)


class RecurringScheduleControlAdapter:
    """Concrete ``RecurringScheduleControl`` over the actual named owners.

    ``set_model_override`` delegates to ``RuntimeSettings``.
    ``set_depth`` delegates to ``DeepResearchService`` (which owns the depth
    tier state).
    ``send_user_turn`` delegates to ``ConversationControlService`` via the
    runtime's public ``send_user_turn`` method.
    """

    def __init__(
        self,
        settings: RuntimeSettings,
        send_user_turn_fn: Any,
        set_depth_fn: Any,
    ) -> None:
        self._settings = settings
        self._send_user_turn_fn = send_user_turn_fn
        self._set_depth_fn = set_depth_fn

    def set_model_override(
        self,
        conversation_id: str,
        model_id: str | None,
    ) -> None:
        self._settings.set_model_override(conversation_id, model_id)

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        self._set_depth_fn(conversation_id, tier)

    async def send_user_turn(self, conversation_id: str, text: str) -> MessageEvent:
        return await self._send_user_turn_fn(conversation_id, text)