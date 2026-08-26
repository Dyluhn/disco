"""Managed execution for the direct research-report → deck handoff."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from disco.core import (
    ConversationState,
    ConversationStatus,
    ReportDeckInvocationEvent,
    ReportEvent,
    ToolCall,
)
from disco.core.contract.export_render import EXPORT_RENDER_KEY, check_export_render
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.builtin._deck_schema import AuthoredDeck

from .report_deck_handoff import ReportDeckJob, serialize_report_for_deck
from .routes.report_decks import DirectReportDeckStartPort
from .runtime import ConversationRuntime


class ReportDeckValidationError(RuntimeError):
    """The direct handoff did not produce the complete canonical deck contract."""


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

    async def _append_invocation(
        self,
        target_id: str,
        source_id: str,
        job: ReportDeckJob,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        await self._store.append(
            target_id,
            ReportDeckInvocationEvent(
                source_conversation_id=source_id,
                goal=job.goal,
                filename=job.filename,
                format=job.format,
                status=status,  # type: ignore[arg-type]
                detail=detail,
            ),
        )

    def _sandbox_for(self, conversation_id: str) -> Any:
        resources = getattr(self._runtime, "_run_resources", None)
        executor = resources.executor(conversation_id) if resources is not None else None
        sandbox = getattr(executor, "sandbox", None) if executor is not None else None
        if sandbox is None:
            raise ReportDeckValidationError("deck sandbox is unavailable for artifact validation")
        return sandbox

    async def _validate_deck_result(self, conversation_id: str, result: Any) -> None:
        """Validate the actual canonical outputs before allowing FINISHED.

        ``ToolResult.success`` is only the executor's claim that the tool returned.
        The terminal contract requires the on-disk PPTX, AuthoredDeck source, and
        branded HTML preview to be independently readable and render-valid.
        """
        structured = result.structured
        if not isinstance(structured, dict):
            raise ReportDeckValidationError("slides_generate returned no structured deck facts")
        filename = structured.get("filename")
        base_name = structured.get("base_name")
        sidecar = structured.get("editable_source")
        slide_count = structured.get("slide_count")
        if (
            not isinstance(filename, str)
            or not filename.endswith(".pptx")
            or not isinstance(base_name, str)
            or not isinstance(sidecar, str)
            or not isinstance(slide_count, int)
            or slide_count < 2
            or structured.get("format") != "pptx"
            or structured.get("renderer") != "pptx-native"
        ):
            raise ReportDeckValidationError(
                "slides_generate did not return the canonical PPTX contract"
            )

        stamped = structured.get(EXPORT_RENDER_KEY)
        if not isinstance(stamped, dict) or not bool(stamped.get("ok")):
            raise ReportDeckValidationError("slides_generate returned no passing PPTX render facts")

        sandbox = self._sandbox_for(conversation_id)

        async def read(path: str) -> bytes:
            try:
                raw = await sandbox.read_file(path)
            except Exception as exc:  # noqa: BLE001 - turn missing output into a typed failure
                raise ReportDeckValidationError(f"deck artifact is missing: {path}") from exc
            return raw.encode("utf-8") if isinstance(raw, str) else raw

        pptx = await read(filename)
        pptx_facts = check_export_render(
            "pptx",
            pptx,
            declared_units=slide_count,
            declared_exact=True,
        )
        if not pptx_facts.ok:
            raise ReportDeckValidationError(f"canonical PPTX render failed: {pptx_facts.detail}")

        authored_bytes = await read(sidecar)
        try:
            authored = AuthoredDeck.model_validate(json.loads(authored_bytes.decode("utf-8")))
        except Exception as exc:  # noqa: BLE001 - invalid sidecar is a terminal contract failure
            raise ReportDeckValidationError("authored deck JSON is invalid") from exc
        if len(authored.slides) < 2:
            raise ReportDeckValidationError("authored deck JSON contains no complete slide set")

        html = (base_name + ".html")
        html_text = (await read(html)).decode("utf-8", "replace")
        if "brand-wordmark" not in html_text or "data-slide-id" not in html_text:
            raise ReportDeckValidationError(
                "branded HTML preview is missing canonical brand/render markers"
            )
        html_facts = check_export_render(
            "html",
            text=html_text,
            declared_units=slide_count,
            declared_exact=True,
        )
        if not html_facts.ok:
            raise ReportDeckValidationError(f"branded HTML render failed: {html_facts.detail}")

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
            invocation = next(
                (
                    event
                    for event in reversed(events)
                    if isinstance(event, ReportDeckInvocationEvent)
                ),
                None,
            )
            if invocation is None:
                raise ReportDeckValidationError("deck invocation fact is missing")
            report = next(
                (event for event in reversed(events) if isinstance(event, ReportEvent)),
                None,
            )
            if report is None:
                raise ReportDeckValidationError("report deck source is missing")
            job = ReportDeckJob(
                report=report,
                goal=invocation.goal,
                filename=invocation.filename,
                format=invocation.format,
            )
            await self._append_invocation(
                conversation_id,
                invocation.source_conversation_id,
                job,
                status="running",
            )
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
                await self._append_invocation(
                    conversation_id,
                    invocation.source_conversation_id,
                    job,
                    status="error",
                    detail="cancelled",
                )
                await self._runtime._lifecycle_commands.append_status(
                    conversation_id,
                    ConversationStatus.IDLE,
                    detail="cancelled",
                )
                return await self._store.get_state(conversation_id)

            result = await tool_task
            if cancellation.is_set():
                await self._append_invocation(
                    conversation_id,
                    invocation.source_conversation_id,
                    job,
                    status="error",
                    detail="cancelled",
                )
                await self._runtime._lifecycle_commands.append_status(
                    conversation_id,
                    ConversationStatus.IDLE,
                    detail="cancelled",
                )
                return await self._store.get_state(conversation_id)
            if not result.success:
                raise RuntimeError(result.error or "slides_generate failed")
            await self._validate_deck_result(conversation_id, result)
            await self._runtime._lifecycle_commands.commit_finished_workspace(
                conversation_id,
                self._runtime._lifecycle_commands.build_status(
                    ConversationStatus.FINISHED,
                    detail="structured deck generated",
                ),
            )
            await self._append_invocation(
                conversation_id,
                invocation.source_conversation_id,
                job,
                status="completed",
            )
            return await self._store.get_state(conversation_id)
        except Exception as exc:
            if not isinstance(exc, asyncio.CancelledError):
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
                        (event for event in reversed(events) if isinstance(event, ReportEvent)),
                        None,
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
            raise
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

    async def recover_pending(self, *, owner_id: str | None = None) -> int:
        """Re-admit queued/running handoffs through the normal supervisor after restart.

        An explicit owner keeps focused/admin callers scoped. Startup passes no
        owner and enumerates the store's persisted owner partitions, so a deck
        launched by an authenticated non-local owner cannot be stranded.
        """

        recovered = 0
        owner_ids = (
            (owner_id,)
            if owner_id is not None
            else await self._store.list_conversation_owner_ids()
        )
        for current_owner_id in owner_ids:
            recovered += await self._recover_pending_owner(current_owner_id)
        return recovered

    async def _recover_pending_owner(self, owner_id: str) -> int:
        recovered = 0
        cursor: str | None = None
        while True:
            ids = await self._store.list_conversations(owner_id=owner_id, limit=200, cursor=cursor)
            if not ids:
                break
            for conversation_id in ids:
                events = await self._store.get_events(conversation_id)
                invocation = next(
                    (
                        event
                        for event in reversed(events)
                        if isinstance(event, ReportDeckInvocationEvent)
                    ),
                    None,
                )
                if invocation is None or invocation.status not in {"queued", "running"}:
                    continue
                state = await self._store.get_state(conversation_id)
                report = next(
                    (event for event in reversed(events) if isinstance(event, ReportEvent)),
                    None,
                )
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
                    continue
                if state.execution_status in {
                    ConversationStatus.FINISHED,
                    ConversationStatus.ERROR,
                    ConversationStatus.STUCK,
                }:
                    terminal_status = (
                        "completed"
                        if state.execution_status is ConversationStatus.FINISHED
                        else "error"
                    )
                    await self._store.append(
                        conversation_id,
                        ReportDeckInvocationEvent(
                            source_conversation_id=invocation.source_conversation_id,
                            goal=invocation.goal,
                            filename=invocation.filename,
                            format=invocation.format,
                            status=terminal_status,
                            detail=(
                                "recovered: terminal state already finished"
                                if terminal_status == "completed"
                                else "recovered: deck run already terminal with an error"
                            ),
                        ),
                    )
                    continue
                registry = getattr(self._runtime._run_supervisor, "_registry", None)
                if registry is not None and registry.active_task(conversation_id) is not None:
                    continue
                # RuntimeSettings and BuildContractService are process-local
                # projections. Rebuild them before the supervisor admits the
                # operation so restart replay cannot compose an ordinary Agent
                # executor for a deck contract.
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
                        arguments={
                            "goal": job.goal,
                            "filename": job.filename,
                            "format": job.format,
                        },
                    ),
                )
                recovered += 1
            if len(ids) < 200:
                break
            cursor = str((int(cursor) if cursor else 0) + len(ids))
        return recovered


__all__ = ["ReportDeckRunService"]
