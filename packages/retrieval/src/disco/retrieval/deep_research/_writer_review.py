"""Private review and scoped rework arc for report writing."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from disco.core import LLMMessage
from disco.core.inspect import record_model_io
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMError,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from ..models import Passage
from ._citation_aliases import CitationAliases
from ._claim_review import CLAIM_REVIEW_INSTRUCTION
from ._condition_carry import condition_carry_findings
from ._output_ceiling import RESEARCH_CEILING_CAP, TurnCeiling
from ._review_assessment import _ReviewAssessment
from ._review_context import nli_context as _nli_context
from ._review_context import quality_audit_context as _quality_audit_context
from ._review_context import repair_validation_context
from ._review_prompts import _REVIEW_SYSTEM, _SELF_REVIEW_PROMPT, RESEARCH_REPORT_RUBRIC
from ._review_protocol import (
    CONCISE_REVIEW_INSTRUCTION,
    ReviewBudget,
    decode_review_decision,
    review_actions,
    review_feedback,
)
from ._source_notes import SourceNotes
from ._stop import ShouldCancelFn, await_stoppable, raise_if_stopped
from ._writer_findings import (
    KIND_ABSENCE,
    KIND_PROCESS,
    KIND_REPEATED,
    KIND_REVIEW,
    KIND_UNRESOLVED_CITATION,
    KIND_UNSUPPORTED,
    SUMMARY_WHERE,
    Finding,
    _assessment_findings,
    absence_findings,
    named_parts,
    process_language_findings,
    render_findings,
    repetition_findings,
    review_findings,
    unresolved_citation_findings,
    unsupported_findings,
)
from ._writer_parts import (
    FinalReport,
    _provider_failure,
    count_prose_words,
    has_report_prose,
    review_call,
    review_max_tokens,
)
from ._writer_prompts import (
    EMPTY_REWORK_REASK,
    FRAGMENT_REWORK_REASK,
    REWORK_INSTRUCTION,
    REWORK_SUMMARY_HEADING,
)
from ._writer_splice import _complete_rework
from ._writer_splice import _not_a_rewrite as _not_a_rewrite
from ._writer_splice import _splice_rework as _splice_rework
from .quality_audit import ClaimRecord

# Fixed review contract. ``writer`` re-exports these names for compatibility,
# but the dependency points only one way: writer -> review.
REVIEW_OUTCOME_VERDICT = "verdict"
REVIEW_OUTCOME_UNAVAILABLE = "unavailable"
REVIEW_STAGES = ("report_review", "report_review_reask")
_REVIEW_MAX_TOKENS = 4_000


def _inspect_metadata(conversation_id: str | None, stage: str) -> dict[str, str] | None:
    """Carry only bounded inspect correlation labels through the router."""
    if not conversation_id:
        return None
    return {"conversation_id": conversation_id[:256], "inspect_stage": stage[:128]}


def _record_writer_io(
    conversation_id: str | None,
    request: CompletionRequest,
    response: CompletionResponse,
    text: str,
    *,
    stage: str,
    attempt: int,
    latency_ms: int,
    declared_decision: dict[str, Any] | None = None,
    parse_error: str | None = None,
) -> None:
    routing = response.routing
    record_model_io(
        conversation_id,
        stage=stage,
        attempt=attempt,
        role=request.profile.role.value,
        model=response.model_used,
        provider=routing.provider if routing is not None else None,
        request_id=response.request_id,
        request=request.model_dump(mode="json", exclude_none=True),
        response={"text": text},
        declared_decision=declared_decision,
        latency_ms=latency_ms,
        usage=response.usage.model_dump(mode="json"),
        finish_reason=response.finish_reason,
        parse_error=parse_error,
    )


async def _writer_call(
    router: LLMRouter,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
    temperature: float,
    conversation_id: str | None,
    stage: str,
    attempt: int,
) -> tuple[str, CompletionResponse]:
    """Make one writer call and record its prose after stripping think spans."""
    request = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        # Leave reasoning selection to the configured provider/model.
        enable_thinking=None,
        metadata=_inspect_metadata(conversation_id, stage),
    )
    started = time.perf_counter()
    response = await router.complete(request)
    latency_ms = max(0, int((time.perf_counter() - started) * 1_000))
    markdown = strip_think_spans(response.text, keep_edge_whitespace=True)
    _record_writer_io(
        conversation_id,
        request,
        response,
        markdown,
        stage=stage,
        attempt=attempt,
        latency_ms=latency_ms,
    )
    return markdown, response


async def _grounding_review(
    summary: str,
    sections: Sequence[tuple[str, str]],
    by_id: dict[str, Passage],
    nli: Any,
) -> tuple[list[tuple[str, str, tuple[str, ...], tuple[str, ...]]], list[ClaimRecord]]:
    """Verify summary and body claims against the passages they cite.

    Returns the unsupported sentences as ``(where, sentence, cited_ids,
    borrowed_ids)`` rows in report order — the shape `unsupported_findings`
    reads — and every claim as a `ClaimRecord` for the reviewer's audit
    context. The summary is graded like a section so the most visible part
    of the report is held to the same bar.

    A sentence that cites nothing is verified against what its own paragraph
    cites (``borrowed_ids``): a paragraph's topic sentence and the judgment it
    draws from the cited sentences beside it are the report's synthesis, and
    the evidence around them either bears them out or contradicts them. Only
    a contradicted sentence, or one in a paragraph that cites nothing at all,
    reaches the rework as unsupported.
    """
    from ..grounding import _verify_claims

    unsupported: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    records: list[ClaimRecord] = []
    spans: list[tuple[str, str, str]] = []
    if summary.strip():
        spans.append((SUMMARY_WHERE, "summary", summary))
    spans.extend((title, title, body) for title, body in sections)
    for where, record_id, text in spans:
        claims = await asyncio.to_thread(
            partial(_verify_claims, borrow_paragraph_citations=True), text, by_id, nli
        )
        for index, claim in enumerate(claims):
            claim_body = claim.get("claim", {})
            sentence = str(claim_body.get("text", "")).strip()
            ids = tuple(str(item) for item in claim_body.get("cited_passage_ids", []))
            borrowed = tuple(str(item) for item in claim.get("borrowed_passage_ids", []))
            records.append(
                ClaimRecord(
                    claim_id=f"{record_id}:{index}",
                    text=sentence,
                    source_ids=ids,
                    section_id=record_id,
                )
            )
            if claim.get("verdict") == "unsupported":
                unsupported.append((where, sentence, ids, borrowed))
    return unsupported, records


def _deterministic_findings(
    summary: str,
    sections: Sequence[tuple[str, str]],
    pool_ids: set[str],
    *,
    valid_ids_line: str,
    untested: Sequence[str],
    notes: Mapping[str, SourceNotes] = {},  # noqa: B006 — read-only default
) -> list[Finding]:
    return [
        *unresolved_citation_findings(summary, sections, pool_ids, valid_ids_line),
        *absence_findings(summary, sections, untested),
        *repetition_findings(sections),
        *process_language_findings(summary, sections),
        *condition_carry_findings(summary, sections, notes),
    ]


async def _save_review(checkpoint: Callable[[], Awaitable[None]] | None) -> None:
    if checkpoint is not None:
        await checkpoint()


def _cached_verdict(
    budget: ReviewBudget, assess: Callable[[dict[str, Any]], list[str]] | None
) -> tuple[dict[str, Any] | None, int]:
    if budget.last_verdict is not None and assess is not None:
        assess(budget.last_verdict)
    return budget.last_verdict, budget.phase_attempts


def _start_review_phase(
    draft: str,
    audit_context: str,
    budget: ReviewBudget,
    reserve: int,
    *,
    sources: bool,
    claims: bool,
) -> None:
    digest = hashlib.sha256(draft.encode()).hexdigest()
    if budget.draft_sha256 == digest:
        return
    budget.draft_sha256 = digest
    budget.phase_attempts, budget.last_verdict, budget.complete = 0, None, False
    budget.provider_error = None
    budget.messages = [
        LLMMessage(role="system", content=_REVIEW_SYSTEM),
        LLMMessage(
            role="user",
            content=_SELF_REVIEW_PROMPT.format(
                rubric=RESEARCH_REPORT_RUBRIC, audit_context=audit_context, draft=draft
            )
            + budget.evidence_context()
            + (review_actions(max(0, budget.remaining - reserve)) if sources else "")
            + (CLAIM_REVIEW_INSTRUCTION if claims else ""),
        ),
    ]
    if budget.concise_review:
        budget.messages.append(LLMMessage(role="user", content=CONCISE_REVIEW_INSTRUCTION))


async def _model_review(
    router: LLMRouter,
    draft: str,
    audit_context: str,
    *,
    conversation_id: str | None,
    sources: dict[str, Passage] | None = None,
    budget: ReviewBudget | None = None,
    reserve: int = 0,
    final_check: bool = False,
    should_cancel: ShouldCancelFn = None,
    assess: Callable[[dict[str, Any]], list[str]] | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Spend a finite shared allowance on inspection and review decisions.

    Reserve a decision before each provider call and commit its observation
    before honoring Stop. Malformed responses receive actionable feedback
    within the same allowance. The router owns provider retries; an exhausted
    provider ends this phase without starting another retry tower. Resume
    restores the exact conversation and revalidates cached claim assessments.
    Returns the last structurally usable verdict and phase attempt count;
    assessment errors remain visible to the caller.
    """
    budget = budget if budget is not None else ReviewBudget(2)
    _start_review_phase(
        draft,
        audit_context,
        budget,
        reserve,
        sources=sources is not None,
        claims=assess is not None,
    )
    if budget.complete:
        return _cached_verdict(budget, assess)
    messages = budget.messages
    budget.ensure_output_capacity(review_max_tokens(_REVIEW_MAX_TOKENS))
    attempts = budget.phase_attempts
    while budget.remaining > reserve:
        raise_if_stopped(
            should_cancel, boundary="report_final_check" if final_check else "report_review"
        )
        stage = "report_final_check" if final_check else REVIEW_STAGES[min(attempts, 1)]
        attempts += 1
        budget.phase_attempts = attempts
        budget.used += 1
        await _save_review(checkpoint)
        try:
            call = await await_stoppable(
                review_call(
                    router,
                    messages,
                    max_tokens=budget.output_ceiling,
                    metadata=_inspect_metadata(conversation_id, stage),
                ),
                should_cancel,
                boundary=stage,
            )
        except LLMError as exc:
            budget.provider_error = _provider_failure(exc)
            budget.complete = True
            await _save_review(checkpoint)
            return _cached_verdict(budget, assess)
        payload, error, feedback, is_verdict = decode_review_decision(
            call.text, sources or {}, budget, reserve, assess, call.response.finish_reason
        )
        if is_verdict:
            budget.last_verdict = payload
        _record_writer_io(
            conversation_id,
            call.request,
            call.response,
            call.text,
            stage=stage,
            attempt=attempts,
            latency_ms=call.latency_ms,
            declared_decision=payload,
            parse_error=error,
        )
        if payload is not None and error is None and feedback is None:
            budget.complete = True
            await _save_review(checkpoint)
            raise_if_stopped(should_cancel, boundary="report_review_decision")
            return payload, attempts
        error = _review_capacity_feedback(budget, call.response, call.text, error)
        reask = review_feedback(
            feedback,
            error,
            budget.remaining - reserve,
            sources=sources is not None,
            claims=assess is not None,
        )
        budget.record_feedback(call.text, reask)
        messages = budget.messages
        await _save_review(checkpoint)
        raise_if_stopped(should_cancel, boundary="report_review_decision")
    return _cached_verdict(budget, assess)


