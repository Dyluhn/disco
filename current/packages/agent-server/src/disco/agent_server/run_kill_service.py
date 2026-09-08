"""Durable, generation-safe hard-kill coordination."""

from __future__ import annotations

import asyncio
import contextlib

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    ReportEvent,
    StatusEvent,
)
from disco.core.loop import signals
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.deep_research import RESEARCH_TRACE_ACTIONS

from .lifecycle_command_service import (
    LifecycleAuthority,
    LifecycleCommandService,
)
from .run_registry import LoopRegistry, RunRegistry, RunResourceRegistry, RunTask
from .workspace_service import WorkspaceCoordinator


def _completed_research_report(events: list[Event]) -> bool:
    """A committed report wins over a late Kill for that same run.

    Report and FINISHED commit together. A later user input or RUNNING status
    belongs to new work and must remain killable, including before preflight.
    """
    index = next(
        (i for i in range(len(events) - 1, -1, -1) if isinstance(events[i], StatusEvent)), None
    )
    if index is None or index == 0:
        return False
    status = events[index]
    return (
        isinstance(status, StatusEvent)
        and status.status is ConversationStatus.FINISHED
        and isinstance(events[index - 1], ReportEvent)
        and not any(
            isinstance(event, MessageEvent) and event.source is EventSource.USER
            for event in events[index + 1 :]
        )
    )


class RunKillService:
    """Own hard-kill authorization, action closure, and resource teardown."""

    def __init__(
        self,
        store: SqliteEventStore,
        workspace: WorkspaceCoordinator,
        lifecycle: LifecycleCommandService,
        runs: RunRegistry,
        loops: LoopRegistry,
        resources: RunResourceRegistry,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._lifecycle = lifecycle
        self._runs = runs
        self._loops = loops
        self._resources = resources

    def _local_target_changed(
        self,
        conversation_id: str,
        generation: int | None,
        issued_task: RunTask | None,
    ) -> bool:
        _ = generation
        current_task = self._runs.task(conversation_id)
        return current_task is not None and current_task is not issued_task

    async def _cancel_issued_task(
        self,
        conversation_id: str,
        issued_task: RunTask | None,
    ) -> None:
        self._runs.detach_task_if_owned(conversation_id, issued_task)
        if issued_task is None or issued_task.done():
            return
        issued_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await issued_task

    async def _close_dangling_actions_locked(
        self,
        conversation_id: str,
        authority: LifecycleAuthority | None = None,
    ) -> list[AgentErrorEvent]:
        """Pair admitted open actions while both workspace fences are held.

        "Admitted" means a tool call a model proposed and an executor ran, whose
        result the kill interrupted — that pairing is what keeps the model's
        next View from carrying an open call, and its sentence is about a
        workspace or an external system whose state is now unknown. Deep
        research's ActionEvents are none of those things: the server appends
        them to narrate its own run (`RESEARCH_TRACE_ACTIONS`), nothing ever
        pairs them, and no model reads them. Closing them produced one "its
        outcome is UNKNOWN; re-verify the workspace" error per turn counter on
        every killed research run.
        """

        if not self._workspace.lock(conversation_id).locked():
            raise RuntimeError("kill action closure requires the workspace fence")
        self._workspace._require_process_fence_locked(conversation_id)
        events = await self._store.get_events(conversation_id)
        paired = signals._paired_action_ids(events)
        captured_view_id = authority[0] if authority is not None else None
        strict = authority[2] if authority is not None else False
        errors: list[Event] = []
        for event in events:
            if not isinstance(event, ActionEvent) or event.id in paired:
                continue
            if event.tool_call.tool_name in RESEARCH_TRACE_ACTIONS:
                continue
            if strict and (captured_view_id is None or event.agent_view_id != captured_view_id):
                continue
            errors.append(
                AgentErrorEvent(
                    id=f"evt_kill_cancelled_{event.id}",
                    error="cancelled",
                    detail=(
                        "Kill interrupted this admitted action before its result was "
                        "recorded. Its outcome is UNKNOWN; re-verify the workspace or "
                        "external system before retrying."
                    ),
                    action_id=event.id,
                    tool_call_id=event.tool_call.call_id,
                    agent_view_id=event.agent_view_id,
                )
            )
        if not errors:
            return []
        stored = await self._store.append_many(conversation_id, errors)
        return [event for event in stored if isinstance(event, AgentErrorEvent)]

    async def _teardown_resources_locked(self, conversation_id: str) -> None:
        executor = self._resources.pop_executor(conversation_id)
        pending = self._resources.pop_pending_session(conversation_id)
        self._loops.forget(conversation_id)
        # Auto-suspend detaches before releasing the workspace fence, then reclaims
        # outside it. A hard kill that acquires the fence afterward must join that
        # owned teardown before it can truthfully publish IDLE or return success.
        await self._resources._await_reclaims(conversation_id)
        if executor is not None:
            await executor.kill()
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()

    async def _kill_locked(
        self,
        conversation_id: str,
        generation: int | None,
        issued_task: RunTask | None,
        authority: LifecycleAuthority,
    ) -> None:
        if self._local_target_changed(conversation_id, generation, issued_task):
            return
        events = await self._store.get_events(conversation_id)
        if not self._lifecycle.authority_is_current_for_events(events, authority):
            return
        if self._lifecycle.authority_already_killed(
            events, authority
        ) or _completed_research_report(events):
            return

        await self._close_dangling_actions_locked(conversation_id, authority)
        await self._teardown_resources_locked(conversation_id)

        if self._local_target_changed(conversation_id, generation, issued_task):
            return
        events = await self._store.get_events(conversation_id)
        if not self._lifecycle.authority_is_current_for_events(events, authority):
            return
        if self._lifecycle.authority_already_killed(
            events, authority
        ) or _completed_research_report(events):
            return
        agent_view_id, run_intent_id, strict = authority
        await self._lifecycle.append_status_locked(
            conversation_id,
            LifecycleCommandService.build_status(
                ConversationStatus.IDLE,
                detail="killed",
                agent_view_id=agent_view_id if strict else None,
                run_intent_id=(run_intent_id if strict and agent_view_id is None else None),
            ),
        )

    async def kill(self, conversation_id: str, generation: int | None = None) -> None:
        """Cancel one observed run and publish IDLE only for its durable authority."""

        issued_task = self._runs.task(conversation_id)
        authority = await self._lifecycle.resolve_current_authority(conversation_id)
        if self._local_target_changed(conversation_id, generation, issued_task):
            return
        await self._cancel_issued_task(conversation_id, issued_task)
        if self._local_target_changed(conversation_id, generation, issued_task):
            return
        async with self._workspace.lock(conversation_id):
            async with self._workspace.interprocess_mutation_fence(conversation_id):
                await self._kill_locked(
                    conversation_id,
                    generation,
                    issued_task,
                    authority,
                )
