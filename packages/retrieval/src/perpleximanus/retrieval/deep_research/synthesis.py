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
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import jsonschema
from perpleximanus.core import LLMMessage, ReportSection
from perpleximanus.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)

from ..models import Passage
from ..ranking import Embedder
from ..streaming import _to_blocks, _verify_claims  # reuse — per-claim NLI verifier
from ..vectorstore import VectorStore
from .gather import SubQuestionResult

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "chart_type": {"enum": ["bar", "line", "pie", "scatter"]},
        "data": {
            "type": "array",
            "items": {
                "type": "object",
                "anyOf": [
                    {
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "number"},
                        },
                        "required": ["label", "value"],
                    },
                    {
                        "properties": {
                            "x": {"type": ["number", "string"]},
                            "y": {"type": "number"},
                            "group": {"type": "string"},
                        },
                        "required": ["x", "y"],
                    },
                ],
            },
        },
        "title": {"type": "string"},
        "x_label": {"type": "string"},
        "y_label": {"type": "string"},
    },
    "required": ["chart_type", "data"],
}


def _validate_charts(markdown: str) -> str:
    """Find ```chart blocks, validate their JSON, and if invalid, degrade them
    to standard markdown tables (or prose if table conversion fails) so the
    UI never receives a broken chart. Handles multiple charts per section."""
    blocks = re.split(r"(```chart\n.*?```)", markdown, flags=re.DOTALL)
    out = []
    for block in blocks:
        if block.startswith("```chart\n"):
            try:
                code = block[9:-3].strip()
                payload = json.loads(code)
                jsonschema.validate(instance=payload, schema=CHART_SCHEMA)
                out.append(block)
            except Exception:  # noqa: BLE001
                # Degrade to table if possible
                try:
                    p = json.loads(block[9:-3].strip())
                    data = p.get("data", [])
                    if p.get("chart_type") == "scatter":
                        cols = ["Group", p.get("x_label", "X"), p.get("y_label", "Y")]
                        rows = [[str(d.get("group", "")), str(d.get("x", "")), str(d.get("y", ""))] for d in data]
                    else:
                        cols = [p.get("x_label", "Label"), p.get("y_label", "Value")]
                        rows = [[str(d.get("label", d.get("x", ""))), str(d.get("value", d.get("y", "")))] for d in data]
                    
                    if not rows:
                        continue

                    table = f"| {' | '.join(cols)} |\n| {' | '.join(['---'] * len(cols))} |\n"
                    for r in rows:
                        table += f"| {' | '.join(r)} |\n"
                    out.append(f"\n{table}\n")
                except Exception: # noqa: BLE001
                    continue # just drop the broken chart
        else:
            out.append(block)
    return "".join(out)