def _review_capacity_feedback(
    budget: ReviewBudget, response: CompletionResponse, text: str, error: str | None
) -> str | None:
    """Preserve bounded replacement capacity in the same durable review allowance."""
    if response.finish_reason != "length":
        return error
    previous = budget.output_ceiling
    ceiling = TurnCeiling(previous)
    ceiling.grow_after(
        finish_reason=response.finish_reason,
        content_chars=len(text),
        output_tokens=response.usage.output_tokens,
    )
    budget.output_ceiling = ceiling.tokens
    capacity = (
        "The next allowed request has a larger output allowance."
        if ceiling.tokens > previous
        else "The output allowance is at its hard cap."
    )
    return f"{error} {capacity} Emit the JSON object and keep internal reasoning brief."


def _canonical_payload(payload: dict[str, Any], aliases: CitationAliases) -> dict[str, Any]:
    """The reviewer's verdict with every quoted citation translated back to
    canonical ids, so its findings live in the same id space as the report
    they are placed in."""
    failures = payload.get("failures")
    if not isinstance(failures, list):
        return payload
    translated = [
        {
            key: aliases.to_canonical(value) if isinstance(value, str) else value
            for key, value in item.items()
        }
        if isinstance(item, dict)
        else item
        for item in failures
    ]
    return {**payload, "failures": translated}


