"""Streaming research-answer pipeline (Stage 4) — the capstone.

Composes the live providers (Stage 3) + the router (Stage 1) into the
`token → final` grounded-answer frame stream the frontend already renders
(current/frontend/src/types/grounded.ts): rewrite is implicit, search → extract →
rerank → constrained streamed generation (RAG_ANSWERER) → NLI-verify each claim
→ honest failure. Yields plain JSON-able dicts in the frontend's frame shape, so
the agent/app server just forwards them over the WebSocket.

This is the function that replaces the research FIXTURE with real, grounded,
cited answers from real sources.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any, NamedTuple, cast

from disco.core.events import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from .engine import (
    _is_explicit_provider_failure,
    _trace_diagnostic,
    annotate_passage_dates,
    extract_discovered_hits,
)
from .grounding import (
    _CITE,
    _drop_weak,
    _is_gfm_table_divider,
    _NLILike,
    _remove_unknown_citation_claims,
    _verify_claims,
)
from .local_encoders import EncoderUnavailable
from .models import ExtractedDoc, Passage, RetrievalRequest, SearchHit
from .providers import ExtractionProvider, SearchProvider
from .ranking import Embedder, QueryRewriter, Reranker, deduplicate_search_hits
from .url_policy import source_url_key
from .vectorstore import VectorStore

_LOG = logging.getLogger(__name__)


def _answer_prompt(query: str, passages: Sequence[Passage]) -> list[LLMMessage]:
    numbered = "\n\n".join(f"[[{p.id}]] ({p.source_title}):\n{p.text}" for p in passages)
    system = (
        "You are a precise research assistant. Answer the question using ONLY the "
        "numbered SOURCES below. End EVERY factual sentence with the id(s) of the "
        "source(s) that support it, written exactly as [[id]] (e.g. [[ab12_p0]]). "
        "Do not invent ids. If the sources don't answer the question, say so. Use "
        "short markdown sections; be concise and factual."
    )
    user = (
        f"SOURCES:\n{numbered}\n\nQUESTION: {query}\n\n"
        "Grounded answer (markdown, cite with [[id]]):"
    )
    return [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)]


# Canonical strip lives in disco.core.think — one rule, no per-site regexes.
_strip_think_spans = strip_think_spans


def _parse_table_row(line: str) -> list[str]:
    return [p.strip() for p in line.strip().split("|")][1:-1]


def _consume_fence(lines: list[str], i: int, n: int) -> tuple[dict, int]:
    """Parse a fenced block starting at ``lines[i]`` (a line opening a ```` ``` ````
    fence). Returns the block — a ``chart`` block when the fence language is
    ``chart`` and its JSON validates, a ``code`` block otherwise — and the
    index just past the closing fence."""
    lang = lines[i].strip().lstrip("`").strip() or "text"
    i += 1
    code_lines: list[str] = []
    while i < len(lines) and not lines[i].strip().startswith("```"):
        code_lines.append(lines[i])
        i += 1
    i += 1  # skip closing fence
    code_str = "\n".join(code_lines)
    if lang == "chart":
        try:
            payload = json.loads(code_str)
            if isinstance(payload, dict) and "chart_type" in payload:
                return {
                    "kind": "chart",
                    "id": f"b{n}",
                    "chart_type": payload.get("chart_type"),
                    "data": payload.get("data"),
                    "title": payload.get("title"),
                    "x_label": payload.get("x_label"),
                    "y_label": payload.get("y_label"),
                    "cited_passage_ids": sorted(set(_CITE.findall(code_str))),
                }, i
        except Exception:  # noqa: BLE001
            pass  # fall back to code block
    return {"kind": "code", "id": f"b{n}", "language": lang, "code": code_str}, i


def _consume_table(lines: list[str], i: int, n: int) -> tuple[dict, int] | None:
    """Try to parse a GFM table (header | divider | data rows) starting at
    ``lines[i]``. Returns the table block + the index just past it, or None
    if this isn't a real table — the caller then falls through to heading/
    prose handling for the same line."""
    if not (lines[i].strip().startswith("|") and i + 2 < len(lines)):
        return None
    if not _is_gfm_table_divider(lines[i + 1]):
        return None
    header = _parse_table_row(lines[i])
    if not header:
        return None
    j = i + 2
    rows: list[list[str]] = []
    while j < len(lines) and lines[j].strip().startswith("|"):
        row = _parse_table_row(lines[j])
        if len(row) == 0:
            break
        # Pad or truncate row to match header length
        if len(row) < len(header):
            row += [""] * (len(header) - len(row))
        else:
            row = row[: len(header)]
        rows.append(row)
        j += 1
    if not rows:
        return None
    # Extract citations from all cells (header + all rows)
    cells = header + [c for r in rows for c in r]
    cited_ids = sorted(set(_CITE.findall(" ".join(cells))))
    block = {
        "kind": "table",
        "id": f"b{n}",
        "columns": header,
        "rows": rows,
        "cited_passage_ids": cited_ids,
    }
    return block, j


def _to_blocks(text: str) -> list[dict]:
    """Split the generated markdown into the frontend's AnswerBlock shape. Minimal:
    headings (#..), fenced code, GFM tables, else prose; prose keeps its [[id]]
    markers and the cited_passage_ids parsed from them."""
    blocks: list[dict] = []
    lines = text.split("\n")
    i = 0
    n = 0
    prose: list[str] = []

    def flush_prose() -> None:
        nonlocal n
        body = "\n".join(prose).strip()
        prose.clear()
        if body:
            blocks.append(
                {
                    "kind": "prose",
                    "id": f"b{n}",
                    "text": body,
                    "cited_passage_ids": sorted(set(_CITE.findall(body))),
                }
            )
            n += 1

    while i < len(lines):
        line = lines[i]
        # 1. Fenced code (and chart-fence specialization)
        if line.strip().startswith("```"):
            flush_prose()
            block, i = _consume_fence(lines, i, n)
            blocks.append(block)
            n += 1
            continue
        # 2. GFM Table
        table = _consume_table(lines, i, n)
        if table is not None:
            flush_prose()
            block, i = table
            blocks.append(block)
            n += 1
            continue
        # 3. Headings
        m = re.match(r"^(#{1,3})\s+(.*)", line.strip())
        if m:
            flush_prose()
            level = min(len(m.group(1)), 3)
            blocks.append(
                {
                    "kind": "heading",
                    "id": f"b{n}",
                    "text": m.group(2).strip(),
                    "level": max(2, level),
                }
            )
            n += 1
            i += 1
            continue
        if not line.strip() and prose:
            flush_prose()
        elif line.strip():
            prose.append(line)
        i += 1
    flush_prose()
    return blocks or [{"kind": "prose", "id": "b0", "text": text.strip(), "cited_passage_ids": []}]


async def _follow_ups(router: LLMRouter, query: str, answer: str) -> list[str]:
    prompt = (
        f"Given this question and answer, suggest 3 short follow-up questions, "
        f"one per line, no numbering.\n\nQ: {query}\n\nA: {answer[:1500]}"
    )
    try:
        for attempt, max_tokens in ((1, 160), (2, 2048)):
            req = CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=max_tokens,
                enable_thinking=False,
                attempt=attempt,
            )
            resp = await router.complete(req)
            lines: list[str] = []
            for raw in resp.text.splitlines():
                item = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", raw).strip()
                if len(item) < 8 or item in lines:
                    continue
                lines.append(item)
            if len(lines) >= 2:
                return lines[:3]
            if attempt == 1:
                _LOG.warning(
                    "Follow-up model returned fewer than two questions; running one bounded repair"
                )
    except Exception as exc:  # noqa: BLE001 — retain useful deterministic fallbacks
        _LOG.warning("Follow-up generation failed; using deterministic fallbacks: %s", exc)

    _LOG.warning("Follow-up model remained empty; using deterministic fallbacks")
    subject = " ".join(query.strip().rstrip("?").split())[:140] or "this topic"
    return [
        f"What evidence would most strengthen the answer about {subject}?",
        f"Which exceptions or edge cases matter most for {subject}?",
        f"What related question about {subject} should be investigated next?",
    ]


