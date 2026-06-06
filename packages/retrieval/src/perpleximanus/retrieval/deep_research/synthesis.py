"""Map-reduce synthesis: section per sub-question, then assemble + coherence.

Hundreds of sources will NOT fit one synthesis prompt. The pattern:

    MAP:    for each section:
                retrieve the relevant corpus subset for this section
                synthesize the section grounded in those passages
                verify per-claim via NLI (reuse `_verify_claims`)
                roll up confidence + disputed_notes from the verdicts

    REDUCE: assemble the sections + one coherence-pass call returns the
            executive summary that opens the report.

The per-section synthesis call is the same shape as the existing standard-
answer synthesis: constrained-citation prompt + RAG_ANSWERER. The output
markdown carries inline `[[passage_id]]` tags the UI's existing citation
machinery already resolves.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from perpleximanus.core import LLMMessage, ReportSection
from perpleximanus.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)

from ..models import Passage
from ..ranking import Embedder
from ..streaming import _verify_claims  # reuse — per-claim NLI verifier
from ..vectorstore import VectorStore
from .gather import SubQuestionResult

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


_SECTION_PROMPT = (
    "You are writing one section of a multi-section research report.\n\n"
    "Section topic: {topic}\n\n"
    "Use ONLY the following passages as your sources. Cite EVERY factual "
    "sentence with [[id]] markers (e.g. [[a_p0]]). When passages disagree, "
    "say so — DO NOT pick one and hide the conflict.\n\n"
    "PASSAGES:\n{passages}\n\n"
    "Write 3–6 short paragraphs. Markdown only. No section header (the report "
    "adds it). End with [[id]] citations on every factual sentence."
)


def _format_passages(passages: list[Passage]) -> str:
    """Inline the passages with their ids — the model uses [[id]] in the body
    to cite them. Truncate each to ~600 chars so a section's prompt fits."""
    parts = []
    for p in passages:
        head = (p.source_title or p.source_url)[:100]
        body = p.text.strip()
        if len(body) > 600:
            body = body[:600] + " …"
        parts.append(f"[{p.id}] {head}\n{body}")
    return "\n\n".join(parts)


_Confidence = Literal["high", "mixed", "low"]


def _confidence_from_claims(claims: list[dict]) -> tuple[_Confidence, int]:
    """Roll the per-claim NLI verdicts into one of three confidence buckets
    + an unsupported count. The thresholds match Perplexity-style honesty:
    low if ≥30% unsupported, mixed if ≥30% weak, otherwise high."""
    if not claims:
        return "low", 0
    total = len(claims)
    unsupported = sum(1 for c in claims if c.get("verdict") == "unsupported")
    weak = sum(1 for c in claims if c.get("verdict") == "weak")
    if unsupported / total >= 0.30:
        return "low", unsupported
    if (unsupported + weak) / total >= 0.30:
        return "mixed", unsupported
    return "high", unsupported


def _extract_disputed_notes(markdown: str) -> list[str]:
    """The section prompt asks the model to call out conflicts. Extract those
    sentences as `disputed_notes` so the UI can surface them as a callout
    above the section body. Pattern: sentences mentioning 'disagree',
    'conflict', 'dispute', 'contradict', 'however' near 'source', etc."""
    sentences = re.split(r"(?<=[.!?])\s+", markdown)
    cue = re.compile(
        r"\b(disagree|conflict|dispute|contradict|inconsistent|versus|"
        r"however[, ].+sources?)\b",
        re.IGNORECASE,
    )
    return [s.strip() for s in sentences if cue.search(s) and len(s) > 20][:3]


async def _retrieve_for_section(
    section_query: str,
    *,
    namespace: str,
    embedder: Embedder | None,
    vector_store: VectorStore,
    fallback_passages: list[Passage],
    top_k: int,
) -> list[Passage]:
    """Retrieve the corpus subset relevant to this section's topic from the
    per-run vector store. When the embedder isn't available (hermetic tests),
    fall back to the sub-question's own gathered passages — sufficient for
    correctness, just no cross-section pollination."""
    if embedder is None:
        return fallback_passages[:top_k]
    try:
        vecs = await embedder.embed([section_query])
        if not vecs:
            return fallback_passages[:top_k]
        return await vector_store.query(namespace, vecs[0], top_k=top_k)
    except Exception:  # noqa: BLE001 — fall back to the sub-q passages
        return fallback_passages[:top_k]