@dataclass
class _Review:
    findings: list[Finding]
    outcome: str
    trail: dict[str, Any]


def _review_trace(
    final: FinalReport,
    findings: list[Finding],
    outcome: str,
    attempts: int,
    unplaced: int,
    final_check: bool,
    suspected_count: int,
) -> dict[str, Any]:
    kinds = [finding.kind for finding in findings]
    row = {
        "kind": "report_final_check" if final_check else "report_review",
        "draft_sha256": hashlib.sha256(final.markdown.encode()).hexdigest(),
        "outcome": outcome,
        "attempts": attempts,
        "findings": len(findings),
        "unsupported": kinds.count(KIND_UNSUPPORTED),
        "repeated": kinds.count(KIND_REPEATED),
        "process_language": kinds.count(KIND_PROCESS),
        "unresolved_citations": kinds.count(KIND_UNRESOLVED_CITATION),
        "absence": kinds.count(KIND_ABSENCE),
        "review": kinds.count(KIND_REVIEW),
        "unplaced": unplaced,
    }
    if suspected_count:
        row["nli_suspicions"] = suspected_count
    return row


async def _review(
    router: LLMRouter,
    final: FinalReport,
    *,
    query: str,
    coverage: dict[str, Any],
    trail: Sequence[dict[str, Any]],
    by_id: dict[str, Passage],
    nli: Any,
    aliases: CitationAliases,
    untested: Sequence[str],
    conversation_id: str | None,
    budget: ReviewBudget | None = None,
    reserve: int = 0,
    final_check: bool = False,
    should_cancel: ShouldCancelFn = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
    notes: Mapping[str, SourceNotes] = {},  # noqa: B006 — read-only default
) -> _Review:
    """Grade the rendered report once and return one findings list.

    The deterministic rulers and the verifier run first, on the canonical
    text; the model reviewer reads the same report rendered back into the ids
    the writer was taught, and its verdict is translated back before it is
    placed. Every finding names a part, so the rework can be scoped to parts.
    """
    sections = list(final.sections)
    pool_ids = set(by_id)
    deterministic = await asyncio.to_thread(
        _deterministic_findings,
        final.summary,
        sections,
        pool_ids,
        valid_ids_line=aliases.valid_ids_line(),
        untested=untested,
        notes=notes,
    )
    unsupported, claims = await _grounding_review(final.summary, sections, by_id, nli)
    # Missing citations are mechanical. NLI disagreement about a cited sentence
    # is advisory; it must not independently order deletion of supported prose.
    uncited = [row for row in unsupported if not (row[2] or row[3])]
    suspected = [row for row in unsupported if row[2] or row[3]]
    findings = [*unsupported_findings(uncited), *deterministic]
    assessment = _ReviewAssessment(claims, by_id, aliases, suspected)

    payload, attempts = await _model_review(
        router,
        aliases.to_alias(final.body_markdown),
        aliases.to_alias(
            _quality_audit_context(
                claims,
                sections,
                by_id,
                final.summary,
                query=query,
                coverage=coverage,
                trail=trail,
            )
            + _nli_context(suspected)
            + (repair_validation_context(trail, claims) if final_check else "")
        ),
        conversation_id=conversation_id,
        sources={aliases.alias_by_passage.get(key, key): value for key, value in by_id.items()},
        budget=budget,
        reserve=reserve,
        final_check=final_check,
        should_cancel=should_cancel,
        assess=assessment if budget is not None else None,
        checkpoint=checkpoint,
    )
    unplaced = 0
    if payload is not None:
        placed, unplaced = review_findings(
            _canonical_payload(payload, aliases), final.summary, sections
        )
        findings.extend(placed)
        findings.extend(_assessment_findings(assessment.rows))
    outcome = REVIEW_OUTCOME_VERDICT if payload is not None else REVIEW_OUTCOME_UNAVAILABLE
    if payload is not None and assessment.unresolved():
        outcome = "incomplete"
    review_trail = _review_trace(
        final, findings, outcome, attempts, unplaced, final_check, len(suspected)
    )
    review_trail.update(assessment.signal_trace())
    if budget is not None:
        review_trail.update(
            decisions_used=budget.used,
            decisions_limit=budget.limit,
            output_ceiling=budget.output_ceiling,
            inspections=list(budget.inspections),
            claim_reviews=assessment.rows,
            assessment_errors=assessment.errors,
            provider_error=budget.provider_error,
        )
    return _Review(findings=findings, outcome=outcome, trail=review_trail)


