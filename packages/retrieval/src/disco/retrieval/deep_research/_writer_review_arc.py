"""One recoverable review and scoped repair, bound to the exact report draft."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from disco.core import LLMMessage
from disco.core.llm import LLMRouter

from ..models import Passage
from ._citation_aliases import CitationAliases
from ._progress_events import emit_review, emit_rework
from ._review_protocol import ReviewBudget, review_actions
from ._stop import ShouldCancelFn, await_stoppable, raise_if_stopped
from ._writer_checkpoint import WriterCheckpoint, WriterCheckpointFn
from ._writer_parts import FinalReport
from ._writer_review import _Review, _review, _rework


@dataclass
class _ArcContext:
    router: LLMRouter
    base_messages: list[LLMMessage]
    query: str
    coverage: dict[str, Any]
    research_trail: Sequence[dict[str, Any]]
    by_id: dict[str, Passage]
    nli: Any
    aliases: CitationAliases
    untested: Sequence[str]
    max_tokens: int
    conversation_id: str | None
    emit: Any
    should_cancel: ShouldCancelFn
    checkpoint: WriterCheckpointFn | None

    async def save(self, state: WriterCheckpoint) -> None:
        state.draft_sha256 = hashlib.sha256(state.final.markdown.encode()).hexdigest()
        if self.checkpoint is not None:
            await self.checkpoint(state.model_copy(deep=True))


async def _grade(state: WriterCheckpoint, ctx: _ArcContext, *, final_check: bool) -> None:
    async def save_decision() -> None:
        await ctx.save(state)

    await emit_review(ctx.emit, 1, 1)
    review = await _review(
        ctx.router,
        state.final,
        query=ctx.query,
        coverage=ctx.coverage,
        trail=[*ctx.research_trail, *state.trail],
        by_id=ctx.by_id,
        nli=ctx.nli,
        aliases=ctx.aliases,
        untested=ctx.untested,
        conversation_id=ctx.conversation_id,
        budget=state.budget,
        reserve=0 if final_check else state.budget.final_reserve,
        final_check=final_check,
        should_cancel=ctx.should_cancel,
        checkpoint=save_decision,
        notes=state.source_notes,
    )
    state.findings, state.review_outcome, state.review_trail = (
        review.findings,
        review.outcome,
        review.trail,
    )
    state.trail.append(review.trail)
    state.stage = "repair" if review.findings and not state.repair_started else "complete"
    if (
        not final_check
        and not state.budget.complete
        and state.budget.remaining
        and state.budget.provider_error is None
    ):
        # Resolve an incomplete verdict before deciding whether prose needs repair.
        # Reuse the reserved decision, exact draft and existing feedback; validated
        # findings remain available if the reviewer cannot resolve the conflict.
        state.stage = "final_check"
        state.budget.messages.append(
            LLMMessage(role="user", content=review_actions(state.budget.remaining))
        )
    await ctx.save(state)


async def _repair(state: WriterCheckpoint, ctx: _ArcContext) -> None:
    if state.repair_started:
        # The provider may have received the committed request before a crash.
        # Preserve its draft and findings; never reset the one-repair allowance.
        spent = any(row.get("kind") == "report_rework" for row in state.trail)
        state.trail.append(
            {
                "kind": "report_rework_exhausted" if spent else "report_rework_interrupted",
                "applied": [],
            }
        )
        state.stage, state.review_outcome = "complete", "incomplete"
        await ctx.save(state)
        return
    await emit_rework(ctx.emit, 1, 1)
    before = state.final.markdown
    state.repair_started = True
    await ctx.save(state)
    messages = list(ctx.base_messages)
    if state.budget.inspections:
        messages.append(LLMMessage(role="user", content=state.budget.evidence_context()))
    state.final, rework = await await_stoppable(
        _rework(
            ctx.router,
            messages,
            state.final,
            state.findings,
            aliases=ctx.aliases,
            pool_ids=set(ctx.by_id),
            max_tokens=ctx.max_tokens,
            conversation_id=ctx.conversation_id,
        ),
        ctx.should_cancel,
        boundary="report_rework",
    )
    rework["requested_findings"] = [asdict(finding) for finding in state.findings]
    state.trail.append(rework)
    state.stage = "final_check" if state.final.markdown != before else "complete"
    await ctx.save(state)


async def _review_arc(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    final: FinalReport,
    *,
    query: str,
    coverage: dict[str, Any],
    research_trail: Sequence[dict[str, Any]],
    by_id: dict[str, Passage],
    nli: Any,
    aliases: CitationAliases,
    untested: Sequence[str],
    max_tokens: int,
    conversation_id: str | None,
    emit: Any,
    should_cancel: ShouldCancelFn,
    review_decisions: int = 4,
    final_reserve: int | None = None,
    checkpoint: WriterCheckpointFn | None = None,
    resume: WriterCheckpoint | None = None,
) -> tuple[FinalReport, _Review, list[dict[str, Any]]]:
    ctx = _ArcContext(
        router,
        base_messages,
        query,
        coverage,
        research_trail,
        by_id,
        nli,
        aliases,
        untested,
        max_tokens,
        conversation_id,
        emit,
        should_cancel,
        checkpoint,
    )
    state = (
        resume.model_copy(deep=True)
        if resume is not None
        else WriterCheckpoint(
            final=final,
            draft_sha256=hashlib.sha256(final.markdown.encode()).hexdigest(),
            stage="review",
            budget=ReviewBudget(
                review_decisions,
                final_reserve=(2 if review_decisions >= 4 else 1)
                if final_reserve is None
                else final_reserve,
            ),
        )
    )
    await ctx.save(state)
    # A grade spends the finite review allowance; repair is reserved only once.
    # Re-entering final_check can resolve protocol feedback before that repair,
    # but cannot reopen the repair allowance after prose has been changed.
    while state.stage != "complete":
        if state.stage == "repair":
            raise_if_stopped(should_cancel, boundary="report_rework")
            await _repair(state, ctx)
        else:
            final_check = state.stage == "final_check"
            raise_if_stopped(
                should_cancel, boundary="report_final" if final_check else "report_review"
            )
            await _grade(state, ctx, final_check=final_check)
    raise_if_stopped(should_cancel, boundary="report_final")
    if state.review_outcome == "verdict" and (state.findings or state.review_trail.get("unplaced")):
        state.review_outcome = "incomplete"
    await ctx.save(state)
    return (
        state.final,
        _Review(state.findings, state.review_outcome, state.review_trail),
        state.trail,
    )
