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
import datetime
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, cast

import httpx
import jsonschema
from disco.core import LLMMessage, ReportSection
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    LLMTransientError,
    ModelRole,
)
from disco.core.think import strip_think_spans

from ..models import Passage
from ..ranking import Embedder
from ..streaming import _verify_claims  # reuse — per-claim NLI verifier
from ..vectorstore import VectorStore
from .gather import GatherLegContext, SubQuestionResult


class _RouterWithCtx(Protocol):
    """Local Protocol that mirrors `DefaultLLMRouter.complete` / `.stream_complete`.

    `disco.core.llm.LLMRouter` (the public Protocol used as a type hint
    throughout retrieval) declares `complete(self, req)` with no
    `context=` kwarg, but the concrete `DefaultLLMRouter` (the only
    production implementation, and what every caller actually passes)
    accepts an optional `context: CallContext | None = None` keyword.
    The Protocol is a real omission in `disco.core.llm.routing` (a
    sibling package outside this work-order's scope) and is owned by
    another agent. Casting to this locally-declared Protocol is a
    typing-only narrowing — at runtime, `cast` is the identity, so
    behavior is byte-identical to the previous `router.complete(req,
    context=leg_context.call_context)` calls.

    Defined at module scope (not under `if TYPE_CHECKING:`) because
    `cast(_RouterWithCtx, router)` is *evaluated* at runtime, and the
    symbol must resolve in the module's globals for the call site to
    even execute. The Protocol class is otherwise inert at runtime."""

    async def complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> CompletionResponse: ...

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# W-11: a SMALL temperature bump for the section-synthesis PROSE only, so every
# report doesn't read as the same deterministic mould. This affects token
# sampling during generation; it does NOT touch the grounding contract — the
# prompt still requires a [[id]] citation on every factual sentence, and the
# per-claim NLI verification gate (`_verify_claims` → confidence/unsupported,
# below) runs unchanged AFTER generation. Factuality is enforced downstream
# regardless of temperature. The chart-fix retry stays deterministic (0.0) so a
# malformed-JSON repair is reproducible, and the coherence summary stays at 0.0.
_SYNTHESIS_TEMPERATURE = 0.4
_INITIAL_TRANSIENT_BACKOFF_S = 2.0
_EMPTY_RETRY_TRANSIENT_BACKOFF_S = 5.0

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
                        rows = [
                            [str(d.get("group", "")), str(d.get("x", "")), str(d.get("y", ""))]
                            for d in data
                        ]
                    else:
                        cols = [p.get("x_label", "Label"), p.get("y_label", "Value")]
                        rows = [
                            [
                                str(d.get("label", d.get("x", ""))),
                                str(d.get("value", d.get("y", ""))),
                            ]
                            for d in data
                        ]
                    
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


def _normalize_citations(markdown: str, valid_ids: set[str]) -> str:
    """BW-05 — promote bare single-bracket ``[id]`` citations to ``[[id]]`` when
    ``id`` is a known passage id. The synthesis prompt asks for ``[[id]]`` but
    models intermittently emit a single bracket; the frontend parsers and the
    backend cited-id extraction only match the double-bracket form, so a bare
    ``[id]`` leaks through as literal prose AND is never tracked/NLI-checked.

    Only ids actually present in ``valid_ids`` are rewritten, so prose brackets
    (markdown links, ``[1]`` footnote-style noise, bracketed asides) are left
    untouched."""
    if not valid_ids:
        return markdown

    def repl(m: re.Match[str]) -> str:
        return f"[[{m.group(1)}]]" if m.group(1) in valid_ids else m.group(0)

    # (?<!\[) / (?!\]) ensure we never touch an already-doubled [[id]].
    return re.sub(r"(?<!\[)\[([\w-]+)\](?!\])", repl, markdown)


def _is_transient_completion_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            LLMTransientError,
            TimeoutError,
            ConnectionError,
            httpx.TimeoutException,
            httpx.TransportError,
        ),
    ):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