# ---------------------------------------------------------------------------
# The one rework: named parts out, the same parts back, spliced in.
# ---------------------------------------------------------------------------


async def _rework(
    router: LLMRouter,
    base_messages: list[LLMMessage],
    final: FinalReport,
    findings: Sequence[Finding],
    *,
    aliases: CitationAliases,
    pool_ids: set[str],
    max_tokens: int,
    conversation_id: str | None,
) -> tuple[FinalReport, dict[str, Any]]:
    """Apply complete named parts, with at most one replacement request.

    A length-finished reply never reaches the splice, even if its prefix looks
    complete. The existing replacement can grow within the shared output cap;
    exhaustion retains the original prose and records the unresolved repair.
    """
    order = [title for title, _body in final.sections]
    named = named_parts(findings, order)
    additions = {finding.where: finding.after for finding in findings if finding.after is not None}
    rendered = render_findings(findings, order, summary_heading=REWORK_SUMMARY_HEADING)
    messages = [
        *base_messages,
        LLMMessage(role="assistant", content=aliases.to_alias(final.markdown)),
        LLMMessage(
            role="user",
            content=REWORK_INSTRUCTION.format(findings=aliases.to_alias(rendered)),
        ),
    ]

    def splice(rework: str) -> tuple[FinalReport, list[str], list[str], dict[str, str]]:
        if not has_report_prose(rework):
            return final, [], [], {}
        return _complete_rework(
            final, aliases.to_canonical(rework), named, pool_ids, additions=additions
        )

    ceiling = TurnCeiling(max_tokens)
    call_messages = messages
    spliced, returned, applied, rejected = splice("")
    cutoffs: list[dict[str, Any]] = []
    unresolved = None
    unavailable = None
    for stage in ("report_rework", "report_rework_reask"):
        rework, response, unavailable = await _rework_call(
            router,
            call_messages,
            max_tokens=ceiling.tokens,
            conversation_id=conversation_id,
            stage=stage,
        )
        if response is None:
            break
        if response.finish_reason == "length":
            cutoffs.append(
                {
                    "stage": stage,
                    "finish_reason": response.finish_reason,
                    "capacity_tokens": ceiling.tokens,
                    "output_tokens": response.usage.output_tokens,
                    "visible_chars": len(rework),
                }
            )
            ceiling.grow_after(
                finish_reason=response.finish_reason,
                content_chars=len(rework),
                output_tokens=response.usage.output_tokens,
            )
            unresolved = (
                "A repair reply was cut off by the output limit; no complete replacement "
                "was applied. The original draft was retained for: " + ", ".join(named)
            )
            reask = _REWORK_CEILING_REASK
        else:
            spliced, returned, applied, rejected = splice(rework)
            reask = _rework_reask(rework, applied, rejected)
            unresolved = (
                "No complete requested replacement was applied. The original draft was retained "
                "for: " + ", ".join(named)
                if reask is not None
                else None
            )
        if reask is None:
            break
        call_messages = [*messages, LLMMessage(role="user", content=reask)]
    trail = {
        "kind": "report_rework",
        "named": named,
        "returned": returned,
        "applied": applied,
        "rejected": rejected,
    }
    if additions:
        trail["missing_coverage"] = [title for title in additions if title not in applied]
    if unavailable is not None:
        trail["unavailable"] = unavailable
    if cutoffs:
        trail["capacity"] = {"cutoffs": cutoffs, "hard_cap_tokens": RESEARCH_CEILING_CAP}
    if unresolved is not None:
        trail["unresolved"] = unresolved
    return spliced, trail


