"""Whole-report writing, bounded evidence review, and one scoped prose repair.

One writer receives the research pool and writes the summary and headed body.
Provider-truncated drafts can continue within a shared bound; a malformed
report gets one structural re-ask. The model owns the prose and organization.

Review combines deterministic citation/structure checks with a fixed-rubric
model assessment. NLI disagreements about cited claims are advisory. The
reviewer can inspect retained source spans and must account for consequential
claims using source-bound evidence records. A shared decision allowance covers
inspections, malformed replies, and verdicts, reserving a final decision for
changed prose. These records establish provenance, not proof of factual truth.

One rework receives the parts with findings and returns whole replacements.
The final artifact is checked again after a change; exhausted, unavailable, or
unresolved review remains visible beside the saved report. Unsupported details
must be corrected or qualified in the prose; a notice alone does not make an
answer acceptable. Final NLI uncertainty is also disclosed verbatim.

Committed writer boundaries retain the draft, findings, source inspections,
and remaining review allowance. Resume verifies input identity, skips finished
work, and never reuses a verdict for a different draft. A reserved repair whose
result was lost preserves the known draft and findings as incomplete.

The writer sees short citation aliases. Every output crosses back to canonical
passage IDs before validation, persistence, or rendering. Empty or ambiguous
evidence pools fail before generation.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from disco.core import LLMMessage, ReportSection
from disco.core.llm import (
    LLMRouter,
)

from ..models import Passage
from ._citation_aliases import NO_ALIASES, CitationAliases, citation_aliases
from ._progress_events import emit_continuation, emit_section_done
from ._source_reading import SourceNotes, notes_blocks, read_sources, source_reading_trail
from ._stop import ShouldCancelFn, await_stoppable, raise_if_stopped
from ._synthesis_parts import _join_continuation_text
from ._task_context import accepted_steering_context, research_reference_context
from ._untested_angles import format_untested_angles, untested_angles
from ._verifier_health import apply_verifier_degradation, verifier_failure_count
from ._writer_checkpoint import WriterCheckpoint, WriterCheckpointFn, writer_input_signature
from ._writer_evidence import _EVIDENCE_CHAR_BUDGET
from ._writer_findings import (  # noqa: F401
    KIND_ABSENCE,
    KIND_PROCESS,
    KIND_REPEATED,
    KIND_REVIEW,
    KIND_UNRESOLVED_CITATION,
    KIND_UNSUPPORTED,
    SUMMARY_WHERE,
    Finding,
    absence_findings,
    named_parts,
    process_language_findings,
    render_findings,
    repetition_findings,
    review_findings,
    unresolved_citation_findings,
    unsupported_findings,
)
from ._writer_parts import (  # noqa: F401
    SUMMARY_SECTION_TITLES,
    FinalReport,
    _coverage_instruction,
    _last_paragraph_block,
    cited_ids,
    confidence_from_claims,
    count_prose_words,
    decode_review_json,
    finalize_report,
    format_evidence_pool,
    has_report_prose,
    normalize_citations,
    parse_report_parts,
    review_call,
    review_max_tokens,
    think_headroom_tokens,
)
from ._writer_prompts import (  # noqa: F401
    CONTINUATION_INSTRUCTION,
    EMPTY_REPORT_REASK,
    EMPTY_REVIEW_REASK,
    EMPTY_REWORK_REASK,
    FRAGMENT_REWORK_REASK,
    MALFORMED_REVIEW_REASK,
    REPORT_PROMPT,
    REWORK_INSTRUCTION,
    REWORK_SUMMARY_HEADING,
    STRUCTURE_NO_SECTIONS,
    STRUCTURE_NO_SUMMARY,
    STRUCTURE_REASK,
    WRITING_RULES,
)
from ._writer_review import (  # noqa: F401
    _REVIEW_SYSTEM,
    _SELF_REVIEW_PROMPT,
    RESEARCH_REPORT_RUBRIC,
    REVIEW_OUTCOME_UNAVAILABLE,
    REVIEW_OUTCOME_VERDICT,
    REVIEW_STAGES,
    _canonical_payload,
    _grounding_review,
    _inspect_metadata,
    _model_review,
    _not_a_rewrite,
    _record_writer_io,
    _Review,
    _review,
    _rework,
    _rework_call,
    _rework_reask,
    _splice_rework,
    _writer_call,
)
from ._writer_review_arc import _review_arc
from .agent import ResearchOutcome
from .depth import DepthBound
from .evidence import EVIDENCE_SYSTEM_PROMPT
from .report_compiler import ReportCompilationError, collect_claim_ledger

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# Mild sampling for prose variety on the draft; the continuation and the rework
# are deterministic repairs of that draft.
_DRAFT_TEMPERATURE = 0.4
# How many times a CUT-OFF report is continued before it ships as it stands.
# Each continuation gets the full per-call ceiling: it is finishing a report,
# and the writer decides how much is left to say.
_MAX_CONTINUATIONS = 3
# Fallback body provision for a tier that assigns none.
_DEFAULT_WRITING_BUDGET_TOKENS = 8_000


@dataclass
class WrittenReport:
    summary: str
    summary_cited_passage_ids: list[str]
    sections: list[ReportSection]
    claims: list[dict[str, Any]]
    unsupported_count: int
    review_notes: list[str] = field(default_factory=list)  # soft editorial notes
    # Sentences the final claim ledger could not support against the passages
    # they cite. The report shipped with them; they are declared verbatim.
    unverified_sentences: list[str] = field(default_factory=list)
    # The writer's own trail rows, appended to the run's trail by the engine.
    trail: list[dict[str, Any]] = field(default_factory=list)
    verifier_failures: int = 0  # grounding checks that no-op'd (see _verifier_health)
    # Whether the fixed-rubric MODEL review returned a verdict on the draft or
    # never did. The deterministic findings drive the rework either way.
    review_outcome: str = REVIEW_OUTCOME_VERDICT


# ---------------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------------


def _writer_max_tokens(bound: DepthBound) -> int:
    """Provision visible report text plus finite reasoning capacity.

    Draft, continuation, structure repair and rework explicitly request reasoning
    through the provider adapter. The same ceiling is reused for these calls.
    """
    body = bound.writing_budget_tokens or _DEFAULT_WRITING_BUDGET_TOKENS
    return body + think_headroom_tokens()


def _recency_preamble(recency_window: Literal["month", "week"] | None) -> str:
    if recency_window is None:
        return ""
    label = "month" if recency_window == "month" else "week"
    return (
        f"Today's date is {datetime.date.today().isoformat()}. This research "
        f"focused on the PAST {label.upper()}; reflect that recency in the "
        "report.\n\n"
    )


def _report_instruction(
    query: str,
    outcome: ResearchOutcome,
    recency_window: Literal["month", "week"] | None,
    aliases: CitationAliases = NO_ALIASES,
    notes: dict[str, SourceNotes] | None = None,
    char_budget: int = _EVIDENCE_CHAR_BUDGET,
) -> str:
    return _recency_preamble(recency_window) + REPORT_PROMPT.format(
        query=query,
        accepted_steering=(
            research_reference_context(outcome.trail) + accepted_steering_context(outcome.trail)
        ),
        brief=outcome.brief or "(none recorded)",
        coverage_instruction=_coverage_instruction(outcome.coverage),
        untested_angles=format_untested_angles(untested_angles(outcome.trail)),
        evidence=format_evidence_pool(
            outcome.passages,
            aliases=aliases.alias_by_passage,
            query=query,
            coverage=outcome.coverage,
            trail=outcome.trail,
            notes=notes_blocks(notes or {}),
            char_budget=char_budget,
        ),
        citation_ids=aliases.prompt_contract(),
        rules=WRITING_RULES,
    )


@dataclass
class _Draft:
    """The report as written so far, with the transport fact the next step
    reads: whether the provider cut the last reply off."""

    markdown: str
    cut_off: bool


async def _write_draft(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    *,
    max_tokens: int,
    conversation_id: str | None,
) -> _Draft:
    """The first whole-report call.

    A provider-level SUCCESS with no prose is a transport-adjacent failure,
    not a verdict on the report: it is re-asked ONCE, naming what came back and
    what is required. A second empty reply is the run's error.
    """
    markdown, response = await _writer_call(
        router,
        base_messages,
        max_tokens=max_tokens,
        temperature=_DRAFT_TEMPERATURE,
        conversation_id=conversation_id,
        stage="report_draft",
        attempt=1,
    )
    if not has_report_prose(markdown):
        markdown, response = await _writer_call(
            router,
            [*base_messages, LLMMessage(role="user", content=EMPTY_REPORT_REASK)],
            max_tokens=max_tokens,
            temperature=_DRAFT_TEMPERATURE,
            conversation_id=conversation_id,
            stage="report_draft_reask",
            attempt=1,
        )
    if not has_report_prose(markdown):
        raise ReportCompilationError("report writer returned no prose")
    return _Draft(markdown=markdown, cut_off=response.finish_reason == "length")


async def _continue_if_cut_off(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    draft: _Draft,
    *,
    rounds_used: int,
    max_tokens: int,
    conversation_id: str | None,
    emit: EmitFn,
    should_cancel: ShouldCancelFn,
) -> tuple[_Draft, int]:
    """Continue a CUT-OFF report from exactly where it stopped.

    The one condition that sends the writer back for more is the provider's
    own ``finish_reason == "length"``: the reply hit the output ceiling
    mid-flow. The report so far is replayed as the writer's own assistant turn
    with its last paragraph quoted, and the continuation is appended through
    the same join the section synthesizer uses — nothing is rewritten. A reply
    that ends cleanly ends the loop; so does an empty one, and so does the
    bound. A report still cut off after the bound ships as it stands, with the
    trail saying so.

    Returns the draft and the number of rounds used in total (``rounds_used``
    plus this call's), so a second cut-off — after the structure re-ask —
    draws on the same bound.
    """
    rounds = rounds_used
    markdown = draft.markdown
    cut_off = draft.cut_off
    while cut_off and rounds < _MAX_CONTINUATIONS:
        raise_if_stopped(should_cancel, boundary="continuation")
        rounds += 1
        await emit_continuation(emit, rounds, _MAX_CONTINUATIONS)
        extra, response = await _writer_call(
            router,
            [
                *base_messages,
                LLMMessage(role="assistant", content=markdown.rstrip()),
                LLMMessage(
                    role="user",
                    content=CONTINUATION_INSTRUCTION.format(
                        last_paragraph=_last_paragraph_block(markdown)
                    ),
                ),
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            conversation_id=conversation_id,
            stage="report_continuation",
            attempt=rounds,
        )
        if not extra.strip():
            break
        markdown = _join_continuation_text(markdown, extra.lstrip())
        cut_off = response.finish_reason == "length"
    return _Draft(markdown=markdown, cut_off=cut_off), rounds


def _structure_problem(markdown: str) -> str | None:
    """The one structural fault a draft can have, named for the re-ask, or
    None when the draft has the shape the reader needs."""
    parsed = parse_report_parts(markdown)
    if not parsed.sections:
        return STRUCTURE_NO_SECTIONS
    if not parsed.summary.strip():
        return STRUCTURE_NO_SUMMARY
    return None


# ---------------------------------------------------------------------------
# Review: three readers, one findings list.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Final assembly.
# ---------------------------------------------------------------------------


async def _finalize(
    final: FinalReport,
    *,
    by_id: dict[str, Passage],
    nli: Any,
    emit: EmitFn,
) -> WrittenReport:
    pool_ids = set(by_id)
    if not final.summary.strip():
        raise ReportCompilationError("final report has no executive summary")
    if not final.sections:
        raise ReportCompilationError("final report has no headed sections")
    summary = final.summary
    sections = [
        ReportSection(
            id=f"r{index}",
            title=title,
            markdown=markdown,
            cited_passage_ids=sorted({item for item in cited_ids(markdown) if item in pool_ids}),
        )
        for index, (title, markdown) in enumerate(final.sections)
    ]
    ledger = await asyncio.to_thread(
        collect_claim_ledger, sections, list(by_id.values()), nli, summary
    )
    claims_by_section: dict[str, list[dict[str, Any]]] = {}
    for claim in ledger:
        claims_by_section.setdefault(claim.section_id, []).append(claim.to_event_dict())
    finalized: list[ReportSection] = []
    all_claims: list[dict[str, Any]] = []
    all_claims.extend(claims_by_section.get("summary", []))
    unsupported_total = sum(claim["verdict"] == "unsupported" for claim in all_claims)
    for section in sections:
        section_claims = claims_by_section.get(section.id, [])
        confidence, unsupported = confidence_from_claims(section_claims)
        finalized.append(
            section.model_copy(update={"confidence": confidence, "unsupported_count": unsupported})
        )
        unsupported_total += unsupported
        all_claims.extend(section_claims)
    for index, section in enumerate(finalized):
        await emit_section_done(emit, section_id=section.id, title=section.title, done=index + 1)
    return WrittenReport(
        summary=summary,
        summary_cited_passage_ids=sorted({item for item in cited_ids(summary) if item in pool_ids}),
        sections=finalized,
        claims=all_claims,
        unsupported_count=unsupported_total,
        unverified_sentences=[
            claim.text for claim in ledger if claim.verdict == "unsupported" and claim.text.strip()
        ],
    )


def _require_usable_pool(outcome: ResearchOutcome) -> None:
    """Refuse to write from a pool the citations cannot resolve against.

    Each of these is a broken RUN, not a bad report: nothing gathered, a
    passage with no id, or two passages sharing one. Checked before the first
    token is spent, because every prompt below teaches the model these ids.
    """
    if not outcome.passages:
        raise ReportCompilationError("research produced no usable evidence")
    passage_ids = [passage.id for passage in outcome.passages]
    if any(not passage_id.strip() for passage_id in passage_ids):
        raise ReportCompilationError("research evidence contains a blank passage id")
    if len(set(passage_ids)) != len(passage_ids):
        raise ReportCompilationError("research evidence contains duplicate passage ids")


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


async def _draft_arc(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    *,
    max_tokens: int,
    conversation_id: str | None,
    emit: EmitFn,
    should_cancel: ShouldCancelFn,
) -> tuple[_Draft, int, list[dict[str, Any]]]:
    trail: list[dict[str, Any]] = []
    await emit("phase", {"phase": "writing"})
    draft = await _write_draft(
        router, base_messages, max_tokens=max_tokens, conversation_id=conversation_id
    )
    raise_if_stopped(should_cancel, boundary="report_draft")
    draft, rounds = await _continue_if_cut_off(
        router,
        base_messages,
        draft,
        rounds_used=0,
        max_tokens=max_tokens,
        conversation_id=conversation_id,
        emit=emit,
        should_cancel=should_cancel,
    )
    raise_if_stopped(should_cancel, boundary="report_structure")
    problem = _structure_problem(draft.markdown)
    if problem is not None:
        trail.append({"kind": "report_structure_reask", "problem": problem})
        markdown, response = await _writer_call(
            router,
            [
                *base_messages,
                LLMMessage(role="assistant", content=draft.markdown.rstrip()),
                LLMMessage(role="user", content=STRUCTURE_REASK.format(problem=problem)),
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            conversation_id=conversation_id,
            stage="report_structure_reask",
            attempt=1,
        )
        if has_report_prose(markdown):
            draft, rounds = await _continue_if_cut_off(
                router,
                base_messages,
                _Draft(markdown=markdown, cut_off=response.finish_reason == "length"),
                rounds_used=rounds,
                max_tokens=max_tokens,
                conversation_id=conversation_id,
                emit=emit,
                should_cancel=should_cancel,
            )
        problem = _structure_problem(draft.markdown)
        if problem is not None:
            raise ReportCompilationError(f"report has no usable structure: {problem}")
    if rounds:
        trail.append(
            {"kind": "report_continuation", "rounds": rounds, "complete": not draft.cut_off}
        )
    raise_if_stopped(should_cancel, boundary="report_review")
    return draft, rounds, trail


async def _prepare_writer_draft(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    resume: WriterCheckpoint | None,
    signature: str,
    *,
    aliases: CitationAliases,
    pool_ids: set[str],
    max_tokens: int,
    conversation_id: str | None,
    emit: EmitFn,
    should_cancel: ShouldCancelFn,
) -> tuple[FinalReport, list[dict[str, Any]], WriterCheckpoint | None]:
    if resume is not None and resume.input_sha256 == signature:
        return resume.final, list(resume.draft_trail), resume
    draft, _rounds, trail = await await_stoppable(
        _draft_arc(
            router,
            base_messages,
            max_tokens=max_tokens,
            conversation_id=conversation_id,
            emit=emit,
            should_cancel=should_cancel,
        ),
        should_cancel,
        boundary="report_draft",
    )
    final = finalize_report(aliases.to_canonical(draft.markdown), pool_ids)
    if resume is not None:
        # New user guidance authorizes a revised draft, not a fresh review budget.
        resume = resume.model_copy(deep=True)
        resume.trail.append(
            {
                "kind": "report_task_revision",
                "prior_input_sha256": resume.input_sha256,
                "input_sha256": signature,
                "decisions_used": resume.budget.used,
            }
        )
        resume.final, resume.stage = final, "review"
        resume.draft_sha256 = hashlib.sha256(final.markdown.encode()).hexdigest()
        resume.findings, resume.review_trail, resume.review_outcome = [], {}, "unavailable"
        resume.budget.draft_sha256 = ""
        trail = [*resume.draft_trail, *trail]
    return final, trail, resume


def _writer_signatures(
    query: str, outcome: ResearchOutcome, resume: WriterCheckpoint | None
) -> tuple[str, str]:
    """The writer and research input digests, refused when the pool or a
    resumed checkpoint cannot be trusted to describe THIS run's evidence."""
    _require_usable_pool(outcome)
    steering = accepted_steering_context(outcome.trail)
    signature = writer_input_signature(query, outcome.passages, outcome.coverage, steering)
    research = writer_input_signature("", outcome.passages, outcome.coverage, steering)
    if resume is not None and resume.research_sha256 != research:
        raise ReportCompilationError("writer checkpoint inputs differ from the retained research")
    return signature, research


async def _writer_opening(
    query: str,
    outcome: ResearchOutcome,
    resume: WriterCheckpoint | None,
    aliases: CitationAliases,
    recency_window: Literal["month", "week"] | None,
    *,
    router: LLMRouter,
    conversation_id: str | None,
    emit: EmitFn,
    should_cancel: ShouldCancelFn,
    evidence_char_budget: int,
) -> tuple[list[LLMMessage], dict[str, SourceNotes], list[dict[str, Any]]]:
    """What the writer reads, and the reading that produced it.

    Every source too long for the pool to render whole is read first, in its
    own call, because that is the only place there is room to read it. Its
    validated quotes then go into the SAME share of the pool the keyword
    windows were spending, so the 220,000-character budget is unchanged and
    what the writer sees inside it is evidence proved against the source.

    A resume whose checkpoint already carries notes reuses them and earns no
    new trail rows: those are already in its retained draft trail.
    """
    notes, trail = (dict(resume.source_notes), []) if resume is not None else ({}, [])
    if not notes:
        notes = await read_sources(
            outcome.passages,
            query=query,
            router=router,
            conversation_id=conversation_id,
            emit=emit,
            should_cancel=should_cancel,
        )
        trail = source_reading_trail(notes)
    return (
        [
            LLMMessage(role="system", content=EVIDENCE_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=_report_instruction(
                    query, outcome, recency_window, aliases, notes, evidence_char_budget
                ),
            ),
        ],
        notes,
        trail,
    )


def _record_review_notes(
    written: WrittenReport, review: _Review, trail: list[dict[str, Any]]
) -> None:
    for entry in trail:
        for title in entry.get("missing_coverage", []):
            written.review_notes.append(f"Required coverage was not added: {title}.")
    for entry in [*trail, review.trail]:
        kind = entry.get("kind")
        if kind == "report_rework":
            reason, label = entry.get("unavailable"), "Report repair unavailable"
            unresolved = entry.get("unresolved")
            if isinstance(unresolved, str) and unresolved:
                note = f"Report repair unresolved: {unresolved}"
                if note not in written.review_notes:
                    written.review_notes.append(note)
        elif kind in {"report_review", "report_final_check"}:
            reason, label = entry.get("provider_error"), "Report review unavailable"
        else:
            continue
        if isinstance(reason, str) and reason:
            note = f"{label}: {reason}"
            if note not in written.review_notes:
                written.review_notes.append(note)
    if review.trail.get("unplaced"):
        written.review_notes.append(
            f"{review.trail['unplaced']} review finding(s) could not be matched to a repair."
        )
    written.review_notes.extend(review.trail.get("assessment_errors", []))
    for finding in review.findings:
        written.review_notes.append(
            f"Unresolved review finding in {finding.where}: {finding.quote} — {finding.fix}"
        )


async def write_report(
    query: str,
    outcome: ResearchOutcome,
    *,
    router: LLMRouter,
    nli: Any,
    bound: DepthBound,
    emit: EmitFn,
    recency_window: Literal["month", "week"] | None = None,
    conversation_id: str | None = None,
    should_cancel: ShouldCancelFn = None,
    checkpoint: WriterCheckpointFn | None = None,
    resume: WriterCheckpoint | None = None,
) -> WrittenReport:
    """Write the whole report from the research outcome.

    One whole-report call, continued by the same writer only while the
    provider cuts it off; one structural re-ask if the shape is wrong; one
    bounded review of the rendered whole; one rework scoped to the parts with
    findings; and a final check of changed prose. Sentences the claim ledger
    cannot support are declared in
    ``unverified_sentences``; ``review_outcome`` records whether the model
    reviewer returned a verdict; ``trail`` carries what each step did.

    ``should_cancel`` is read during requests and between model calls (``_stop``)
    and raises ``ResearchStopped``: the answer to Stop is the caller's research
    checkpoint, never a report assembled from what happened to be written.
    ``checkpoint`` commits review/repair boundaries, and ``resume`` restores
    their exact draft and remaining work after verifying the research inputs.
    """
    signature, research_signature = _writer_signatures(query, outcome, resume)
    by_id = {passage.id: passage for passage in outcome.passages}
    pool_ids = set(by_id)
    # The ids the WRITER is taught: `s1`…`sN` over the pool in the order the
    # evidence block is rendered in. Every prompt this run sends speaks them and
    # every span of writer output is translated back out of them, so opaque
    # canonical handles never reach a writing model (`_citation_aliases`).
    aliases = citation_aliases(outcome.passages)
    # The angles research never reached, derived once from the run's own trail.
    # They go into every generation prompt and into the absence check.
    untested = untested_angles(outcome.trail)
    # Measure the DELTA over this run: one verifier instance can be shared.
    verifier_before = verifier_failure_count(nli)
    base_messages, source_notes, notes_trail = await _writer_opening(
        query,
        outcome,
        resume,
        aliases,
        recency_window,
        router=router,
        conversation_id=conversation_id,
        emit=emit,
        should_cancel=should_cancel,
        evidence_char_budget=bound.evidence_char_budget,
    )
    max_tokens = _writer_max_tokens(bound)
    final, trail, resume = await _prepare_writer_draft(
        router,
        base_messages,
        resume,
        signature,
        aliases=aliases,
        pool_ids=pool_ids,
        max_tokens=max_tokens,
        conversation_id=conversation_id,
        emit=emit,
        should_cancel=should_cancel,
    )

    trail = [*notes_trail, *trail]

    async def save_writer(state: WriterCheckpoint) -> None:
        state.input_sha256, state.draft_trail = signature, list(trail)
        state.research_sha256, state.source_notes = research_signature, source_notes
        if checkpoint is not None:
            await checkpoint(state)

    final, review, review_trail = await _review_arc(
        router,
        base_messages,
        final,
        query=query,
        coverage=outcome.coverage,
        research_trail=outcome.trail,
        by_id=by_id,
        nli=nli,
        aliases=aliases,
        untested=untested,
        max_tokens=max_tokens,
        conversation_id=conversation_id,
        emit=emit,
        should_cancel=should_cancel,
        review_decisions=bound.review_decisions,
        checkpoint=save_writer,
        resume=resume,
    )
    trail.extend(review_trail)

    written = await _finalize(final, by_id=by_id, nli=nli, emit=emit)
    _record_review_notes(written, review, review_trail)
    if written.unverified_sentences:
        trail.append({"kind": "report_unverified", "sentences": len(written.unverified_sentences)})
    written.trail = trail
    written.review_outcome = review.outcome
    apply_verifier_degradation(written, before=verifier_before, nli=nli)
    return written


__all__ = [
    "RESEARCH_REPORT_RUBRIC",
    "REVIEW_OUTCOME_UNAVAILABLE",
    "REVIEW_OUTCOME_VERDICT",
    "REVIEW_STAGES",
    "WrittenReport",
    "write_report",
]