async def _corpus_passages(
    req: RetrievalRequest,
    *,
    embedder: Embedder | None,
    vector_store: VectorStore | None,
) -> list[Passage]:
    if not req.corpus_ids or embedder is None or vector_store is None:
        return []
    qvec = (await embedder.embed([req.query]))[0]
    out: list[Passage] = []
    for namespace in req.corpus_ids:
        out.extend(await vector_store.query(namespace, qvec, top_k=req.top_k))
    return out


def _merge_unique_passages(
    passages: Sequence[Passage],
    extra: Sequence[Passage],
) -> list[Passage]:
    merged = list(passages)
    seen = {p.id for p in merged}
    for passage in extra:
        if passage.id in seen:
            continue
        seen.add(passage.id)
        merged.append(passage)
    return merged


async def _with_local_corpus_passages(
    passages: Sequence[Passage],
    *,
    current_query: str,
    top_k: int,
    seed_passages: Sequence[Passage],
    corpus_ids: frozenset[str],
    embedder: Embedder | None,
    vector_store: VectorStore | None,
) -> list[Passage]:
    merged = _merge_unique_passages(passages, seed_passages) if seed_passages else list(passages)
    if not corpus_ids:
        return merged
    corpus_req = RetrievalRequest(
        query=current_query,
        use_web=False,
        corpus_ids=corpus_ids,
        top_k=top_k,
    )
    return _merge_unique_passages(
        merged,
        await _corpus_passages(corpus_req, embedder=embedder, vector_store=vector_store),
    )


