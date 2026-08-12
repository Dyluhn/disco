"""Grounded executive-summary reduction for Deep Research reports."""

from __future__ import annotations

import asyncio
import datetime
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
    router: LLMRouter,
    recency_window: str | None,
) -> str:
    instruction = _recency_preamble(recency_window) + _COHERENCE_PROMPT.format(
        query=query, outline=_coherence_outline(sections)
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


async def _supported_section_findings(
    sections: list[ReportSection], by_id: dict[str, Passage], nli: Any
) -> list[str]:
    findings: list[str] = []
    for section in sections:
        claims = await asyncio.to_thread(_verify_claims, section.markdown, by_id, nli)
        for claim in claims:
            if claim["verdict"] != "supported":
                continue
            body = claim["claim"]
            ids = " ".join(
                f"[[{passage_id}]]"
                for passage_id in body["cited_passage_ids"]
                if passage_id in by_id
            )
            if ids:
                findings.append(f"{body['text'].rstrip(' .')}. {ids}")
                break
        if len(findings) >= 4:
            break
    return findings


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
    summary = await _generate_summary(query, sections, router, recency_window)
    if summary:
        claims = await asyncio.to_thread(_verify_claims, summary, by_id, nli)
        grounded = _retain_supported_claims(summary, claims)
        if grounded:
            return grounded
    findings = await _supported_section_findings(sections, by_id, nli)
    if findings:
        return " ".join(findings)
    return "The report did not contain a factual claim that passed grounding verification."
