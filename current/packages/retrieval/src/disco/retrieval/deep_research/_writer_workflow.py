"""Prompt, review, and final assembly helpers for the whole-report writer."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from disco.core import LLMMessage, ReportSection
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMRouter, ModelRole, Requirement
from disco.core.think import strip_think_spans

from ..models import Passage
from ._synthesis_parts import repair_tables
from ._writer_parts import (
    cited_ids,
    confidence_from_claims,
    normalize_citations,
    parse_report,
    readable_summary,
    validate_charts,
)
from .depth import DepthBound
from .quality_audit import (
    ClaimRecord,
    find_high_specificity_claims,
    hedge_boilerplate_metrics,
    source_concentration,
)
from .report_compiler import ReportCompilationError, collect_claim_ledger
from .source_identity import canonical_work_key


def coverage_instruction(coverage: dict[str, Any]) -> str:
    covered = _covered_lines(coverage)
    open_gaps = _clean_strings(coverage.get("open"))
    contradictions = _clean_strings(coverage.get("contradictions_checked"))
    if not covered and not open_gaps and not contradictions:
        return ""
    lines = ["COVERAGE MAP (research state; use as guidance, not as a required outline):"]
    _append_coverage_lines(lines, covered, open_gaps, contradictions)
    lines.append(
        "Develop every supported covered angle from the evidence, then consolidate "
        "them into natural sections. Do not answer this as a questionnaire, copy "
        "these angle names as headings mechanically, or discuss the coverage map."
    )
    return "\n".join(lines) + "\n\n"


def _covered_lines(coverage: dict[str, Any]) -> list[str]:
    covered: list[str] = []
    for item in coverage.get("covered", []):
        if not isinstance(item, dict):
            continue
        angle = item.get("angle")
        if not isinstance(angle, str) or not angle.strip():
            continue
        ids = [
            item_id.strip()
            for item_id in item.get("evidence_ids", [])
            if isinstance(item_id, str) and item_id.strip()
        ]
        suffix = f" (evidence: {', '.join(ids)})" if ids else ""
        covered.append(f"- {angle.strip()}{suffix}")
    return covered


def _clean_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _append_coverage_lines(
    lines: list[str], covered: list[str], open_gaps: list[str], contradictions: list[str]
) -> None:
    if covered:
        lines.append("Covered angles:\n" + "\n".join(covered))
    if open_gaps:
        lines.append("Open gaps:\n" + "\n".join(f"- {gap}" for gap in open_gaps))
    if contradictions:
        lines.append(
            "Contradictions checked:\n" + "\n".join(f"- {claim}" for claim in contradictions)
        )


def quality_audit_context(
    claims: list[ClaimRecord],
    sections: list[tuple[str, str]],
    by_id: dict[str, Passage],
) -> str:
    def work_for_source(source_id: str) -> str:
        passage = by_id.get(source_id)
        return canonical_work_key(passage) if passage is not None else ""

    def domain_for_source(source_id: str) -> str:
        passage = by_id.get(source_id)
        if passage is None:
            return ""
        return urlsplit(passage.source_url).netloc.lower().removeprefix("www.")

    lines: list[str] = []
    specific = find_high_specificity_claims(claims, work_for_source, domain_for_source)
    uncorroborated = [finding for finding in specific if finding.needs_corroboration]
    if uncorroborated:
        lines.append(
            "High-specificity claims backed by fewer than two distinct works. "
            "Flag only when the sentence is not already explicitly attributed "
            "to its original/single source; otherwise single-source attribution is acceptable:"
        )
        lines.extend(
            f"- {finding.text[:220]} (works={finding.work_count}, domains={finding.domain_count}; "
            f"signals={','.join(finding.matched_rules)})"
            for finding in uncorroborated[:8]
        )
    concentration = source_concentration(claims, work_for_source, threshold=0.15)
    if concentration.flagged and concentration.dominant_work:
        lines.append(
            f"- One work supports {concentration.dominant_share:.0%} of all claims "
            f"({concentration.dominant_work}). Check whether the draft over-relies "
            "on its framing; diversify or make that dependence explicit."
        )
    paragraphs = [
        paragraph.strip()
        for _, body in sections
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    ]
    hedge = hedge_boilerplate_metrics(paragraphs)
    if hedge.repeated_phrases:
        repeated = ", ".join(f"{phrase} x{count}" for phrase, count in hedge.repeated_phrases)
        lines.append(
            f"- Repeated hedge language: {repeated}. Flag only boilerplate repetition; "
            "preserve hedging that communicates real uncertainty."
        )
    return (
        "\n".join(lines)
        if lines
        else "- No specificity, work-concentration, or hedge-repetition signal was raised."
    )


def is_hard_deficiency(line: str) -> bool:
    if line.startswith("Report is "):
        return False
    if line.startswith("R6 —") and re.search(r"\b(?:word|words|length|range)\b", line, re.I):
        return False
    return not line.startswith(("R4 —", "R5 —", "Near-duplicate body paragraphs"))


def _review_request(
    messages: list[LLMMessage], conversation_id: str | None, max_tokens: int
) -> CompletionRequest:
    from . import writer

    return CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.RAG_ANSWERER, requirements=frozenset({Requirement.JSON_MODE})
        ),
        messages=messages,
        temperature=0.0,
        max_tokens=max_tokens,
        response_format="json",
        enable_thinking=False,
        metadata=writer._inspect_metadata(conversation_id, "report_review"),
    )


async def _review_call(
    router: LLMRouter,
    request: CompletionRequest,
    *,
    conversation_id: str | None,
    attempt: int,
    stage: str,
) -> tuple[list[str] | None, str | None, str, Any]:
    from . import writer

    started = writer.time.perf_counter()
    response = await router.complete(request)
    latency_ms = max(0, int((writer.time.perf_counter() - started) * 1_000))
    text = strip_think_spans(response.text)
    lines, error = writer._decode_review(text)
    writer._record_writer_io(
        conversation_id,
        request,
        response,
        text,
        stage=stage,
        attempt=attempt,
        latency_ms=latency_ms,
        declared_decision={"passes": not lines, "failures": lines} if lines is not None else None,
        parse_error=error,
    )
    return lines, error, text, response


async def self_review_deficiencies(
    router: LLMRouter,
    draft: str,
    prior_flagged: list[str],
    audit_context: str,
    *,
    conversation_id: str | None,
    attempt: int,
) -> list[str]:
    from . import writer

    previously = ""
    if prior_flagged:
        previously = (
            "Previously flagged deficiencies (judge whether they are fixed; do "
            "not invent new criteria):\n"
            + "\n".join(f"- {line}" for line in prior_flagged)
            + "\n\n"
        )
    instruction = writer._SELF_REVIEW_PROMPT.format(
        rubric=writer.RESEARCH_REPORT_RUBRIC,
        previously_flagged=previously,
        audit_context=audit_context,
        draft=draft,
    )
    messages = [
        LLMMessage(role="system", content=writer._REVIEW_SYSTEM),
        LLMMessage(role="user", content=instruction),
    ]
    request = _review_request(messages, conversation_id, writer._REVIEW_MAX_TOKENS)
    lines, error, text, response = await _review_call(
        router, request, conversation_id=conversation_id, attempt=attempt, stage="report_review"
    )
    if lines is not None:
        return lines
    retry_tokens = (
        min(writer._MAX_OUTPUT_TOKENS, writer._REVIEW_MAX_TOKENS * 2)
        if response.finish_reason == "length" or not text.strip()
        else writer._REVIEW_MAX_TOKENS
    )
    retry_messages = [
        *messages,
        LLMMessage(
            role="user",
            content=(
                f"Your response could not be used: {error}. Reply again with "
                'ONE strict JSON object: {"passes": true|false, "failures": '
                '[{"rubric": "...", "where": "...", "fix": "..."}]}'
            ),
        ),
    ]
    retry_request = _review_request(retry_messages, conversation_id, retry_tokens)
    lines, retry_error, _retry_text, _retry_response = await _review_call(
        router,
        retry_request,
        conversation_id=conversation_id,
        attempt=attempt,
        stage="report_review_retry",
    )
    if lines is None:
        raise ReportCompilationError(
            f"report reviewer returned malformed JSON after retry: {retry_error}"
        )
    return lines


async def finalize(
    final_text: str,
    residual: list[str],
    *,
    by_id: dict[str, Passage],
    nli: Any,
    bound: DepthBound,
    emit: Any,
) -> Any:
    from . import writer

    summary_raw, section_pairs = parse_report(final_text)
    if not summary_raw.strip():
        raise ReportCompilationError("final report has no executive summary")
    if not section_pairs:
        raise ReportCompilationError("final report has no headed sections")
    pool_ids = set(by_id)
    sections = _normalized_sections(section_pairs, pool_ids)
    summary = normalize_citations(readable_summary(summary_raw), pool_ids)
    assembled = (
        summary
        + "\n\n"
        + "\n\n".join(f"## {section.title}\n{section.markdown}" for section in sections)
    )
    pairs = [(section.title, section.markdown) for section in sections]
    post = writer._deterministic_deficiencies(assembled, summary, pairs, pool_ids, bound)
    post.extend(await writer._grounding_deficiencies(summary, pairs, by_id, nli))
    _raise_post_hard(post, writer)
    residual = list(dict.fromkeys([*residual, *post]))
    ledger = await asyncio.to_thread(
        collect_claim_ledger, sections, list(by_id.values()), nli, summary
    )
    finalized, all_claims, unsupported_total = _final_claims(sections, ledger)
    await _emit_final_sections(finalized, emit)
    return writer.WrittenReport(
        summary=summary,
        summary_cited_passage_ids=sorted({item for item in cited_ids(summary) if item in pool_ids}),
        sections=finalized,
        claims=all_claims,
        unsupported_count=unsupported_total,
        review_notes=residual,
    )


def _normalized_sections(
    section_pairs: list[tuple[str, str]], pool_ids: set[str]
) -> list[ReportSection]:
    return [
        _normalized_section(index, title, body, pool_ids)
        for index, (title, body) in enumerate(section_pairs)
    ]


def _normalized_section(
    index: int, title: str, body: str, pool_ids: set[str]
) -> ReportSection:
    markdown = repair_tables(normalize_citations(validate_charts(body), pool_ids))
    return ReportSection(
        id=f"r{index}",
        title=title,
        markdown=markdown,
        cited_passage_ids=sorted({item for item in cited_ids(markdown) if item in pool_ids}),
    )


def _raise_post_hard(post: list[str], writer: Any) -> None:
    hard = [line for line in post if writer._is_hard_deficiency(line)]
    if hard:
        raise ReportCompilationError("final report failed after normalization: " + "; ".join(hard))


def _final_claims(
    sections: list[ReportSection], ledger: list[Any]
) -> tuple[list[ReportSection], list[dict[str, Any]], int]:
    by_section: dict[str, list[dict[str, Any]]] = {}
    for claim in ledger:
        by_section.setdefault(claim.section_id, []).append(claim.to_event_dict())
    finalized: list[ReportSection] = []
    all_claims: list[dict[str, Any]] = [*by_section.get("summary", [])]
    unsupported_total = 0
    for section in sections:
        section_claims = by_section.get(section.id, [])
        confidence, unsupported = confidence_from_claims(section_claims)
        finalized.append(
            section.model_copy(update={"confidence": confidence, "unsupported_count": unsupported})
        )
        unsupported_total += unsupported
        all_claims.extend(section_claims)
    return finalized, all_claims, unsupported_total


async def _emit_final_sections(sections: list[ReportSection], emit: Any) -> None:
    for index, section in enumerate(sections):
        await emit("phase", {"phase": "synthesize", "section": index + 1})
        await emit(
            "section_done", {"section_id": section.id, "title": section.title, "done": index + 1}
        )


async def write_report(
    query: str,
    outcome: Any,
    *,
    router: LLMRouter,
    nli: Any,
    bound: DepthBound,
    emit: Any,
    recency_window: Literal["month", "week"] | None = None,
    conversation_id: str | None = None,
) -> Any:
    from . import writer

    _validate_outcome(outcome)
    by_id = {passage.id: passage for passage in outcome.passages}
    base_messages = [
        LLMMessage(role="system", content=writer._EVIDENCE_SYSTEM_PROMPT),
        LLMMessage(
            role="user", content=writer._report_instruction(query, outcome, bound, recency_window)
        ),
    ]
    max_tokens = writer._writer_max_tokens(bound)
    await emit("phase", {"phase": "writing"})
    draft = await writer._generate_report(
        router,
        base_messages,
        max_tokens=max_tokens,
        temperature=writer._DRAFT_TEMPERATURE,
        conversation_id=conversation_id,
        stage="report_draft",
        attempt=1,
    )
    if not draft:
        raise ReportCompilationError("report writer returned no prose")
    draft, residual = await _review_loop(
        writer,
        router,
        base_messages,
        draft,
        by_id=by_id,
        nli=nli,
        bound=bound,
        max_tokens=max_tokens,
        conversation_id=conversation_id,
        emit=emit,
    )
    return await finalize(draft, residual, by_id=by_id, nli=nli, bound=bound, emit=emit)


def _validate_outcome(outcome: Any) -> None:
    if not outcome.passages:
        raise ReportCompilationError("research produced no usable evidence")
    passage_ids = [passage.id for passage in outcome.passages]
    if any(not passage_id.strip() for passage_id in passage_ids):
        raise ReportCompilationError("research evidence contains a blank passage id")
    if len(set(passage_ids)) != len(passage_ids):
        raise ReportCompilationError("research evidence contains duplicate passage ids")


async def _review_loop(
    writer: Any,
    router: LLMRouter,
    base_messages: list[LLMMessage],
    draft: str,
    *,
    by_id: dict[str, Passage],
    nli: Any,
    bound: DepthBound,
    max_tokens: int,
    conversation_id: str | None,
    emit: Any,
) -> tuple[str, list[str]]:
    prior_flagged: list[str] = []
    residual: list[str] = []
    for attempt in range(writer._WRITER_ATTEMPTS):
        await emit("phase", {"phase": "reviewing", "attempt": attempt + 1})
        deficiencies = await writer._review_candidate(
            router,
            draft,
            by_id=by_id,
            nli=nli,
            bound=bound,
            prior_flagged=prior_flagged,
            conversation_id=conversation_id,
            attempt=attempt + 1,
        )
        hard = [line for line in deficiencies if writer._is_hard_deficiency(line)]
        soft = [line for line in deficiencies if not writer._is_hard_deficiency(line)]
        if not deficiencies:
            return draft, residual
        if attempt == writer._WRITER_ATTEMPTS - 1:
            if hard:
                raise ReportCompilationError(
                    "report retained hard deficiencies after rework: " + "; ".join(hard)
                )
            return draft, soft
        prior_flagged = deficiencies
        await emit("phase", {"phase": "writing", "attempt": attempt + 2})
        draft = await writer._rework_report(
            router,
            base_messages,
            draft,
            deficiencies,
            bound=bound,
            max_tokens=max_tokens,
            conversation_id=conversation_id,
            attempt=attempt + 2,
        )
    return draft, residual