def _unreadable_sources_message(hits: Sequence[SearchHit], docs: Sequence[ExtractedDoc]) -> str:
    unreadable = sum(1 for d in docs if d.status in ("blocked", "paywalled", "not_found"))
    return (
        f"Found {len(hits)} sources but couldn't read any of them right now "
        f"({unreadable} blocked or paywalled, the rest failed to fetch). This is "
        "usually a temporary extraction hiccup — try the search again."
    )


def _drain_think_buffer(carry: str, in_think: bool) -> tuple[str, str, bool]:
    """Incrementally strip ``<think>...</think>`` spans from a growing token
    buffer. Returns the visible text to emit right now, plus the
    ``carry``/``in_think`` state to resume with on the next chunk — a whole
    retrieved chunk can straddle a tag boundary, so both the trailing partial
    tag and the open/closed state must survive across calls."""
    emit = ""
    while carry:
        if in_think:
            end = carry.lower().find("</think>")
            if end == -1:
                carry = carry[-16:]
                break
            carry = carry[end + len("</think>") :]
            in_think = False
            continue
        start = carry.lower().find("<think>")
        if start == -1:
            keep = max(0, len(carry) - 8)
            emit += carry[:keep]
            carry = carry[keep:]
            break
        emit += carry[:start]
        carry = carry[start + len("<think>") :]
        in_think = True
    return emit, carry, in_think


