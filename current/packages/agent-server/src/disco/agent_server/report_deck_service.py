"""Managed execution for the direct research-report → deck handoff."""

from __future__ import annotations

import json
from typing import Any

from disco.core import (
    ReportDeckInvocationEvent,
    ReportEvent,
    ToolCall,
)
from disco.core.contract.export_render import EXPORT_RENDER_KEY, check_export_render
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.builtin._deck_schema import AuthoredDeck

from ._report_deck_execution import (
    ReportDeckValidationError,
    _ReportDeckExecutionMixin,
)
from .report_deck_handoff import ReportDeckJob
from .routes.report_decks import DirectReportDeckStartPort
from .runtime import ConversationRuntime


class ReportDeckRunService(_ReportDeckExecutionMixin):
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
        filename, base_name, sidecar, slide_count = self._validate_deck_contract(
            result.structured
        )
        await self._validate_deck_files(
            conversation_id, filename, base_name, sidecar, slide_count
        )

    @staticmethod
    def _valid_deck_fields(structured: dict[str, Any]) -> bool:
        filename = structured.get("filename")
        return (
            isinstance(filename, str)
            and filename.endswith(".pptx")
            and isinstance(structured.get("base_name"), str)
            and isinstance(structured.get("editable_source"), str)
            and isinstance(structured.get("slide_count"), int)
            and structured["slide_count"] >= 2
            and structured.get("format") == "pptx"
            and structured.get("renderer") == "pptx-native"
        )

    @staticmethod
    def _validate_deck_contract(structured: Any) -> tuple[str, str, str, int]:
        if not isinstance(structured, dict):
            raise ReportDeckValidationError("slides_generate returned no structured deck facts")
        filename = structured.get("filename")
        base_name = structured.get("base_name")
        sidecar = structured.get("editable_source")
        slide_count = structured.get("slide_count")
        canonical = ReportDeckRunService._valid_deck_fields(structured)
        stamped = structured.get(EXPORT_RENDER_KEY)
        if not canonical:
            raise ReportDeckValidationError(
                "slides_generate did not return the canonical PPTX contract"
            )
        if not isinstance(stamped, dict) or not bool(stamped.get("ok")):
            raise ReportDeckValidationError("slides_generate returned no passing PPTX render facts")
        assert (
            isinstance(filename, str)
            and isinstance(base_name, str)
            and isinstance(sidecar, str)
            and isinstance(slide_count, int)
        )
        return filename, base_name, sidecar, slide_count

    async def _validate_deck_files(
        self,
        conversation_id: str,
        filename: str,
        base_name: str,
        sidecar: str,
        slide_count: int,
    ) -> None:
        sandbox = self._sandbox_for(conversation_id)

        async def read(path: str) -> bytes:
            try:
                raw = await sandbox.read_file(path)
            except Exception as exc:  # noqa: BLE001 - turn missing output into a typed failure
                raise ReportDeckValidationError(f"deck artifact is missing: {path}") from exc
            return raw.encode("utf-8") if isinstance(raw, str) else raw

        pptx_facts = check_export_render(
            "pptx", await read(filename), declared_units=slide_count, declared_exact=True
        )
        if not pptx_facts.ok:
            raise ReportDeckValidationError(f"canonical PPTX render failed: {pptx_facts.detail}")
        try:
            authored = AuthoredDeck.model_validate(
                json.loads((await read(sidecar)).decode("utf-8"))
            )
        except Exception as exc:  # noqa: BLE001 - invalid sidecar is a terminal contract failure
            raise ReportDeckValidationError("authored deck JSON is invalid") from exc
        if len(authored.slides) < 2:
            raise ReportDeckValidationError("authored deck JSON contains no complete slide set")
        html_text = (await read(base_name + ".html")).decode("utf-8", "replace")
        if "brand-wordmark" not in html_text or "data-slide-id" not in html_text:
            raise ReportDeckValidationError(
                "branded HTML preview is missing canonical brand/render markers"
            )
        html_facts = check_export_render(
            "html", text=html_text, declared_units=slide_count, declared_exact=True
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



__all__ = ["ReportDeckRunService"]