async def synthesize_section(
    sub_result: SubQuestionResult,
    *,
    router: LLMRouter,
    embedder: Embedder | None,
    vector_store: VectorStore,
    namespace: str,
    nli: Any,
    section_id: str,
    top_k_for_section: int,
    emit: EmitFn,
) -> ReportSection:
    """Synthesize one section from the corpus + verify per-claim. Returns a
    ReportSection ready for the ReportEvent. Any failure mode degrades
    gracefully: an empty corpus → "(no sources gathered)" body marked
    `confidence: low`."""
    passages = await _retrieve_for_section(
        sub_result.subq.title,
        namespace=namespace,
        embedder=embedder,
        vector_store=vector_store,
        fallback_passages=sub_result.passages,
        top_k=top_k_for_section,
    )
    await emit(
        "synthesize_section",
        {"section": sub_result.subq.title, "passages_used": len(passages)},
    )
    if not passages:
        return ReportSection(
            id=section_id,
            title=sub_result.subq.title,
            markdown="*(No sources were gathered for this section.)*",
            confidence="low",
        )

    instruction = _SECTION_PROMPT.format(
        topic=sub_result.subq.title, passages=_format_passages(passages)
    )
    try:
        resp = await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=[LLMMessage(role="user", content=instruction)],
                temperature=0.0,
                max_tokens=1400,
            )
        )
        markdown = resp.text.strip()
    except Exception as exc:  # noqa: BLE001 — synthesis failure → honest empty
        return ReportSection(
            id=section_id,
            title=sub_result.subq.title,
            markdown=f"*(Section synthesis failed: {type(exc).__name__})*",
            confidence="low",
        )

    # per-claim NLI verification (reuse the existing verifier — same shape
    # as the standard-answer flow, applied to this section's body + this
    # section's passages).
    by_id = {p.id: p for p in passages}
    claims = await asyncio.to_thread(_verify_claims, markdown, by_id, nli)
    confidence, unsupported = _confidence_from_claims(claims)
    disputed_notes = _extract_disputed_notes(markdown)
    # the cited passage ids actually mentioned in the body (regex extract)
    cited_ids = sorted({m for m in re.findall(r"\[\[([\w-]+)\]\]", markdown) if m in by_id})

    return ReportSection(
        id=section_id,
        title=sub_result.subq.title,
        markdown=markdown,
        cited_passage_ids=cited_ids,
        confidence=confidence,
        disputed_notes=disputed_notes,
        unsupported_count=unsupported,
    )


_COHERENCE_PROMPT = (
    "You are writing the executive summary that opens a multi-section research "
    "report.\n\n"
    "Question: {query}\n\n"
    "The report has these sections (with one-line summaries each):\n{outline}\n\n"
    "Write a 2–3 paragraph executive summary that frames the report and points "
    "to which sections cover what. Plain markdown. No section headers. "
    "No new claims — just frame the existing work."
)


async def coherence_pass(
    query: str,
    sections: list[ReportSection],
    *,
    router: LLMRouter,
) -> str:
    """One small reduce call: take the section titles + a one-line gist of
    each, produce the executive summary that opens the report. Cheap (no
    citations to verify — purely framing prose)."""
    if not sections:
        return "*(No sections were generated.)*"
    outline = "\n".join(
        f"- {s.title}: {(s.markdown.strip().split('.')[0] or s.title)[:140]}"
        for s in sections
    )
    instruction = _COHERENCE_PROMPT.format(query=query, outline=outline)
    try:
        resp = await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=[LLMMessage(role="user", content=instruction)],
                temperature=0.0,
                max_tokens=400,
            )
        )
        return resp.text.strip() or f"This report investigates: {query}"
    except Exception:  # noqa: BLE001 — degrade to a generic frame
        return f"This report investigates: {query}"
