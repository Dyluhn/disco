"""Recover research only from boundaries committed in the conversation event log."""

from __future__ import annotations

import asyncio
from typing import Any

from disco.core import (
    ActionEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ReportEvent,
    ResearchCheckpointEvent,
    StatusEvent,
)
from disco.retrieval.deep_research._recovery_state import RecoveryCheckpoint
from disco.retrieval.deep_research._report_event import fit_research_checkpoint_event
from disco.retrieval.deep_research.recovery import (
    RECOVERY_ACTION,
    ResearchRecoveryError,
    read_checkpoint,
)

from .lifecycle_command_service import LifecycleCommandService


def _current_execution(events: list[Event]) -> list[Event]:
    start = max(
        (
            index
            for index, event in enumerate(events)
            if isinstance(event, StatusEvent) and event.status is ConversationStatus.RUNNING
        ),
        default=0,
    )
    return events[start:]


def _resume_event(
    saved: RecoveryCheckpoint,
    reference: dict[str, Any],
    *,
    fallback: bool,
) -> ResearchCheckpointEvent:
    event = ResearchCheckpointEvent(
        query=saved.query,
        passages=[passage.model_dump(mode="json") for passage in saved.state.passages],
        all_hits=[hit.model_dump(mode="json") for hit in saved.state.all_hits],
        trail=saved.state.trail,
        completed_queries=list(
            dict.fromkeys(
                str(row["query"])
                for row in saved.state.trail
                if row.get("kind") == "search" and row.get("query")
            )
        ),
        depth_tier=saved.depth_tier,
        recency_window=saved.recency_window,
        meta={
            "research_recovery_ref": reference,
            "recovery_reason": "server_restart",
            "recovery_stage": saved.stage,
            "recovery_fallback": fallback,
            "recovery_source_count": len(saved.state.passages),
            "turns_remaining": saved.bound.max_research_turns - saved.state.turns_charged,
            "sources_remaining": saved.bound.max_sources - len(saved.state.budget_ids),
        },
    )
    return fit_research_checkpoint_event(event)


async def recover_research_run(
    conversation_id: str,
    events: list[Event],
    commands: LifecycleCommandService,
) -> bool:
    """True when research recovery owns the orphan's terminal/resumable outcome."""
    current = _current_execution(events)
    # Compatibility with a pre-atomic publisher killed after its report append.
    # The report is already committed: finish it instead of running it a second time.
    if any(isinstance(event, ReportEvent) for event in current):
        await commands.append_current_run_transition(
            conversation_id,
            [],
            commands.build_status(ConversationStatus.FINISHED),
            expected_statuses=frozenset({ConversationStatus.RUNNING}),
        )
        return True
    references = [
        event.tool_call.arguments
        for event in current
        if isinstance(event, ActionEvent) and event.tool_call.tool_name == RECOVERY_ACTION
    ]
    if not references:
        return False
    run_id = references[-1].get("run_id")
    # Resume opens another RUNNING interval but retains the research run_id.
    # An older committed boundary from that same run is still valid fallback;
    # a different research run in the conversation must never be resurrected.
    references = [
        event.tool_call.arguments
        for event in events
        if isinstance(event, ActionEvent)
        and event.tool_call.tool_name == RECOVERY_ACTION
        and event.tool_call.arguments.get("run_id") == run_id
    ]
    for index, reference in enumerate(reversed(references)):
        if reference.get("run_id") != run_id:
            break
        try:
            saved = await asyncio.to_thread(read_checkpoint, conversation_id, reference)
        except ResearchRecoveryError:
            continue
        checkpoint = _resume_event(saved, reference, fallback=index > 0)
        note = (
            "Research was interrupted by a server restart. Resume restores the saved "
            "evidence and remaining work budget. The request in flight may be repeated."
        )
        if index > 0:
            note += (
                " The latest snapshot was unreadable; an earlier committed boundary was recovered."
            )
        await commands.append_current_run_transition(
            conversation_id,
            [
                checkpoint,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=note),
                ),
            ],
            commands.build_status(
                ConversationStatus.PAUSED, detail="research interrupted; checkpoint recovered"
            ),
            expected_statuses=frozenset({ConversationStatus.RUNNING}),
        )
        return True
    await commands.append_current_run_transition(
        conversation_id,
        [
            ErrorEvent(
                code="deep_research_recovery_unavailable",
                detail=(
                    "The saved research checkpoints are unreadable or use an unsupported version. "
                    "The original files were preserved; recovery cannot safely continue."
                ),
            )
        ],
        commands.build_status(ConversationStatus.ERROR, detail="research checkpoint unavailable"),
        expected_statuses=frozenset({ConversationStatus.RUNNING}),
    )
    return True