_SECTION_PROMPT = (
    "You are writing one section of an analytical research report. Your job is "
    "to SYNTHESIZE across the cited sources — not summarize them serially.\n\n"
    "Section topic: {topic}\n\n"
    "SOURCES (use ONLY these — every factual sentence must end in [[id]] "
    "citations):\n{passages}\n\n"
    "WRITE THE SECTION. Follow these rules carefully — they are what "
    "distinguish analysis from a sourced-summary:\n\n"
    "1. SYNTHESIZE, don't summarize. Connect, compare, and weigh across "
    "sources within the section. Where sources CONVERGE, say so and cite the "
    "agreement [[id1]] [[id2]]. Where they DIVERGE — different numbers, "
    "different timelines, conflicting claims — surface the disagreement "
    "explicitly with both citations. Do NOT write paragraph after paragraph of "
    "'Source A says X [[a]]. Source B says Y [[b]].' Connect the cited claims "
    "into an argument that answers the section topic.\n\n"
    "2. ATTRIBUTE vendor claims; assert independent findings. There is a "
    "difference between (a) a vendor promoting its own product, (b) a market "
    "research firm's projection, and (c) a peer-reviewed measured result. "
    "  - For (a): write 'Samsung has ANNOUNCED a battery PROMISING a 600-mile "
    "range', NOT 'solid-state batteries deliver a 600-mile range.' "
    "  - For (b): 'The market is PROJECTED to grow at 39% CAGR through 2030 "
    "[[id]]', not 'the market will grow.' "
    "  - For (c): peer-reviewed measured findings can be stated more "
    "directly. "
    "If a citation comes from a company promoting its own tech, mark it as a "
    "claim, not a fact.\n\n"
    "3. MEASURED, ANALYTICAL REGISTER. Cut these words and any like them: "
    "'transformative,' 'revolutionary,' 'poised to revolutionize,' 'pivotal,' "
    "'game-changing,' 'breakthrough,' 'paradigm shift,' 'cutting-edge.' "
    "Describe, weigh, and qualify. The reader is an analyst, not a marketer.\n\n"
    "4. FOREGROUND TENSION AND UNCERTAINTY. For maturing-but-overhyped tech, "
    "the gap between announcements and shipping reality is OFTEN THE FINDING. "
    "Where the field disagrees, where projected timelines slip, where the "
    "evidence is thin or one-sided — surface it, don't smooth it over. "
    "Tensions and uncertainty go in the section body, not hidden in footnotes.\n\n"
    "5. STAY GROUNDED. Every factual claim ends with [[id]] citations to the "
    "passages above. Cross-source observations (agreement/conflict) ARE "
    "themselves grounded — cite the sources you're comparing. If you draw an "
    "inference BEYOND what any single source says (a pattern across them, an "
    "implication, a judgement of significance), introduce it with a phrase "
    "like 'Taken together,' or 'The evidence suggests,' or 'This report's "
    "assessment is that' — so a reader can distinguish your inference from a "
    "cited fact. The inference still draws on cited sources, but its STATUS "
    "as an inference is flagged.\n\n"
    "6. VISUALIZE DATA. If sources provide multiple numerical data points "
    "suitable for comparison (trends, shares, distributions), include a chart. "
    "Use this exact format:\n"
    "```chart\n"
    "{{\n"
    "  \"chart_type\": \"bar\" | \"line\" | \"pie\" | \"scatter\",\n"
    "  \"title\": \"Chart Title\",\n"
    "  \"x_label\": \"Label for X axis\",\n"
    "  \"y_label\": \"Label for Y axis\",\n"
    "  \"data\": [{{\"label\": \"A\", \"value\": 10}}, {{\"label\": \"B\", \"value\": 20}}] \n"
    "  // OR for scatter: \"data\": [{{\"x\": 1, \"y\": 2, \"group\": \"A\"}}]\n"
    "}}\n"
    "```\n\n"
    "FORMAT: 4–7 short paragraphs of markdown. No section header (the report "
    "renders one). Every factual sentence ends in [[id]]."
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

        # CHART VALIDATION & RETRY
        chart_matches = re.findall(r"```chart\n(.*?)\n```", markdown, re.DOTALL)
        if chart_matches:
            invalid_errors = []
            for m in chart_matches:
                try:
                    p = json.loads(m.strip())
                    jsonschema.validate(instance=p, schema=CHART_SCHEMA)
                except Exception as e:
                    invalid_errors.append(str(e))

            if invalid_errors:
                # One retry with error trace
                retry_msg = (
                    "Your previous output contained invalid chart JSON:\n"
                    f"{' ; '.join(invalid_errors)}\n\n"
                    "Please rewrite the section, ensuring all ```chart blocks "
                    "strictly follow the schema provided in rule 6. If you cannot "
                    "fix the chart, use a standard markdown table instead."
                )
                try:
                    resp = await router.complete(
                        CompletionRequest(
                            profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                            messages=[
                                LLMMessage(role="user", content=instruction),
                                LLMMessage(role="assistant", content=markdown),
                                LLMMessage(role="user", content=retry_msg),
                            ],
                            temperature=0.0,
                            max_tokens=1400,
                        )
                    )
                    markdown = resp.text.strip()
                except Exception:  # noqa: BLE001
                    pass  # Keep the first version if retry fails

        # Final safety validation (degrade invalid charts to tables)
        markdown = _validate_charts(markdown)

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
    "FORMAT: 2–4 short paragraphs of markdown. No headers. No new claims "
    "beyond what the sections established — the summary distills."
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
    # Give the coherence pass enough to write findings, not structure: the
    # first ~2 sentences of each section (the lead findings), the section's
    # confidence rating (mixed/low means a tension the summary should surface),
    # and any disputed_notes the section flagged.
    outline_lines = []
    for s in sections:
        lead = ". ".join(s.markdown.strip().split(". ")[:2])[:320] or s.title
        # strip [[id]] markers — clutter for the summary writer
        lead = re.sub(r"\[\[[\w-]+\]\]", "", lead).replace("  ", " ").strip()
        marks = []
        if s.confidence in ("mixed", "low"):
            marks.append(f"confidence={s.confidence}")
        if s.disputed_notes:
            marks.append(f"flagged disagreement: {s.disputed_notes[0][:100]}")
        suffix = f"  [{'; '.join(marks)}]" if marks else ""
        outline_lines.append(f"- **{s.title}** — {lead}{suffix}")
    outline = "\n".join(outline_lines)
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