_REWORK_CEILING_REASK = (
    "Your previous reply was cut off by the output limit before the rewritten parts "
    "were complete, so none of it was applied. Return each part named in the findings "
    "above, complete under its exact '## ' heading, and nothing else. "
    "Keep any internal reasoning brief."
)


async def _rework_call(
    router: LLMRouter,
    messages: list[LLMMessage],
    *,
    max_tokens: int,
    conversation_id: str | None,
    stage: str,
) -> tuple[str, CompletionResponse | None, str | None]:
    """Return prose, completion metadata, and a sanitized provider failure if any."""
    try:
        rework, response = await _writer_call(
            router,
            messages,
            max_tokens=max_tokens,
            temperature=0.0,
            conversation_id=conversation_id,
            stage=stage,
            attempt=1,
        )
    except LLMError as exc:
        return "", None, _provider_failure(exc)
    return rework, response, None


def _rework_reask(rework: str, applied: Sequence[str], rejected: dict[str, str]) -> str | None:
    """Name missing or rejected parts within the existing replacement allowance."""
    if not has_report_prose(rework):
        return EMPTY_REWORK_REASK
    if applied or not rejected:
        return None
    return FRAGMENT_REWORK_REASK.format(
        words=count_prose_words(rework),
        parts="; ".join(f"{part}: {reason}" for part, reason in rejected.items()),
    )
