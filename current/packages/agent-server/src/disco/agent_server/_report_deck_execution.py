"""Execution mixin for direct report-to-deck runs."""

# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from disco.core import (
    ConversationState,
    ConversationStatus,
    ReportDeckInvocationEvent,
    ReportEvent,
    ToolCall,
)

from .report_deck_handoff import ReportDeckJob, serialize_report_for_deck


class ReportDeckValidationError(RuntimeError):
    """The direct handoff did not produce the complete canonical deck contract."""


class _ReportDeckExecutionMixin:
    async def _load_job(self, conversation_id: str) -> tuple[Any, ReportDeckJob]:
        events = await self._store.get_events(conversation_id)
        invocation = next(
            (event for event in reversed(events) if isinstance(event, ReportDeckInvocationEvent)),
            None,
        )
        if invocation is None:
            raise ReportDeckValidationError("deck invocation fact is missing")
        report = next((event for event in reversed(events) if isinstance(event, ReportEvent)), None)
        if report is None:
            raise ReportDeckValidationError("report deck source is missing")
        return invocation, ReportDeckJob(
            report=report,
            goal=invocation.goal,
            filename=invocation.filename,
            format=invocation.format,
        )

    async def _begin_job(self, conversation_id: str, invocation: Any, job: ReportDeckJob) -> None:
        await self._append_invocation(
            conversation_id, invocation.source_conversation_id, job, status="running"
        )
        self._runtime._loop_factory.set_source_report(
            conversation_id, serialize_report_for_deck(job.report)
        )
        await self._runtime._lifecycle_commands.append_status(
            conversation_id,
            ConversationStatus.RUNNING,
            detail="generating deck from research report",
        )

    async def _wait_for_tool(
        self,
        conversation_id: str,
        invocation: Any,
        job: ReportDeckJob,
        tool_task: asyncio.Task[object],
        stop_task: asyncio.Task[bool],
        cancellation: asyncio.Event,
    ) -> Any | None:
        done, _ = await asyncio.wait((tool_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        stopped = cancellation.is_set() or (stop_task in done and tool_task not in done)
        if stopped or cancellation.is_set():
            tool_task.cancel()
            with suppress(asyncio.CancelledError):
                await tool_task
            await self._append_invocation(
                conversation_id,
                invocation.source_conversation_id,
                job,
                status="error",
                detail="cancelled",
            )
            await self._runtime._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.IDLE, detail="cancelled"
            )
            return None
        return await tool_task

    async def _record_tool_failure(self, conversation_id: str, exc: Exception) -> None:
        with suppress(Exception):
            events = await self._store.get_events(conversation_id)
            invocation = next(
                (
                    event
                    for event in reversed(events)
                    if isinstance(event, ReportDeckInvocationEvent)
                ),
                None,
            )
            report = next(
                (event for event in reversed(events) if isinstance(event, ReportEvent)), None
            )
            if invocation is not None and report is not None:
                await self._append_invocation(
                    conversation_id,
                    invocation.source_conversation_id,
                    ReportDeckJob(
                        report=report,
                        goal=invocation.goal,
                        filename=invocation.filename,
                        format=invocation.format,
                    ),
                    status="error",
                    detail=str(exc)[:1_000],
                )

    async def _cleanup_tool_tasks(
        self,
        conversation_id: str,
        tool_task: asyncio.Task[object] | None,
        stop_task: asyncio.Task[bool] | None,
    ) -> None:
        if stop_task is not None:
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task
        if tool_task is not None and not tool_task.done():
            tool_task.cancel()
            with suppress(asyncio.CancelledError):
                await tool_task
        self._runtime._loop_factory.clear_source_report(conversation_id)
        self._runtime._cancellations.clear(conversation_id)

    async def _run_tool(
        self, conversation_id: str, tool_call: ToolCall, cancellation: asyncio.Event
    ) -> ConversationState:
        tool_task: asyncio.Task[object] | None = None
        stop_task: asyncio.Task[bool] | None = None
        try:
            invocation, job = await self._load_job(conversation_id)
            await self._begin_job(conversation_id, invocation, job)
            tool_task = asyncio.create_task(
                self._runtime.execute_disco_tool(conversation_id, tool_call)
            )
            stop_task = asyncio.create_task(cancellation.wait())
            result = await self._wait_for_tool(
                conversation_id, invocation, job, tool_task, stop_task, cancellation
            )
            if result is None:
                return await self._store.get_state(conversation_id)
            if not result.success:
                raise RuntimeError(result.error or "slides_generate failed")
            await self._validate_deck_result(conversation_id, result)
            await self._runtime._lifecycle_commands.commit_finished_workspace(
                conversation_id,
                self._runtime._lifecycle_commands.build_status(
                    ConversationStatus.FINISHED, detail="structured deck generated"
                ),
            )
            await self._append_invocation(
                conversation_id, invocation.source_conversation_id, job, status="completed"
            )
            return await self._store.get_state(conversation_id)
        except Exception as exc:
            if not isinstance(exc, asyncio.CancelledError):
                await self._record_tool_failure(conversation_id, exc)
            raise
        finally:
            await self._cleanup_tool_tasks(conversation_id, tool_task, stop_task)

    async def _recover_conversation(self, conversation_id: str) -> bool:
        events = await self._store.get_events(conversation_id)
        invocation = next(
            (event for event in reversed(events) if isinstance(event, ReportDeckInvocationEvent)),
            None,
        )
        if invocation is None or invocation.status not in {"queued", "running"}:
            return False
        state = await self._store.get_state(conversation_id)
        report = next((event for event in reversed(events) if isinstance(event, ReportEvent)), None)
        if report is None:
            await self._store.append(
                conversation_id,
                ReportDeckInvocationEvent(
                    source_conversation_id=invocation.source_conversation_id,
                    goal=invocation.goal,
                    filename=invocation.filename,
                    format=invocation.format,
                    status="error",
                    detail="recovery could not find the copied report source",
                ),
            )
            return False
        if state.execution_status in {
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
            ConversationStatus.STUCK,
        }:
            terminal_status = (
                "completed" if state.execution_status is ConversationStatus.FINISHED else "error"
            )
            detail = (
                "recovered: terminal state already finished"
                if terminal_status == "completed"
                else "recovered: deck run already terminal with an error"
            )
            await self._store.append(
                conversation_id,
                ReportDeckInvocationEvent(
                    source_conversation_id=invocation.source_conversation_id,
                    goal=invocation.goal,
                    filename=invocation.filename,
                    format=invocation.format,
                    status=terminal_status,
                    detail=detail,
                ),
            )
            return False
        registry = getattr(self._runtime._run_supervisor, "_registry", None)
        if registry is not None and registry.active_task(conversation_id) is not None:
            return False
        self._runtime.settings.set_artifact_mode(conversation_id, True)
        self._runtime.contract.set_build_kind(conversation_id, "deck")
        job = ReportDeckJob(
            report=report,
            goal=invocation.goal,
            filename=invocation.filename,
            format=invocation.format,
        )
        await self._schedule_tool(
            conversation_id,
            job,
            ToolCall(
                tool_name="slides_generate",
                arguments={"goal": job.goal, "filename": job.filename, "format": job.format},
            ),
        )
        return True

    async def _recover_pending_owner(self, owner_id: str) -> int:
        recovered = 0
        cursor: str | None = None
        while True:
            ids = await self._store.list_conversations(owner_id=owner_id, limit=200, cursor=cursor)
            if not ids:
                break
            for conversation_id in ids:
                recovered += int(await self._recover_conversation(conversation_id))
            if len(ids) < 200:
                break
            cursor = str((int(cursor) if cursor else 0) + len(ids))
        return recovered
