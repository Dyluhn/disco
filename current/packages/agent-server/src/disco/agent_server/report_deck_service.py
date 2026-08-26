"""Managed execution for the direct research-report → deck handoff."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from disco.core import ConversationState, ConversationStatus, ReportEvent, ToolCall
from disco.core.store.sqlite import SqliteEventStore

from .report_deck_handoff import ReportDeckJob, serialize_report_for_deck
from .routes.report_decks import DirectReportDeckStartPort
from .runtime import ConversationRuntime


class ReportDeckRunService:
    """Run one exact deck tool through the existing supervisor and lifecycle."""

    def __init__(self, store: SqliteEventStore, runtime: ConversationRuntime) -> None:
        self._store = store
        self._runtime = runtime

    def start_port(self) -> DirectReportDeckStartPort:
        return DirectReportDeckStartPort(
            self._store,
            self._runtime,
            persist_source=self._persist_source,
            schedule_tool=self._schedule_tool,
        )

    async def _persist_source(
        self,
        target_id: str,
        _source_id: str,
        job: ReportDeckJob,
    ) -> None:
        # Copy the typed report into the target conversation with a fresh event
        # identity.  The bounded prompt projection stays host-only and cannot be
        # replaced by client-authored request content.
        copied = ReportEvent(
            **job.report.model_dump(
                exclude={"id", "timestamp", "seq", "agent_view_id", "meta"}
            )
        )
        await self._store.append(target_id, copied)

    async def _schedule_tool(
        self,
        target_id: str,
        _job: ReportDeckJob,
        tool_call: ToolCall,
    ) -> None:
        cancellation = self._runtime._cancellations.begin(target_id)
        self._runtime._run_supervisor.create_operation_task(
            target_id,
            lambda: self._run_tool(target_id, tool_call, cancellation),
        )

    async def _run_tool(
        self,
        conversation_id: str,
        tool_call: ToolCall,
        cancellation: asyncio.Event,
    ) -> ConversationState:
        tool_task: asyncio.Task[object] | None = None
        stop_task: asyncio.Task[bool] | None = None
        try:
            # The typed source is durable. Build its bounded prompt projection
            # only for the active operation, so a scheduling failure cannot
            # strand a load-bearing process-local cache entry.
            events = await self._store.get_events(conversation_id)
            report = next(
                (event for event in reversed(events) if isinstance(event, ReportEvent)),
                None,
            )
            if report is None:
                raise RuntimeError("report deck source is missing")
            self._runtime._loop_factory.set_source_report(
                conversation_id,
                serialize_report_for_deck(report),
            )
            await self._runtime._lifecycle_commands.append_status(
                conversation_id,
                ConversationStatus.RUNNING,
                detail="generating deck from research report",
            )

            tool_task = asyncio.create_task(
                self._runtime.execute_disco_tool(conversation_id, tool_call)
            )
            stop_task = asyncio.create_task(cancellation.wait())
            done, _ = await asyncio.wait(
                (tool_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation.is_set() or stop_task in done and tool_task not in done:
                tool_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tool_task
                await self._runtime._lifecycle_commands.append_status(
                    conversation_id,
                    ConversationStatus.IDLE,
                    detail="cancelled",
                )
                return await self._store.get_state(conversation_id)

            result = await tool_task
            if cancellation.is_set():
                await self._runtime._lifecycle_commands.append_status(
                    conversation_id,
                    ConversationStatus.IDLE,
                    detail="cancelled",
                )
                return await self._store.get_state(conversation_id)
            if not result.success:
                raise RuntimeError(result.error or "slides_generate failed")
            await self._runtime._lifecycle_commands.commit_finished_workspace(
                conversation_id,
                self._runtime._lifecycle_commands.build_status(
                    ConversationStatus.FINISHED,
                    detail="structured deck generated",
                ),
            )
            return await self._store.get_state(conversation_id)
        finally:
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


__all__ = ["ReportDeckRunService"]