async def _generate_visible_answer(
    router: LLMRouter,
    query: str,
    passages: Sequence[Passage],
    *,
    think: bool,
) -> tuple[str, list[dict[str, Any]]]:
    """Generate one visible answer, with one bounded empty-content repair."""
    base_tokens = 4096 if think else 1200
    base_messages = _answer_prompt(query, passages)
    for generation_attempt in (1, 2):
        token_frames: list[dict[str, Any]] = []
        raw_answer = ""
        in_think = False
        carry = ""
        messages = base_messages
        if generation_attempt == 2:
            messages = [
                *base_messages,
                LLMMessage(
                    role="user",
                    content=(
                        "The previous completion returned no visible answer. "
                        "Return the final grounded answer now, with the required "
                        "source ids; do not return only reasoning."
                    ),
                ),
            ]
        async for chunk in router.stream_complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=messages,
                temperature=0.0,
                enable_thinking=think if generation_attempt == 1 else False,
                max_tokens=(base_tokens if generation_attempt == 1 else max(4096, base_tokens * 2)),
                attempt=generation_attempt,
                stream=True,
            )
        ):
            if not chunk.delta_text:
                continue
            raw_answer += chunk.delta_text
            carry += chunk.delta_text
            emit, carry, in_think = _drain_think_buffer(carry, in_think)
            if emit:
                token_frames.append({"type": "token", "token": emit, "block_id": "answer"})

        answer_text = _strip_think_spans(raw_answer).strip()
        if answer_text:
            streamed = "".join(frame["token"] for frame in token_frames)
            if answer_text.startswith(streamed):
                tail = answer_text[len(streamed) :]
                if tail:
                    token_frames.append({"type": "token", "token": tail, "block_id": "answer"})
            return answer_text, token_frames
        if generation_attempt == 1:
            _LOG.warning("Research answerer returned no visible text; running one bounded repair")
    return "", []


class _ResearchAborted(Exception):
    """Internal signal: one of `stream_research_answer`'s three honest-failure
    exits (no search hits, no readable passages, no visible answer) fired
    inside `_run_research_round`. Always caught inside
    `stream_research_answer` itself — the caller yields ONE error frame with
    this exception's exact message (no type-name prefix) and stops; it never
    escapes to the generic `except Exception` handler below, which DOES
    prefix the message and exists only for genuinely unexpected failures."""


class _RoundResult(NamedTuple):
    """Everything `stream_research_answer` needs from one accepted or
    rejected discover→extract→rerank→generate→verify round."""

    blocks: list[dict]
    claims: list[dict]
    top: list[Passage]
    all_hits: list[dict]
    supported: int
    this_round_urls: frozenset[str]
    token_frames: list[dict[str, Any]]
    answer_text: str


async def _discover_round(
    search: SearchProvider,
    current_query: str,
    domains_deny: frozenset[str],
    discover_limit: int,
) -> list[SearchHit]:
    """Search for this round (honoring the denied domains from re-scope);
    raises `_ResearchAborted` on zero hits."""
    detailed = getattr(search, "search_detailed", None)
    if callable(detailed):
        hits, diagnostic = await cast(Any, detailed)(
            current_query,
            limit=discover_limit,
            domains_deny=domains_deny or None,
        )
        if not hits and _is_explicit_provider_failure(diagnostic):
            bounded = _trace_diagnostic(diagnostic)
            raise _ResearchAborted(f"Search provider failed: {bounded}")
    else:
        hits = await search.search(
            current_query, limit=discover_limit, domains_deny=domains_deny or None
        )
    hits = deduplicate_search_hits(hits)[:discover_limit]
    if not hits:
        raise _ResearchAborted("No search results were found for this query.")
    return hits


def _passages_from_docs(docs: Sequence[ExtractedDoc]) -> list[Passage]:
    return [p for d in docs if d.fetched_ok for p in d.passages]


