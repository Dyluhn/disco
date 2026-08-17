"""Grounded executive-summary reduction for Deep Research reports."""

from __future__ import annotations

import asyncio
import datetime
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from disco.core import LLMMessage, ReportSection
from disco.core.llm import CapabilityProfile, CompletionRequest, LLMRouter, ModelRole
from disco.core.think import strip_think_spans

from ..grounding import _retain_supported_claims, _verify_claims
from ..models import Passage
from .evidence import EVIDENCE_SYSTEM_PROMPT

_COHERENCE_PROMPT = (
    "You are writing the executive summary of a research report. State the "
    "FINDINGS — the actual answer to the question — so a reader who reads ONLY "
    "the summary learns what the report concludes.\n\n"
    "Question: {query}\n\n"
    "Sections of the report (with their lead findings):\n{outline}\n\n"
    "Grounding-supported cited findings from every section (complete claim ledger; dates are "
    "source publication dates, not guesses):\n{evidence}\n\n"
    "RULES — these are the difference between a summary that informs and one "
    "that just describes structure:\n\n"
    "1. LEAD WITH THE ANSWER. The first sentence states the report's bottom-"
    "line answer to the question. Not 'this report examines X.' Not 'X is a "
    "transformative technology.' The actual finding: where things stand, what "
    "the state of play is, what the report concluded.\n\n"
    "2. NAME THE KEY TENSIONS AND UNCERTAINTIES. If the field is divided, say "
    "what's contested. If announcements outpace shipping reality, say so. If "
    "evidence is thin in a particular area, name it. A good executive summary "
    "tells the reader where to be skeptical.\n\n"
    "3. NO STRUCTURE DESCRIPTION. The following sentences are BANNED: 'This "
    "report begins by…', 'It then examines…', 'Finally, it evaluates…', "
    "'The report is organized as…', 'The first section covers…'. Never tell "
    "the reader what's coming; tell them what was found.\n\n"
    "4. MEASURED REGISTER. Cut: 'transformative,' 'revolutionary,' 'poised "
    "to revolutionize,' 'pivotal,' 'game-changing.' Describe and qualify.\n\n"
    "5. SYNTHESIZE ACROSS SECTIONS. The summary is not three section "
    "summaries glued together; it's the overarching story those sections "
    "tell when read together.\n\n"
    "6. DO NOT TURN A QUALIFICATION INTO A CONTRADICTION. If a section reports "
    "a concrete release, launch, or announcement, name it before qualifying its "
    "importance or availability. Never claim none occurred when a section says "
    "one did.\n\n"
    "FORMAT: 2–4 short paragraphs of markdown. No headers. No new claims "
    "beyond what the sections established — the summary distills."
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

# The complete supported-claim ledger is retained for grounding.  Only the
# fallback's pairwise NLI work is bounded: a recent posting window finds likely
# lexical conflicts, and unexamined findings remain present rather than being
# silently discarded.  This keeps exhaustive reports linear in claim count.
_CONFLICT_TERM_HISTORY = 32
_CONFLICT_CANDIDATES_PER_FINDING = 4
_MAX_FALLBACK_CONFLICT_CHECKS = 256
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


def _fallback_sentence(finding: _SupportedFinding) -> str:
    """Render one grounded finding without leaking quote-block markers."""
    cleaned = re.sub(r"(?m)^\s*>\s?", "", finding.text).rstrip(" .")
    citations = " ".join(f"[[{passage_id}]]" for passage_id in finding.cited_ids)
    return f"{cleaned}. {citations}"


def _coherence_outline(sections: list[ReportSection]) -> str:
    """Render bounded, cited section evidence for the summary reduce call."""
    outline_lines = []
    for section in sections:
        lead = (section.markdown.strip()[:1_200] or section.title).replace("  ", " ").strip()
        marks = []
        if section.confidence in ("mixed", "low"):
            marks.append(f"confidence={section.confidence}")
        if section.disputed_notes:
            marks.append(f"flagged disagreement: {section.disputed_notes[0][:100]}")
        suffix = f"  [{'; '.join(marks)}]" if marks else ""
        outline_lines.append(f"- **{section.title}** — {lead}{suffix}")
    return "\n".join(outline_lines)


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
) -> str:
    instruction = _recency_preamble(recency_window) + _COHERENCE_PROMPT.format(
        query=query,
        outline=_coherence_outline(sections),
        evidence=_render_findings_ledger(findings),
    )
    try:
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
    except Exception:  # noqa: BLE001 — verified section fallback is authoritative
        return ""
    return strip_think_spans(response.text)


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


