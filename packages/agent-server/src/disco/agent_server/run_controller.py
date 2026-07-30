"""Conversation run launch and reentry.

``RunController`` owns loop launch and the synchronous ``kick`` reentry.  The
``resume_conversation`` reentry delegate has been removed: it created a
construction-order circularity with ``ResumeService`` (which triggers the
pinned run start through the bounded ``RunStartPort`` boundary).  The
integrator provides a ``RunReentryAdapter`` combining ``RunController.kick``
and ``ResumeService.resume_conversation`` to ``RunFinalizer`` (which needs
both through ``RunReentryPort``).
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable

from disco.core.loop import AgentLoop

from .build_loop_factory import BuildLoopFactory
from .build_platform_runtime import BuildPlatformRuntime
from .driver_runtime import DriverRuntime
from .run_registry import RunRegistry, RunTask
from .run_supervisor import RunSupervisor
from .sandbox_resource_reconciler import SandboxResourceReconciler
from .title_service import TitleService


class RunController:
    """Launch one resolved loop and expose the synchronous kick reentry."""

    def __init__(
        self,
        registry: RunRegistry,
        supervisor: RunSupervisor,
        titles: TitleService,
        sandboxes: SandboxResourceReconciler,
        build_platform: BuildPlatformRuntime,
        drivers: DriverRuntime,
        loops: BuildLoopFactory,
    ) -> None:
        self._registry = registry
        self._supervisor = supervisor
        self._titles = titles
        self._sandboxes = sandboxes
        self._build_platform = build_platform
        self._drivers = drivers
        self._loops = loops

    def kick(
        self,
        conversation_id: str,
        *,
        claimed_user_seq: int | None = None,
    ) -> None:
        self._titles.schedule(conversation_id)
        if self._registry.active_task(conversation_id) is not None:
            return
        self._sandboxes.evict_stale(conversation_id)

        async def resolve_loop() -> AgentLoop:
            await self._build_platform.prepare_route_pin(conversation_id)
            snapshot = await self._drivers.resolve_context(conversation_id)
            return self._loops.loop_for_resolved(conversation_id, snapshot)

        self._supervisor.create_task(
            conversation_id,
            resolve_loop,
            claimed_user_seq=claimed_user_seq,
        )

    def loop_for(self, conversation_id: str) -> AgentLoop:
        """Resolve the current loop for a gate or other synchronous control."""

        return self._loops.loop_for(conversation_id)

    def create_task(
        self,
        conversation_id: str,
        loop: AgentLoop | None = None,
        *,
        loop_factory: Callable[[], Awaitable[AgentLoop]] | None = None,
        claimed_user_seq: int | None = None,
        expected_run_intent_id: str | None = None,
        task_context: contextvars.Context | None = None,
    ) -> tuple[RunTask, int]:
        if (loop is None) == (loop_factory is None):
            raise ValueError("provide exactly one of loop or loop_factory")
        selected: AgentLoop | Callable[[], Awaitable[AgentLoop]]
        if loop is not None:
            selected = loop
        else:
            assert loop_factory is not None
            selected = loop_factory
        return self._supervisor.create_task(
            conversation_id,
            selected,
            claimed_user_seq=claimed_user_seq,
            expected_run_intent_id=expected_run_intent_id,
            task_context=task_context,
        )
