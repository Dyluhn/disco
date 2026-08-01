"""Deep Research follow-up collaborators.

Extracted from ``DeepResearchService._follow_up_deep_research``
(PKG-11-RETRIEVAL wave 1, PY-0199): the two near-identical "answer + persist"
paths (no grounding corpus vs. grounded on the prior report's passages)
shared one completion+error-handling routine here — that de-duplication is
what actually shrinks the caller, not a relocated copy. The grounding-block
builder (the ``[[{pid}]]`` citation format) stays in
``deep_research_service.py`` itself, alongside the prompt it feeds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core import (
    ActionEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ReportEvent,
    ToolCall,
)
from disco.core.llm import CapabilityProfile, CompletionRequest, ModelRole

if TYPE_CHECKING:
    from ..deep_research_service import DeepResearchService


def find_follow_up_query(events: list[Event], prior_report: ReportEvent) -> str | None:
    """The follow-up question is the most recent USER message after the
    report."""
    last_report_seq = prior_report.seq or 0
    return next(
        (
            e.message.content
            for e in reversed(events)
            if isinstance(e, MessageEvent)
            and e.source == EventSource.USER
            and (e.seq or 0) > last_report_seq
        ),
        None,
    )


async def run_follow_up_completion(
    service: DeepResearchService, conversation_id: str, content: str
) -> bool:
    """Complete + persist the follow-up answer, or persist a failure
    reminder and take the conversation to ERROR. Returns whether it
    succeeded — shared by both the no-corpus and grounded-corpus paths,
    which previously each carried their own copy of this try/except."""
    from ..deep_research_service import _clean_model_text

    router = service._drivers.router()
    try:
        answer = await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=[LLMMessage(role="user", content=content)],
                temperature=0.0,
                max_tokens=1400,
            )
        )
    except Exception as exc:
        await service._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"Follow-up failed: {type(exc).__name__}: {exc}\n"
                        "</system-reminder>"
                    ),
                ),
            ),
        )
        await service._lifecycle_commands.append_status(
            conversation_id,
            ConversationStatus.ERROR,
        )
        return False

    await service._store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(
                role="assistant",
                content=_clean_model_text(answer.text),
            ),
        ),
    )
    return True


async def run_follow_up(
    service: DeepResearchService,
    conversation_id: str,
    events: list[Event],
    prior_report: ReportEvent,
) -> None:
    """Run a follow-up synthesis on an existing deep-research report.

    Reuses the prior report's corpus (passages) as grounding context so the
    follow-up answer is source-backed. The user's follow-up question is the
    most recent USER message after the report. The answer is emitted as
    message events (agent response) on the conversation log, and a new
    lightweight ReportEvent captures the follow-up.

    This is the RP-13 report-follow-up path — same event-stream-append
    pattern as RP-08's scheduled-task re-injection. Every step below is on
    ``service`` (an instance attribute lookup), so instance-level test
    monkeypatches keep resolving correctly regardless of this module
    boundary."""
    from ..deep_research_service import _build_follow_up_prompt

    # Find the follow-up question (most recent USER message since the report).
    follow_up_query = find_follow_up_query(events, prior_report)
    if not follow_up_query or not follow_up_query.strip():
        return

    await service._lifecycle_commands.append_status(
        conversation_id, ConversationStatus.RUNNING, detail="follow_up"
    )

    # Reuse the prior report's passages as the grounding corpus.
    passages = prior_report.passages or []
    if not passages:
        # No corpus to ground on — just answer directly.
        # WALK-12: emit a phase signal so the UI can show "Writing answer…"
        # instead of appearing frozen (the backend call is otherwise opaque).
        await service._store.append(
            conversation_id,
            ActionEvent(
                thought="Follow-up: synthesizing answer (no passage corpus)",
                tool_call=ToolCall(tool_name="phase", arguments={"phase": "synthesizing"}),
            ),
        )
        ok = await run_follow_up_completion(service, conversation_id, follow_up_query)
    else:
        # WALK-12: emit "reading" phase so the UI shows "Reading sources…"
        # while we build the grounding block from the report corpus.
        await service._store.append(
            conversation_id,
            ActionEvent(
                thought="Follow-up: reading grounding passages from report corpus",
                tool_call=ToolCall(tool_name="phase", arguments={"phase": "reading"}),
            ),
        )
        prompt = _build_follow_up_prompt(prior_report, passages, follow_up_query)

        # WALK-12: emit "synthesizing" phase so the UI shows "Writing answer…"
        await service._store.append(
            conversation_id,
            ActionEvent(
                thought="Follow-up: synthesizing grounded answer",
                tool_call=ToolCall(tool_name="phase", arguments={"phase": "synthesizing"}),
            ),
        )
        ok = await run_follow_up_completion(service, conversation_id, prompt)

    if not ok:
        return

    await service._lifecycle_commands.append_status(
        conversation_id, ConversationStatus.FINISHED, detail="follow_up_complete"
    )
