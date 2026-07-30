"""Run task supervision, terminal recovery, and persistence boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from disco.core import (
    ConversationState,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    agent_view_consistent_events,
    workspace_run_intent_admission_required,
)
from disco.core.loop import AgentLoop, AgentViewSuperseded, signals
from disco.core.store.sqlite import SqliteEventStore

from .lifecycle_command_service import LifecycleCommandService
from .run_completion import RunCompletionPort
from .run_registry import (
    KernelPinRegistry,
    RunAuthorityLedger,
    RunIngressLedger,
    RunRecoveryLedger,
    RunRegistry,
    RunResourceRegistry,
    RunTask,
    RunWorkspacePort,
)
from .run_supervision_protocols import (
    DeepResearchRunPort,
    RunObservabilityPort,
    RunPersistencePort,
    RunPreflightPort,
    RunReentryPort,
    RunSurfacePolicy,
)
from .workspace_commit import WorkspaceRunSuperseded, pending_workspace_run_intent

logger = logging.getLogger(__name__)

_ACTIONLESS_AUTO_RESUME_MARKER = "AUTO-RESUME-ONCE(actionless)"
_ACTIONLESS_AUTO_RESUME_NUDGE = (
    "<system-reminder>\n"
    f"{_ACTIONLESS_AUTO_RESUME_MARKER}: host is resuming this autonomous build "
    "once after an actionless pause. Continue from the first unfinished plan "
    "step now: call a concrete tool that changes or verifies the deliverable, "
    "or call `finish` if the work is genuinely complete. Do not wait for a "
    "human response.\n"
    "</system-reminder>"
)

_CONCLUDED_STATUSES = frozenset(
    {
        ConversationStatus.FINISHED,
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.PAUSED,
    }
)
_RUN_PARKED_STATUSES = frozenset(
    {
        ConversationStatus.IDLE,
        ConversationStatus.PAUSED,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    }
)
_KERNEL_UNPIN_STATUSES = frozenset(
    {
        ConversationStatus.FINISHED,
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.IDLE,
    }
)
_ENDED_UNSEALED = frozenset(
    {
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.PAUSED,
        ConversationStatus.IDLE,
    }
)
_MAX_NONTERMINAL_REKICKS = 3
_ACTIONLESS_AUTO_RESUME_SEGMENT_CAP = 3


def _latest_status_event(events: list[Event]) -> StatusEvent | None:
    return next((event for event in reversed(events) if isinstance(event, StatusEvent)), None)


def _actionless_auto_resume_attempted(events: list[Event]) -> bool:
    successful = signals.successful_action_ids(events)
    for event in reversed(events):
        if signals.is_successful_productive_action(event, successful):
            return False
        if (
            isinstance(event, MessageEvent)
            and event.source == EventSource.ENVIRONMENT
            and _ACTIONLESS_AUTO_RESUME_MARKER in (event.message.content or "")
        ):
            return True
    return False


def _actionless_auto_resume_total(events: list[Event]) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, MessageEvent)
        and event.source == EventSource.ENVIRONMENT
        and _ACTIONLESS_AUTO_RESUME_MARKER in (event.message.content or "")
    )


def _latest_productive_seq(events: list[Event]) -> int:
    successful = signals.successful_action_ids(events)
    return max(
        (
            event.seq or 0
            for event in events
            if signals.is_successful_productive_action(event, successful)
        ),
        default=0,
    )


def _latest_user_seq(events: list[Event]) -> int | None:
    return max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, MessageEvent) and event.source == EventSource.USER
        ),
        default=None,
    )


class RunFinalizer:
    """Generation- and durable-authority-safe run terminalization."""

    def __init__(
        self,
        registry: RunRegistry,
        ingress: RunIngressLedger,
        recovery: RunRecoveryLedger,
        kernels: KernelPinRegistry,
        store: SqliteEventStore,
        workspace: RunWorkspacePort,
        lifecycle: LifecycleCommandService,
        reentry: RunReentryPort,
        policy: RunSurfacePolicy,
        observability: RunObservabilityPort,
    ) -> None:
        self._registry, self._ingress = registry, ingress
        self._recovery, self._kernels = recovery, kernels
        self._store, self._workspace, self._lifecycle = store, workspace, lifecycle
        self._reentry, self._policy = reentry, policy
        self._observability = observability

    async def rekick_unadmitted(self, conversation_id: str) -> None:
        try:
            events = await self._store.get_events(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception("superseded-run handoff could not read %s", conversation_id)
            return
        if workspace_run_intent_admission_required(events):
            self._reentry.kick(conversation_id)

    async def rekick_stranded(self, conversation_id: str) -> None:
        try:
            events = agent_view_consistent_events(await self._store.get_events(conversation_id))
        except Exception:  # noqa: BLE001
            logger.exception("post-terminal re-kick could not read events for %s", conversation_id)
            return
        if (
            not signals.has_unprocessed_user_message(events)
            and pending_workspace_run_intent(events) is None
        ):
            return
        latest_user_seq = _latest_user_seq(events)
        if latest_user_seq is None:
            return
        claimed = self._ingress.claimed_user_seq(conversation_id)
        if claimed is not None and latest_user_seq <= claimed:
            return
        if not self._recovery.claim_terminal_rekick(conversation_id, latest_user_seq):
            return
        logger.info(
            "stranded follow-up on %s (user seq %s after a terminal conclusion) — re-kicking",
            conversation_id,
            latest_user_seq,
        )
        await self._workspace.rekick_stranded_followup(conversation_id, latest_user_seq)

    async def finalize_clean(
        self,
        conversation_id: str,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None:
        try:
            if not await self._owns_authority(
                conversation_id,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            ):
                return
            state = await self._store.get_state(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception("clean-return finalize could not read state for %s", conversation_id)
            return
        status = state.execution_status
        self._recovery.record_status(conversation_id, status)
        if await self._auto_resume_actionless(conversation_id, status, generation):
            return
        if status in _CONCLUDED_STATUSES or status in _RUN_PARKED_STATUSES:
            await self._finish_healthy(conversation_id, status, generation)
            return
        await self._recover_nonterminal(
            conversation_id,
            status,
            generation,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def terminalize_crash(
        self,
        conversation_id: str,
        error: BaseException,
        generation: int | None = None,
        *,
        agent_view_id: str | None = None,
        run_intent_id: str | None = None,
    ) -> None:
        try:
            if not await self._owns_authority(
                conversation_id,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            ):
                return
            state = await self._store.get_state(conversation_id)
            if not self._registry.generation_is_current(conversation_id, generation):
                return
            if state.execution_status in _CONCLUDED_STATUSES:
                return
            detail = f"uncaught {type(error).__name__}: {error}"[:200]
            logger.error("run task for %s crashed: %s", conversation_id, detail, exc_info=error)
            stored = await self._append_status(
                conversation_id,
                ConversationStatus.ERROR,
                detail,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            )
            if not stored:
                return
            self._observability.emit_audit(conversation_id, ConversationStatus.ERROR)
            self._kernels.clear_if_current(conversation_id, generation, self._registry)
            await self._observability.emit_reminder(
                conversation_id,
                f"The run stopped on an unexpected internal error ({type(error).__name__}). "
                "It has been recorded as failed; you can retry or adjust the task.",
            )
            await self.rekick_stranded(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception("crash terminalization failed for %s", conversation_id)

    async def _owns_authority(
        self,
        conversation_id: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        if agent_view_id is None and run_intent_id is None:
            return True
        return await self._workspace.run_authority_is_current(
            conversation_id,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def _finish_healthy(
        self,
        conversation_id: str,
        status: ConversationStatus,
        generation: int | None,
    ) -> None:
        self._recovery.reset_stall(conversation_id)
        if status in _CONCLUDED_STATUSES or status is ConversationStatus.IDLE:
            self._observability.emit_audit(conversation_id, status)
        if status not in _KERNEL_UNPIN_STATUSES:
            return
        self._kernels.clear_if_current(conversation_id, generation, self._registry)
        await self.rekick_stranded(conversation_id)

    async def _recover_nonterminal(
        self,
        conversation_id: str,
        status: ConversationStatus,
        generation: int | None,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> None:
        try:
            events = agent_view_consistent_events(await self._store.get_events(conversation_id))
            latest_progress = _latest_productive_seq(events)
        except Exception:  # noqa: BLE001
            latest_progress = None
        attempts = self._recovery.record_progress(conversation_id, latest_progress)
        if attempts < _MAX_NONTERMINAL_REKICKS:
            attempt = self._recovery.increment_stall(conversation_id)
            logger.warning(
                "run task for %s returned at %s without concluding; re-kicking "
                "(silent-stall recovery, no-progress attempt %d/%d)",
                conversation_id,
                status.value,
                attempt,
                _MAX_NONTERMINAL_REKICKS,
            )
            self._reentry.kick(conversation_id)
            return
        self._recovery.reset_stall(conversation_id)
        await self._terminalize_stall(
            conversation_id,
            generation,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )

    async def _terminalize_stall(
        self,
        conversation_id: str,
        generation: int | None,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> None:
        try:
            state = await self._store.get_state(conversation_id)
            if state.execution_status in _CONCLUDED_STATUSES:
                return
            logger.error(
                "run task for %s wedged at RUNNING after re-kick; marking STUCK",
                conversation_id,
            )
            stored = await self._append_status(
                conversation_id,
                ConversationStatus.STUCK,
                "loop ended without reaching a terminal state",
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            )
            if not stored:
                return
            self._observability.emit_audit(conversation_id, ConversationStatus.STUCK)
            self._kernels.clear_if_current(conversation_id, generation, self._registry)
            await self._observability.emit_reminder(
                conversation_id,
                "The run ended without completing or stopping cleanly (the model turn "
                "produced no actionable response). It's been marked stuck — send a "
                "message to steer it and continue.",
            )
            await self.rekick_stranded(conversation_id)
        except Exception:  # noqa: BLE001
            logger.exception("stall terminalization failed for %s", conversation_id)

    async def _auto_resume_actionless(
        self,
        conversation_id: str,
        status: ConversationStatus,
        generation: int | None,
    ) -> bool:
        if status is not ConversationStatus.PAUSED:
            return False
        if not self._registry.generation_is_current(conversation_id, generation):
            return False
        if not self._policy.is_build_surface(self._policy.surface(conversation_id)):
            return False
        if not self._policy.autonomous(conversation_id):
            return False
        try:
            events = agent_view_consistent_events(await self._store.get_events(conversation_id))
        except Exception:  # noqa: BLE001
            logger.exception("actionless auto-resume could not read events for %s", conversation_id)
            return False
        latest_status = _latest_status_event(events)
        if (
            latest_status is None
            or latest_status.status is not ConversationStatus.PAUSED
            or latest_status.detail != "actionless"
        ):
            return False
        pause_count = signals.actionless_pause_count_current_execution_segment(events)
        if pause_count == 1:
            if _actionless_auto_resume_attempted(events):
                return False
            if _actionless_auto_resume_total(events) >= _ACTIONLESS_AUTO_RESUME_SEGMENT_CAP:
                return False
            await self._store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=_ACTIONLESS_AUTO_RESUME_NUDGE),
                ),
            )
        elif pause_count == 2:
            if not signals.should_synthesize_finish_after_actionless_pauses(events):
                return False
        else:
            return False
        result = await self._reentry.resume_conversation(conversation_id)
        if result.get("ok") is True:
            return True
        logger.warning(
            "actionless auto-resume for %s was not accepted: %s",
            conversation_id,
            result,
        )
        return False

    async def _append_status(
        self,
        conversation_id: str,
        status: ConversationStatus,
        detail: str,
        *,
        agent_view_id: str | None,
        run_intent_id: str | None,
    ) -> bool:
        stored = await self._lifecycle.append_task_status_if_current(
            conversation_id,
            LifecycleCommandService.build_status(status, detail=detail),
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )
        return stored is not None


class RunSupervisor:
    """Synchronous task registration plus done-callback dispatch."""

    def __init__(
        self,
        registry: RunRegistry,
        authorities: RunAuthorityLedger,
        ingress: RunIngressLedger,
        resources: RunResourceRegistry,
        workspace: RunWorkspacePort,
        finalizer: RunCompletionPort,
    ) -> None:
        self._registry, self._authorities = registry, authorities
        self._ingress, self._resources = ingress, resources
        self._workspace, self._finalizer = workspace, finalizer

    def create_task(
        self,
        conversation_id: str,
        loop: AgentLoop | Awaitable[AgentLoop] | None = None,
        *,
        loop_factory: Callable[[], Awaitable[AgentLoop]] | None = None,
        claimed_user_seq: int | None = None,
        expected_run_intent_id: str | None = None,
        task_context: contextvars.Context | None = None,
        completion_managed_externally: bool = False,
    ) -> tuple[RunTask, int]:
        if self._registry.active_task(conversation_id) is not None:
            raise RuntimeError("conversation already has a live run task")
        if (loop is None) == (loop_factory is None):
            raise ValueError("provide exactly one of loop or loop_factory")

        async def run() -> ConversationState:
            selected = loop_factory() if loop_factory is not None else loop
            assert selected is not None
            selected_loop = (
                selected
                if isinstance(selected, AgentLoop)
                else await cast(Awaitable[AgentLoop], selected)
            )
            return await self._workspace.run_after_admission(
                conversation_id,
                selected_loop,
                expected_run_intent_id=expected_run_intent_id,
            )

        task = asyncio.create_task(run(), context=task_context)
        generation = self._registry.register_task(conversation_id, task)
        if claimed_user_seq is not None:
            self._ingress.claim_user_seq(conversation_id, claimed_user_seq)
        if not completion_managed_externally:
            task.add_done_callback(
                lambda completed: self.on_task_done(conversation_id, completed, generation)
            )
        return task, generation

    def on_task_done(
        self,
        conversation_id: str,
        task: RunTask,
        generation: int | None = None,
    ) -> None:
        agent_view_id, run_intent_id = self._authorities.discard(task)
        owned = self._registry.complete_task(conversation_id, task)
        if owned:
            self._workspace.clear_run_claim(conversation_id)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            self._schedule(
                self._finalizer.finalize_clean(
                    conversation_id,
                    generation,
                    agent_view_id=agent_view_id,
                    run_intent_id=run_intent_id,
                )
            )
            return
        if isinstance(error, (AgentViewSuperseded, WorkspaceRunSuperseded)):
            self._schedule(self._finalizer.rekick_unadmitted(conversation_id))
            return
        self._schedule(
            self._finalizer.terminalize_crash(
                conversation_id,
                error,
                generation,
                agent_view_id=agent_view_id,
                run_intent_id=run_intent_id,
            )
        )

    async def cancel_runs(self) -> None:
        await self._registry.close()

    async def close_resources(self) -> None:
        await self._resources.close()
        self._authorities.clear()
        self._ingress.clear()

    @staticmethod
    def _schedule(awaitable: Awaitable[object]) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.ensure_future(awaitable)


class RunPersistenceSupervisor:
    """Surface-aware preflight, loop execution, and workspace persistence."""

    def __init__(
        self,
        registry: RunRegistry,
        authorities: RunAuthorityLedger,
        store: SqliteEventStore,
        lifecycle: LifecycleCommandService,
        policy: RunSurfacePolicy,
        observability: RunObservabilityPort,
        preflight: RunPreflightPort,
        persistence: RunPersistencePort,
        deep_research: DeepResearchRunPort,
    ) -> None:
        self._registry, self._authorities = registry, authorities
        self._store, self._lifecycle = store, lifecycle
        self._policy, self._observability = policy, observability
        self._preflight, self._persistence = preflight, persistence
        self._deep_research = deep_research

    async def run(self, conversation_id: str, loop: AgentLoop) -> ConversationState:
        surface = self._policy.surface(conversation_id)
        current = asyncio.current_task()
        task = cast(RunTask | None, current)
        registered = task is not None and self._registry.owns_task(conversation_id, task)
        run_intent_id = await self._bind_initial_authority(
            conversation_id,
            task,
            registered=registered,
            surface=surface,
        )
        if surface == "deep_research":
            await self._deep_research.run_deep_research(conversation_id)
            return await self._store.get_state(conversation_id)
        failed_state = await self._preflight_run(
            conversation_id,
            run_intent_id=run_intent_id,
            surface=surface,
        )
        if failed_state is not None:
            return failed_state
        if self._policy.is_build_surface(surface):
            async with self._persistence.workspace_lock(conversation_id):
                await self._persistence.rehydrate(conversation_id)
                await self._persistence.rematerialize_uploads(conversation_id)
        state = await self._run_loop(
            conversation_id,
            loop,
            task=task,
            registered=registered,
            run_intent_id=run_intent_id,
        )
        await self._snapshot_ended(conversation_id, surface=surface)
        return state

    async def _bind_initial_authority(
        self,
        conversation_id: str,
        task: RunTask | None,
        *,
        registered: bool,
        surface: str,
    ) -> str | None:
        if not registered or task is None or not self._policy.is_build_surface(surface):
            return None
        agent_view_id, run_intent_id, _strict = await self._lifecycle.resolve_current_authority(
            conversation_id
        )
        self._authorities.bind(
            task,
            agent_view_id=agent_view_id,
            run_intent_id=run_intent_id,
        )
        return run_intent_id

    async def _preflight_run(
        self,
        conversation_id: str,
        *,
        run_intent_id: str | None,
        surface: str,
    ) -> ConversationState | None:
        reason = await self._preflight.driver_failure(conversation_id)
        if reason is not None:
            return await self._preflight_failure(
                conversation_id,
                reason,
                "The run did not start — check the model's endpoint and API key in "
                "Settings, then send a message to retry.",
                run_intent_id=run_intent_id,
            )
        if not self._policy.is_build_surface(surface):
            return None
        reason = await self._preflight.sandbox_failure(conversation_id)
        if reason is None:
            return None
        return await self._preflight_failure(
            conversation_id,
            reason,
            "The run did not start — check the sandbox backend's host/connection in "
            "Settings → Sandbox, then send a message to retry.",
            run_intent_id=run_intent_id,
        )

    async def _preflight_failure(
        self,
        conversation_id: str,
        reason: str,
        guidance: str,
        *,
        run_intent_id: str | None,
    ) -> ConversationState:
        stored = await self._lifecycle.append_task_status_if_current(
            conversation_id,
            LifecycleCommandService.build_status(
                ConversationStatus.ERROR,
                detail=reason[:200],
            ),
            agent_view_id=None,
            run_intent_id=run_intent_id,
        )
        if stored is not None:
            self._observability.emit_audit(conversation_id, ConversationStatus.ERROR)
            await self._observability.emit_reminder(
                conversation_id,
                f"{reason} {guidance}",
            )
        return await self._store.get_state(conversation_id)

    async def _run_loop(
        self,
        conversation_id: str,
        loop: AgentLoop,
        *,
        task: RunTask | None,
        registered: bool,
        run_intent_id: str | None,
    ) -> ConversationState:
        try:
            return await loop.run()
        finally:
            if registered and task is not None:
                self._authorities.bind(
                    task,
                    agent_view_id=AgentLoop._current_agent_view_id(),
                    run_intent_id=run_intent_id,
                )

    async def _snapshot_ended(
        self,
        conversation_id: str,
        *,
        surface: str,
    ) -> None:
        ended_state = await self._store.get_state(conversation_id)
        status = ended_state.execution_status
        if not self._policy.is_build_surface(surface):
            return
        if status not in _ENDED_UNSEALED | {ConversationStatus.FINISHED}:
            return
        self._observability.emit_audit(conversation_id, status)
        if status is ConversationStatus.FINISHED:
            return
        async with self._persistence.workspace_lock(conversation_id):
            await self._persistence.snapshot(conversation_id, trigger=status.value)
