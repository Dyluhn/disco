"""Grounded executive-summary reduction for Deep Research reports.

One writer, one bounded repair, one validation gate. A summary that cannot
pass validation raises ReportCompilationError — the run surfaces an error
instead of shipping a manufactured or diagnostic summary.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from dataclasses import dataclass
from typing import Any

from disco.core import LLMMessage, ReportSection
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMRouter, ModelRole
from disco.core.think import strip_think_spans

from ..grounding import _retain_supported_claims, _verify_claims
from ..models import Passage
from .evidence import EVIDENCE_SYSTEM_PROMPT
from .report_compiler import ReportCompilationError

_COHERENCE_PROMPT = (
    "You are the executive editor of a research report. Write a direct, "
    "finding-led answer so a reader who reads ONLY the summary understands the "
    "report's conclusion.\n\n"
    "Question: {query}\n\n"
    "Complete final report:\n{report}\n\n"
    "The first sentence MUST explicitly answer the question and name its "
    "subject. Do not open by defining an evaluation framework unless the user "
    "asked for that definition. Lead with the bottom-line answer, then give the "
    "strongest supporting "
    "findings and decision-relevant tensions or uncertainty. Discuss the subject "
    "itself, never the report's organization or how the material was gathered. "
    "When the report has multiple sections, synthesize across them; never copy "
    "the opening paragraph of one section as the whole summary. "
    "Use a measured register. Preserve the inline citations already present in "
    "the report and introduce no new claims. Grounding-supported cited findings "
    "are already embedded in the complete report. If the report names a "
    "concrete release, launch, or announcement, Never claim none occurred.\n\n"
    "FORMAT: 2–4 short paragraphs of markdown. No headers."
)

_WORD = re.compile(r"[a-z0-9]+")
_NEGATIONS = frozenset({"no", "not", "none", "never", "neither", "without", "unavailable"})
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "no",
        "not",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)

_SUMMARY_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*_`])")


@dataclass(frozen=True)
class _SupportedFinding:
    text: str
    cited_ids: tuple[str, ...]
    section_index: int
    published_at: datetime.date | None


def _paragraphize(sentences: list[str]) -> list[str]:
    """Group ordered sentences into at most four balanced paragraphs."""
    if not sentences:
        return []
    paragraph_count = min(4, len(sentences))
    base_size, larger_count = divmod(len(sentences), paragraph_count)
    paragraphs: list[str] = []
    offset = 0
    for index in range(paragraph_count):
        size = base_size + (1 if index < larger_count else 0)
        paragraphs.append(" ".join(sentences[offset : offset + size]))
        offset += size
    return paragraphs


def _recency_preamble(recency_window: str | None) -> str:
    if recency_window is None:
        return ""
    label = "month" if recency_window == "month" else "week"
    return (
        f"Today's date is {datetime.date.today().isoformat()}. This research focused "
        f"on the PAST {label.upper()}. Reflect that recency bias in the summary.\n\n"
    )


async def _generate_summary(
    query: str,
    sections: list[ReportSection],
    findings: list[_SupportedFinding],
    router: LLMRouter,
    recency_window: str | None,
    repair_of: str | None = None,
) -> str:
    """One summary-writer call. Provider failure propagates to the caller —
    a dead provider is a run failure, never a silently degraded summary."""
    report = _complete_report_text(sections)
    dated_context = ""
    dates = sorted(
        {finding.published_at.isoformat() for finding in findings if finding.published_at}
    )
    if dates:
        dated_context = "\n\nDated context already represented by the report: " + ", ".join(
            f"published={date}" for date in dates
        )
    repair = ""
    if repair_of:
        repair = (
            "\n\nRewrite the previous draft below. Keep only claims established in "
            "the complete report and preserve their inline citations.\n"
            f"Previous draft:\n{repair_of}\n"
        )
    instruction = _recency_preamble(recency_window) + _COHERENCE_PROMPT.format(
        query=query,
        report=report,
    ) + dated_context + repair
    response = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
            messages=[
                LLMMessage(role="system", content=EVIDENCE_SYSTEM_PROMPT),
                LLMMessage(role="user", content=instruction),
            ],
            temperature=0.0,
            max_tokens=400,
        )
    )
    return strip_think_spans(response.text)


def _complete_report_text(sections: list[ReportSection]) -> str:
    """Render the final verified body with the concluding analysis first.

    Evidence-led reports conventionally place implications or the direct
    answer last.  Putting that section first in the editor-only view prevents
    long reports from turning the executive summary into a copy of section
    one; the complete report remains present and its published order is not
    changed.
    """
    ordered = [sections[-1], *sections[:-1]] if len(sections) > 1 else sections
    return "\n\n".join(
        f"## {section.title}\n{section.markdown.strip()}"
        for section in ordered
        if section.markdown.strip()
    )


def _latest_source_date(
    cited_ids: tuple[str, ...], by_id: dict[str, Passage]
) -> datetime.date | None:
    dates = [by_id[passage_id].published_at for passage_id in cited_ids if passage_id in by_id]
    known = [date for date in dates if date is not None]
    return max(known, default=None)


def _collect_supported_findings(
    sections: list[ReportSection], by_id: dict[str, Passage], nli: Any
) -> list[_SupportedFinding]:
    findings: list[_SupportedFinding] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for section_index, section in enumerate(sections):
        claims = _verify_claims(section.markdown, by_id, nli)
        for claim in claims:
            if claim["verdict"] != "supported":
                continue
            body = claim["claim"]
            cited_ids = tuple(
                passage_id for passage_id in body["cited_passage_ids"] if passage_id in by_id
            )
            text = str(body["text"]).strip()
            key = (" ".join(text.lower().split()), cited_ids)
            if not cited_ids or not text or key in seen:
                continue
            seen.add(key)
            findings.append(
                _SupportedFinding(
                    text=text,
                    cited_ids=cited_ids,
                    section_index=section_index,
                    published_at=_latest_source_date(cited_ids, by_id),
                )
            )
    return findings


def _stem(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("sed") and len(token) > 5:
        return token[:-1]
    if token.endswith("ed") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and len(token) > 4:
        return token[:-1]
    return token


def _claim_terms(text: str) -> set[str]:
    return {_stem(token) for token in _WORD.findall(text.lower()) if token not in _STOPWORDS}


def _has_negation(text: str) -> bool:
    return bool(_NEGATIONS.intersection(_WORD.findall(text.lower())))


def _claims_may_conflict(left: str, right: str) -> bool:
    shared = _claim_terms(left).intersection(_claim_terms(right))
    return len(shared) >= 2 or (bool(shared) and _has_negation(left) != _has_negation(right))


def _nli_contradicts(left: str, right: str, nli: Any) -> bool:
    if not _claims_may_conflict(left, right):
        return False
    try:
        return nli.entail(left, right) == "contradict" or nli.entail(right, left) == "contradict"
    except Exception:  # noqa: BLE001 — unavailable conflict check falls back to grounding only
        return False


def _summary_claim_conflicts(
    claim: dict,
    finding: _SupportedFinding,
    by_id: dict[str, Passage],
    nli: Any,
) -> bool:
    body = claim["claim"]
    text = str(body["text"])
    if not _nli_contradicts(text, finding.text, nli):
        return False
    cited_ids = tuple(str(item) for item in body.get("cited_passage_ids", []))
    claim_date = _latest_source_date(cited_ids, by_id)
    # A strictly newer supported source may truthfully describe a changed state.
    return not (
        claim_date is not None
        and finding.published_at is not None
        and claim_date > finding.published_at
    )


def _reject_conflicting_summary_claims(
    claims: list[dict],
    findings: list[_SupportedFinding],
    by_id: dict[str, Passage],
    nli: Any,
) -> tuple[list[dict], bool]:
    out: list[dict] = []
    had_conflict = False
    for claim in claims:
        conflict = claim["verdict"] == "supported" and any(
            _summary_claim_conflicts(claim, finding, by_id, nli) for finding in findings
        )
        had_conflict = had_conflict or conflict
        out.append({**claim, "verdict": "unsupported"} if conflict else claim)
    return out, had_conflict


def _readable_summary(summary: str) -> str:
    """Honor the 2–4 paragraph contract when a provider returns one prose wall."""
    cleaned = re.sub(r"(?m)^\s*>\s?", "", summary).strip()
    blocks = [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]
    if len(blocks) != 1 or "\n" in blocks[0]:
        return "\n\n".join(blocks)
    sentences = [part.strip() for part in _SUMMARY_SENTENCE_BREAK.split(blocks[0]) if part.strip()]
    if len(sentences) < 2:
        return blocks[0]
    return "\n\n".join(_paragraphize(sentences))


_SUMMARY_DIAGNOSTIC = re.compile(
    r"\b(?:verification|retrieval gap|provider failure|search process)\b",
    re.IGNORECASE,
)


def _is_substantive_summary(summary: str) -> bool:
    """Reject structural/status digests before they can become the summary."""
    plain = re.sub(r"\[\[[\w-]+\]\]", "", summary)
    words = _WORD.findall(plain)
    return (
        len(words) >= 3
        and bool(re.search(r"[.!?]", plain))
        and bool(re.search(r"\[\[[\w-]+\]\]", summary))
        and _SUMMARY_DIAGNOSTIC.search(summary) is None
    )


def _summary_copies_one_section(summary: str, sections: list[ReportSection]) -> bool:
    """Reject a section excerpt masquerading as an executive synthesis."""
    if len(sections) < 2:
        return False
    sentences = [
        re.sub(r"\[\[[\w-]+\]\]", "", sentence).strip(" \n*-#>.").casefold()
        for sentence in _SUMMARY_SENTENCE_BREAK.split(summary)
    ]
    substantive = [sentence for sentence in sentences if len(sentence.split()) >= 5]
    if len(substantive) < 2:
        return False
    for section in sections:
        body = re.sub(r"\[\[[\w-]+\]\]", "", section.markdown).casefold()
        copied = sum(sentence in body for sentence in substantive)
        if copied / len(substantive) >= 0.75:
            return True
    return False


async def coherence_pass(
    query: str,
    sections: list[ReportSection],
    *,
    router: LLMRouter,
    passages: list[Passage],
    nli: Any,
    recency_window: str | None = None,
) -> str:
    """Generate a cited executive summary and retain only supported claims.

    One draft, then one bounded repair against the same complete report. A
    summary that still fails the validation gate — uncited, insubstantial,
    conflicting with section findings, or a copy of one section — raises
    ReportCompilationError so the run errors instead of shipping it.
    """
    if not sections:
        raise ReportCompilationError("cannot write an executive summary for an empty report")
    by_id = {passage.id: passage for passage in passages}
    findings = await asyncio.to_thread(_collect_supported_findings, sections, by_id, nli)
    if not findings:
        raise ReportCompilationError(
            "claim verification retained no supported findings to summarize"
        )

    async def _ground_summary(candidate: str) -> str:
        if not candidate:
            return ""
        claims = await asyncio.to_thread(_verify_claims, candidate, by_id, nli)
        claims, had_conflict = await asyncio.to_thread(
            _reject_conflicting_summary_claims, claims, findings, by_id, nli
        )
        grounded = _retain_supported_claims(candidate, claims)
        if (
            grounded
            and not had_conflict
            and _is_substantive_summary(grounded)
            and not _summary_copies_one_section(grounded, sections)
        ):
            return _readable_summary(grounded)
        return ""

    draft = await _generate_summary(query, sections, findings, router, recency_window)
    grounded = await _ground_summary(draft)
    if grounded:
        return grounded

    repaired = await _generate_summary(
        query, sections, findings, router, recency_window, repair_of=draft
    )
    grounded = await _ground_summary(repaired)
    if grounded:
        return grounded
    raise ReportCompilationError("executive summary failed validation after one repair")