def _render_findings_ledger(findings: list[_SupportedFinding]) -> str:
    """Render the complete tier-bounded claim ledger.

    Deep Research admits at most twelve sections and section synthesis is itself
    output-bounded, so retaining every supported claim here is finite and avoids
    recreating the tail-loss that hid contradictory findings.
    """
    if not findings:
        return "- No section claim passed host grounding verification."
    return "\n".join(
        f"- section={finding.section_index + 1}; published="
        f"{finding.published_at.isoformat() if finding.published_at else 'unknown'}; "
        f"finding={finding.text.rstrip(' .')}. "
        + " ".join(f"[[{passage_id}]]" for passage_id in finding.cited_ids)
        for finding in findings
    )


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


def _fallback_conflict_candidates(
    findings: list[_SupportedFinding],
) -> list[tuple[int, int]]:
    """Return a bounded set of likely contradiction pairs.

    Findings are visited oldest-to-newest when dates are known.  Each claim
    considers only recent postings for its substantive terms and at most four
    strongest lexical candidates.  Missing a pair is conservative: fallback
    keeps both claims; it never drops an unchecked finding.
    """

    postings: dict[str, deque[int]] = {}
    pairs: list[tuple[int, int]] = []
    ordered = sorted(
        range(len(findings)),
        key=lambda index: (
            findings[index].published_at or datetime.date.min,
            index,
        ),
    )
    for right_index in ordered:
        right = findings[right_index]
        right_terms = _claim_terms(right.text)
        candidate_overlap: dict[int, int] = {}
        for term in right_terms:
            for left_index in postings.get(term, ()):
                candidate_overlap[left_index] = candidate_overlap.get(left_index, 0) + 1
        opposite_negation = _has_negation(right.text)
        ranked = sorted(
            (
                (overlap, left_index)
                for left_index, overlap in candidate_overlap.items()
                if overlap >= 2 or _has_negation(findings[left_index].text) != opposite_negation
            ),
            key=lambda item: (-item[0], item[1]),
        )
        for _overlap, left_index in ranked[:_CONFLICT_CANDIDATES_PER_FINDING]:
            pairs.append((left_index, right_index))
            if len(pairs) >= _MAX_FALLBACK_CONFLICT_CHECKS:
                return pairs
        for term in right_terms:
            posting = postings.setdefault(term, deque(maxlen=_CONFLICT_TERM_HISTORY))
            posting.append(right_index)
    return pairs


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


def _fallback_findings(
    findings: list[_SupportedFinding], nli: Any, recency_window: str | None
) -> str:
    dropped: set[int] = set()
    unresolved = False
    for left_index, right_index in _fallback_conflict_candidates(findings):
        left = findings[left_index]
        right = findings[right_index]
        if not _nli_contradicts(left.text, right.text, nli):
            continue
        if (
            recency_window is not None
            and left.published_at
            and right.published_at
            and left.published_at != right.published_at
        ):
            dropped.add(left_index if left.published_at < right.published_at else right_index)
        else:
            unresolved = True
    kept = [finding for index, finding in enumerate(findings) if index not in dropped]
    paragraphs = _paragraphize([_fallback_sentence(finding) for finding in kept])
    if not paragraphs:
        return ""
    # Keep the mechanically grounded fallback readable without asking another
    # model to rewrite it. Four contiguous paragraphs preserve finding order and
    # prevent Markdown markers or a long claim ledger from becoming one wall.
    if unresolved:
        paragraphs[0] = "Sources disagree on some current-state findings. " + paragraphs[0]
    return "\n\n".join(paragraphs)


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


async def coherence_pass(
    query: str,
    sections: list[ReportSection],
    *,
    router: LLMRouter,
    passages: list[Passage],
    nli: Any,
    recency_window: str | None = None,
) -> str:
    """Generate a cited summary and retain only independently supported claims."""
    if not sections:
        return "*(No sections were generated.)*"
    by_id = {passage.id: passage for passage in passages}
    findings = await asyncio.to_thread(_collect_supported_findings, sections, by_id, nli)
    summary = await _generate_summary(query, sections, findings, router, recency_window)
    if summary:
        claims = await asyncio.to_thread(_verify_claims, summary, by_id, nli)
        claims, had_conflict = await asyncio.to_thread(
            _reject_conflicting_summary_claims, claims, findings, by_id, nli
        )
        grounded = _retain_supported_claims(summary, claims)
        if grounded and not had_conflict:
            return _readable_summary(grounded)
    if findings:
        return await asyncio.to_thread(_fallback_findings, findings, nli, recency_window)
    return "The report did not contain a factual claim that passed grounding verification."