async def _extract_round_passages(
    extraction: ExtractionProvider,
    hits: Sequence[SearchHit],
    exclude_urls: frozenset[str],
    *,
    extract_cap: int,
    discover_limit: int,
) -> tuple[list[ExtractedDoc], list[Passage], list[dict], frozenset[str]]:
    """Extract readable passages for this round's hits (with provenance +
    honest failure status). Extraction failures cluster under load (Crawl4AI
    does real browser crawls), so a whole batch can blip to empty — retry
    once with a wider net (more hits) before giving up, since the next
    results are usually readable too. On re-search rounds, previously-
    extracted URLs are skipped so the round fetches fresh sources."""
    candidate_hits: list[SearchHit] = [
        h for h in hits if source_url_key(h.url) not in exclude_urls
    ] or list(hits)
    docs = await extract_discovered_hits(extraction, candidate_hits[:extract_cap])
    docs = annotate_passage_dates(docs, candidate_hits[:extract_cap])
    passages = _passages_from_docs(docs)
    if not passages and len(candidate_hits) > extract_cap:
        extra = await extract_discovered_hits(
            extraction, candidate_hits[extract_cap:discover_limit]
        )
        extra = annotate_passage_dates(extra, candidate_hits[extract_cap:discover_limit])
        docs = docs + extra
        passages = _passages_from_docs(docs)
    status_by_url = {source_url_key(d.url): d.status for d in docs}
    all_hits = [
        {**h.model_dump(mode="json"), "status": status_by_url.get(source_url_key(h.url))}
        for h in hits
    ]
    this_round_urls = frozenset(source_url_key(h.url) for h in hits)
    return docs, passages, all_hits, this_round_urls


async def _run_research_round(
    current_query: str,
    *,
    search: SearchProvider,
    extraction: ExtractionProvider,
    reranker: Reranker,
    router: LLMRouter,
    nli: _NLILike,
    think: bool,
    domains_deny: frozenset[str],
    discover_limit: int,
    extract_cap: int,
    top_k: int,
    exclude_urls: frozenset[str],
    seed_passages: Sequence[Passage],
    corpus_ids: frozenset[str],
    embedder: Embedder | None,
    vector_store: VectorStore | None,
) -> _RoundResult:
    """Run one discover → extract → rerank → generate → verify round. Raises
    `_ResearchAborted` for any of the pipeline's three honest-failure exits;
    the caller (`stream_research_answer`) yields the error frame and stops."""
    hits = await _discover_round(search, current_query, domains_deny, discover_limit)
    docs, passages, all_hits, this_round_urls = await _extract_round_passages(
        extraction, hits, exclude_urls, extract_cap=extract_cap, discover_limit=discover_limit
    )

    # Inject local evidence BEFORE the empty-passage early-exit so uploads and
    # Spaces can answer even when web extraction fails.
    passages = await _with_local_corpus_passages(
        passages,
        current_query=current_query,
        top_k=top_k,
        seed_passages=seed_passages,
        corpus_ids=corpus_ids,
        embedder=embedder,
        vector_store=vector_store,
    )
    if not passages:
        raise _ResearchAborted(_unreadable_sources_message(hits, docs))

    # rerank to the working set (web passages + any seeds that survived the
    # dedup above).
    top = await reranker.rerank(current_query, passages, top_k=top_k)
    by_id = {p.id: p for p in top}

    # constrained generation.
    answer_text, token_frames = await _generate_visible_answer(
        router, current_query, top, think=think
    )
    if not answer_text:
        _LOG.error("Research answerer returned no visible text after bounded repair")
        raise _ResearchAborted(
            "The answer model returned no visible answer after one bounded "
            "repair. Try another model or retry the search."
        )

    # An invented id has no provenance card and cannot be meaningfully graded.
    # Remove its complete statement before both rendering and verification.
    answer_text = _remove_unknown_citation_claims(answer_text, set(by_id))
    if not answer_text:
        raise _ResearchAborted(
            "The answer model cited only unknown sources. Try another model or retry the search."
        )

    # structure + verify. The NLI verifier is SYNC (blocking httpx), so run it
    # in a thread — otherwise its many calls freeze the event loop and the
    # WebSocket's keepalive pings time out, dropping the connection mid-answer.
    blocks = _to_blocks(answer_text)
    claims = await asyncio.to_thread(_verify_claims, answer_text, by_id, nli)
    # F1 no-answer signal: zero supported claims after NLI verification. Also
    # catches an all-unsupported/neutral answer (equally "not grounded").
    supported = sum(1 for c in claims if c["verdict"] == "supported")

    return _RoundResult(
        blocks=blocks,
        claims=claims,
        top=top,
        all_hits=all_hits,
        supported=supported,
        this_round_urls=this_round_urls,
        token_frames=token_frames,
        answer_text=answer_text,
    )