async def _complete_with_transient_retry(
    router: LLMRouter,
    request: CompletionRequest,
    *,
    context: CallContext,
    transient_backoff_s: float,
) -> CompletionResponse:
    try:
        return await cast(_RouterWithCtx, router).complete(request, context=context)
    except Exception as exc:
        if not _is_transient_completion_error(exc):
            raise
        await asyncio.sleep(transient_backoff_s)
        return await cast(_RouterWithCtx, router).complete(request, context=context)


_DELIM_CELL_RE = re.compile(r"^\s*:?-{1,}:?\s*$")


def _is_delimiter_row(line: str) -> bool:
    """A GFM table delimiter row — every cell is ``-``/``:--``/``--:``/``:-:``."""
    s = line.strip()
    if "|" not in s or "-" not in s:
        return False
    inner = s.strip("|")
    cells = inner.split("|")
    return bool(cells) and all(_DELIM_CELL_RE.match(c) for c in cells)


def _normalize_table_row(line: str, ncols: int) -> str:
    """Re-emit a pipe row with leading/trailing pipes and exactly ``ncols``
    cells (pad short, clip long) so remark-gfm parses it."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = [c.strip() for c in inner.split("|")]
    if len(cells) < ncols:
        cells += [""] * (ncols - len(cells))
    elif len(cells) > ncols:
        cells = cells[:ncols]
    return "| " + " | ".join(cells) + " |"


def _repair_tables(markdown: str) -> str:
    """BW-07 — repair malformed GFM tables so remark-gfm parses them instead of
    falling back to a literal-pipe paragraph.

    For each run of consecutive pipe-bearing lines (outside fenced code blocks),
    this normalizes rows that are missing their leading/trailing pipe and injects
    a ``|---|`` delimiter row when a multi-column header is followed by data rows
    with no delimiter. A lone single pipe line (a truncated/header-only fragment,
    or just prose that happens to contain a ``|``) is left untouched — we only
    rewrite a run we are confident is a real table (>= 2 rows, header has >= 2
    cells, or it already carries a delimiter)."""
    lines = markdown.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence or "|" not in line:
            out.append(line)
            i += 1
            continue

        # Gather a run of consecutive pipe-bearing, non-fence, non-blank lines.
        run: list[str] = []
        j = i
        while j < n:
            lj = lines[j]
            if lj.lstrip().startswith("```") or "|" not in lj or not lj.strip():
                break
            run.append(lj)
            j += 1

        has_delim = any(_is_delimiter_row(r) for r in run)
        header_cells = len([c for c in run[0].strip().strip("|").split("|")])
        is_table = has_delim or (len(run) >= 2 and header_cells >= 2)
        if not is_table:
            out.extend(run)
            i = j
            continue

        # Real table: normalize. Determine column count from the header row.
        ncols = max(2, header_cells)
        rebuilt: list[str] = [_normalize_table_row(run[0], ncols)]
        body = run[1:]
        if body and _is_delimiter_row(body[0]):
            rebuilt.append("| " + " | ".join(["---"] * ncols) + " |")
            body = body[1:]
        else:
            rebuilt.append("| " + " | ".join(["---"] * ncols) + " |")
        for r in body:
            rebuilt.append(_normalize_table_row(r, ncols))
        out.extend(rebuilt)
        i = j
    return "\n".join(out)


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
    "claim, not a fact. "
    "NEVER refer to a source by its publishing PLATFORM (e.g. 'a Medium post', "
    "'a Reddit thread', 'a YouTube video', 'a Substack article'). Describe "
    "what the source IS instead: 'an independent analyst note', 'an industry "
    "report', 'a community discussion', 'a primary vendor statement'. The "
    "platform name is irrelevant noise — the content type and provenance are "
    "what matter.\n\n"
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
    "6. VISUALIZE WHEN IT AIDS COMPREHENSION. Offer a visual whenever it lets "
    "the reader grasp the section faster than prose would — not only for hard "
    "numbers. Use a CHART when the cited sources give comparable quantities "
    "(trends over time, shares, distributions, rankings). Use a compact "
    "markdown TABLE to line a few entities up across the same handful of "
    "attributes (e.g. approaches × tradeoffs, vendors × capabilities, options × "
    "criteria). Build the visual ONLY from values that actually appear in the "
    "cited sources — never invent, estimate, or round-fill a data point to "
    "complete one, and cite the sources behind it. If the data isn't really "
    "there, write prose instead. Charts use this exact format:\n"
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
    "FORMAT: roughly 3–7 short paragraphs of markdown, but VARY the structure "
    "to fit the content rather than forcing every section into the same shape. "
    "Where the section enumerates parallel items (options, criteria, steps, "
    "examples), a short bulleted or numbered list reads clearer than prose; "
    "where a couple of sentences carry the section's load-bearing finding, a "
    "one-line blockquote callout (markdown '> ') can foreground it; reach for a "
    "table or chart per rule 6 where it earns its place. Default to analytical "
    "prose — these are accents, not a checklist to fill. No section header (the "
    "report renders one). Every factual sentence — INCLUDING list items, table "
    "cells, and callouts — still ends in [[id]] citations."
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
    above the section body.

    A sentence qualifies only if ALL THREE hold:
    1. It contains a genuine conflict cue word (disagree / conflict / dispute /
       contradict / inconsistent / versus).
    2. It contains at least one [[id]] citation marker — so every surfaced note
       is grounded in a specific source, not a free-floating hedge.
    3. It is long enough to carry real content (> 40 chars), filtering out
       tautologies like "the sources leave important tensions unresolved."

    Dropped: the old `however[, ].+sources?` branch, which matched any hedging
    sentence ("However, the sources leave important tensions unresolved.") with
    no citation and no specific conflict — exactly the vacuous notes users
    complained about."""
    sentences = re.split(r"(?<=[.!?])\s+", markdown)
    # Anchor at the word START only so inflected forms match:
    # "contradicts", "conflicting", "disputed", "disagreement" etc. all qualify.
    cue = re.compile(
        r"\b(?:disagree|conflict|dispute|contradict|inconsistent|versus)",
        re.IGNORECASE,
    )
    citation = re.compile(r"\[\[[\w-]+\]\]")
    return [
        s.strip()
        for s in sentences
        if cue.search(s) and citation.search(s) and len(s) > 40
    ][:3]


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
    leg_context: GatherLegContext,
    recency_window: str | None = None,
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
    _synth_payload: dict[str, object] = {
        "section": sub_result.subq.title,
        "passages_used": len(passages),
    }
    if sub_result.subq.label is not None:
        _synth_payload["label"] = sub_result.subq.label
    await emit("synthesize_section", _synth_payload)
    if not passages:
        return ReportSection(
            id=section_id,
            title=sub_result.subq.title,
            markdown="*(No sources were gathered for this section.)*",
            confidence="low",
        )

    # DR-3 E4: date + recency directive (section synthesis).
    # When recency_window is None the preamble is empty → byte-identical to
    # a run without recency (the OFF assertion).
    _synth_preamble = ""
    if recency_window is not None:
        _today = datetime.date.today().isoformat()
        _label = "month" if recency_window == "month" else "week"
        _synth_preamble = (
            f"Today's date is {_today}. Focus on findings from the PAST {_label.upper()}. "
            f"Prefer recent data over historical baselines where sources differ.\n\n"
        )
    instruction = _synth_preamble + _SECTION_PROMPT.format(
        topic=sub_result.subq.title, passages=_format_passages(passages)
    )
    # Per-leg isolation: the synthesis LLM call is also scoped to this leg's
    # CallContext. The synthesis phase is serialized (one leg at a time per
    # the engine's single-depth LLM queue), so concurrent contamination is
    # impossible — but threading the per-leg context keeps the leg's cost
    # tracking coherent and makes the leg's identity visible at the router
    # for the entire gather→synth pipeline.
    leg_messages: list[LLMMessage] = [LLMMessage(role="user", content=instruction)]
    try:
        resp = await _complete_with_transient_retry(
            router,
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=leg_messages,
                temperature=_SYNTHESIS_TEMPERATURE,
                max_tokens=1400,
            ),
            context=leg_context.call_context,
            transient_backoff_s=_INITIAL_TRANSIENT_BACKOFF_S,
        )
        # EMPTY-RESPONSE RETRY. A section's `content` can come back empty even on a
        # successful call. The dominant cause in production is a REASONING model
        # (the rag_answerer is MiniMax/Qwen-class): the model spends its whole token
        # budget on hidden reasoning and returns `finish_reason=="length"` with an
        # EMPTY content channel — the truncation guard below can't recover it because
        # each continuation also reasons to exhaustion. A blank generation or a soft
        # refusal (`finish_reason=="stop"`, empty text) is the other path. Either way
        # the empty body was stored verbatim and the UI rendered a titled section
        # card with NO content (the blank last-section bug — observed live as a
        # stored section with markdown='' / cited=[] / confidence=low).
        #
        # Retry ONCE. When the empty was length-exhaustion, give the reasoning room
        # to finish AND still emit prose by widening the budget; otherwise reissue at
        # the normal cap. The final empty-section guard (after post-processing) still
        # catches anything that stays empty.
        #
        # Emptiness is judged POST think-strip: a think-only response (the whole
        # budget spent inside <think>) is exactly the length-exhaustion case this
        # retry exists for — raw-text non-emptiness must not mask it.
        if not strip_think_spans(resp.text):
            _retry_tokens = 2800 if resp.finish_reason == "length" else 1400
            try:
                resp = await _complete_with_transient_retry(
                    router,
                    CompletionRequest(
                        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                        messages=leg_messages,
                        temperature=_SYNTHESIS_TEMPERATURE,
                        max_tokens=_retry_tokens,
                    ),
                    context=leg_context.call_context,
                    transient_backoff_s=_EMPTY_RETRY_TRANSIENT_BACKOFF_S,
                )
            except Exception:  # noqa: BLE001 — handled by the empty-section guard
                pass
        # BW-07 (2) — TRUNCATION GUARD. The section cap is small; when a section
        # is cut off mid-content (`finish_reason=="length"`, often mid-table)
        # remark-gfm receives a half-table and renders raw pipes. Continue from
        # the cut point (bounded) so the section terminates on a clean boundary.
        #
        # NOTE: accumulate the RAW text (do NOT strip yet) — the trailing
        # whitespace IS the cut boundary. Stripping first makes
        # `markdown[-1:].isspace()` permanently False, so every continuation
        # glues directly and merges adjacent rows/words ("| a | 10 |" + "| b |"
        # -> "| a | 10 || b |", losing the row break). The final strip happens
        # once, after the loop. (Think spans are stripped span-only here so the
        # trailing cut boundary survives.)
        markdown = strip_think_spans(resp.text, keep_edge_whitespace=True)
        _cont = 0
        while resp.finish_reason == "length" and _cont < 2:
            _cont += 1
            try:
                resp = await cast(_RouterWithCtx, router).complete(
                    CompletionRequest(
                        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                        messages=[
                            LLMMessage(role="user", content=instruction),
                            LLMMessage(role="assistant", content=markdown.rstrip()),
                            LLMMessage(
                                role="user",
                                content=(
                                    "Your previous reply was cut off. Continue "
                                    "EXACTLY where you stopped — do not repeat any "
                                    "earlier text and do not add a preamble. If you "
                                    "were mid-table, finish the table."
                                ),
                            ),
                        ],
                        temperature=_SYNTHESIS_TEMPERATURE,
                        max_tokens=1400,
                    ),
                    context=leg_context.call_context,
                )
            except Exception:  # noqa: BLE001 — keep the partial section
                break
            extra = strip_think_spans(resp.text, keep_edge_whitespace=True)
            if not extra.strip():
                break
            stripped_extra = extra.lstrip()
            if markdown[-1:].isspace():
                # Cut fell on a line/word boundary — start the continuation on a
                # fresh line so a new table row or paragraph never merges into the
                # last one (the half-table case BW-07 exists to fix).
                markdown = markdown.rstrip() + "\n" + stripped_extra
            elif markdown[-1:] == "|" and stripped_extra[:1] == "|":
                # Cut landed AFTER a complete table row but BEFORE its trailing
                # newline: the previous chunk ends on a row-closing pipe and the
                # continuation opens a NEW row with a leading pipe. A direct glue
                # would fuse the two rows on one line ("| Alpha | 90 |" +
                # "| Beta | 85 |" -> "| Alpha | 90 || Beta | 85 |"); the adjacent
                # `||` across the boundary is the tell of a wrongly merged row.
                # Join on a fresh line so each row stays a distinct row. (A cut
                # mid-cell ends on a value, not a pipe — "| Alpha | 9" — and its
                # continuation opens on the value too — "0 |" — so it never trips
                # this branch and still glues directly below.)
                markdown = markdown + "\n" + stripped_extra
            else:
                # Cut landed mid-token/mid-cell — glue the fragments with no
                # separator so the partial token/cell completes ("| a | 10" +
                # "0 |" -> "| a | 100 |"), never inserting a spurious space.
                markdown = markdown + stripped_extra
        markdown = markdown.strip()

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
                # One retry with error trace. The retry's message list is also
                # built FRESH (per-leg, not shared with another leg) and is
                # scoped to this leg's CallContext. The retry sequence stays
                # inside this leg — it never crosses a sibling's leg_context.
                retry_msg = (
                    "Your previous output contained invalid chart JSON:\n"
                    f"{' ; '.join(invalid_errors)}\n\n"
                    "Please rewrite the section, ensuring all ```chart blocks "
                    "strictly follow the schema provided in rule 6. If you cannot "
                    "fix the chart, use a standard markdown table instead."
                )
                try:
                    resp = await cast(_RouterWithCtx, router).complete(
                        CompletionRequest(
                            profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                            messages=[
                                LLMMessage(role="user", content=instruction),
                                LLMMessage(role="assistant", content=markdown),
                                LLMMessage(role="user", content=retry_msg),
                            ],
                            temperature=0.0,
                            max_tokens=1400,
                        ),
                        context=leg_context.call_context,
                    )
                    markdown = strip_think_spans(resp.text)
                except Exception:  # noqa: BLE001
                    pass  # Keep the first version if retry fails

        # Final safety validation (degrade invalid charts to tables)
        markdown = _validate_charts(markdown)
        # BW-05 — promote bare [id] citations to [[id]] so they render + track.
        markdown = _normalize_citations(markdown, {p.id for p in passages})
        # BW-07 (1) — repair malformed/half tables so remark-gfm never sees one.
        markdown = _repair_tables(markdown)

    except Exception as exc:  # noqa: BLE001 — synthesis failure → honest empty
        return ReportSection(
            id=section_id,
            title=sub_result.subq.title,
            markdown=f"*(Section synthesis failed: {type(exc).__name__})*",
            confidence="low",
        )

    # EMPTY-SECTION GUARD (final). If the body is STILL empty after the
    # empty-response retry + the truncation/chart/table post-processing (e.g. the
    # only content was an invalid chart that _validate_charts dropped, or the
    # model just never produced prose), degrade HONESTLY. Never store a blank
    # body — the UI renders the section's title+confidence header unconditionally,
    # so an empty markdown shows as a titled card with no content (a false
    # affordance). This says plainly that the section had no usable output.
    if not markdown.strip():
        return ReportSection(
            id=section_id,
            title=sub_result.subq.title,
            markdown="*(This section could not be generated from the gathered sources.)*",
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
    recency_window: str | None = None,
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
    # DR-3 E4: date + recency preamble for coherence pass.
    _coh_preamble = ""
    if recency_window is not None:
        _today = datetime.date.today().isoformat()
        _label = "month" if recency_window == "month" else "week"
        _coh_preamble = (
            f"Today's date is {_today}. This research focused on the PAST {_label.upper()}. "
            f"Reflect that recency bias in the summary.\n\n"
        )
    instruction = _coh_preamble + _COHERENCE_PROMPT.format(query=query, outline=outline)
    try:
        resp = await router.complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=[LLMMessage(role="user", content=instruction)],
                temperature=0.0,
                max_tokens=400,
            )
        )
        return strip_think_spans(resp.text) or f"This report investigates: {query}"
    except Exception:  # noqa: BLE001 — degrade to a generic frame
        return f"This report investigates: {query}"