def _build_final_answer(query: str, result: _RoundResult, follow_ups: list[str]) -> dict[str, Any]:
    return {
        "query": query,
        "blocks": result.blocks,
        "claims": result.claims,
        "passages": [p.model_dump(mode="json") for p in result.top],
        "all_hits": result.all_hits,
        "unsupported_count": sum(1 for c in result.claims if c["verdict"] == "unsupported"),
        "follow_ups": follow_ups,
    }


async def stream_research_answer(
    query: str,
    *,
    router: LLMRouter,
    search: SearchProvider,
    extraction: ExtractionProvider,
    reranker: Reranker,
    nli: _NLILike,
    rewriter: QueryRewriter | None = None,
    max_research_rounds: int = 1,
    domains_deny: frozenset[str] = frozenset(),
    drop_weak: bool = False,
    think: bool = False,
    discover_limit: int = 10,
    extract_cap: int = 6,
    top_k: int = 6,
    seed_passages: Sequence[Passage] = (),
    corpus_ids: frozenset[str] = frozenset(),
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield the frontend's research frames for a live, grounded answer. The
    re-scope controls apply here: `domains_deny` filters discovery; `drop_weak`
    prunes the final answer to its supported claims; `think` raises the token
    budget so a reasoning model has room to think AND still emit the answer (with
    a small budget, reasoning eats it all and the content comes back empty).

    This ordinary Search surface is always one-shot. ``rewriter`` and
    ``max_research_rounds`` remain accepted for API compatibility with older
    callers, but are intentionally ignored; Deep Research owns any bounded
    multi-query planning in its separate engine.

    ``seed_passages`` and ``corpus_ids`` add local evidence before rerank."""
    # This is the ordinary Search answer path. It is deliberately one-shot for
    # every provider shape, including legacy providers without
    # ``search_detailed``. Deep Research owns its bounded multi-query loop in
    # the deep_research engine; allowing this surface to invoke a rewriter
    # creates a second planner/retry policy and makes a simple Search request
    # fan out unexpectedly.
    del max_research_rounds
    bounded_extra = 0

    yield {"type": "state", "status": "running"}
    try:
        exclude_urls: frozenset[str] = frozenset()
        current_query = query

        for _round_idx in range(1 + bounded_extra):
            try:
                result = await _run_research_round(
                    current_query,
                    search=search,
                    extraction=extraction,
                    reranker=reranker,
                    router=router,
                    nli=nli,
                    think=think,
                    domains_deny=domains_deny,
                    discover_limit=discover_limit,
                    extract_cap=extract_cap,
                    top_k=top_k,
                    exclude_urls=exclude_urls,
                    seed_passages=seed_passages,
                    corpus_ids=corpus_ids,
                    embedder=embedder,
                    vector_store=vector_store,
                )
            except _ResearchAborted as aborted:
                yield {"type": "error", "message": str(aborted)}
                return

            if result.supported > 0:
                # Accepted round — emit the buffered tokens, then the final frames.
                for frame in result.token_frames:
                    yield frame
                follow_ups = await _follow_ups(router, query, result.answer_text)
                answer = _build_final_answer(query, result, follow_ups)
                if drop_weak:
                    answer = _drop_weak(answer)
                yield {"type": "final", "answer": answer}
                yield {"type": "state", "status": "finished"}
                return

            yield {
                "type": "error",
                "message": (
                    "The available sources did not support any factual claim in "
                    "the generated answer. Try a broader query or different sources."
                ),
            }
            return

    except EncoderUnavailable as exc:
        # RAM guard: the encoder pre-check stopped a model load that would OOM the
        # process.  Emit an honest, actionable error frame so the UI shows the real
        # reason rather than a dead socket or a silent empty-answer page.
        yield {"type": "error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface the real reason, don't swallow
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
