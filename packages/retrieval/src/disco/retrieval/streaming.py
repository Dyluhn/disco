"""Streaming research-answer pipeline (Stage 4) — the capstone.

Composes the live providers (Stage 3) + the router (Stage 1) into the
`token → final` grounded-answer frame stream the frontend already renders
(frontend/src/types/grounded.ts): rewrite is implicit, search → extract →
rerank → constrained streamed generation (RAG_ANSWERER) → NLI-verify each claim
→ honest failure. Yields plain JSON-able dicts in the frontend's frame shape, so
the agent/app server just forwards them over the WebSocket.

This is the function that replaces the research FIXTURE with real, grounded,
cited answers from real sources.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from disco.core.events import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from .local_encoders import EncoderUnavailable
from .models import ExtractedDoc, Passage, RetrievalRequest, SearchHit
from .providers import ExtractionProvider, SearchProvider
from .ranking import Embedder, QueryRewriter, Reranker
from .vectorstore import VectorStore

# NLI verdict (3-way) -> the UI's claim band.
_VERDICT = {"entail": "supported", "neutral": "weak", "contradict": "unsupported"}
_CITE = re.compile(r"\[\[([\w-]+)\]\]")
_SENT = re.compile(r"(?<=[.!?])\s+")
# A citation CLUSTER (one or more [[id]] together) preceded by the prose it cites.
# The model is told to cite at the END of each sentence, so the marker usually
# follows the period ("...France. [[id]]") — capture the lead so we re-attach it.
_CLUSTER = re.compile(r"(.*?)((?:\[\[[\w-]+\]\]\s*)+)", re.DOTALL)
_MD = re.compile(r"[*`#_>]+")  # emphasis/code/heading marks — noise for NLI
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")  # ![alt](url) — never an NLI premise
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # [text](url) -> keep just the text


def _plain(text: str) -> str:
    """Strip markdown emphasis so the NLI hypothesis is clean prose."""
    return _MD.sub("", text).strip()


def _clean_premise(text: str) -> str:
    """Drop markdown image/link artifacts before NLI — they are not prose and the
    entailment model treats them as confident contradictions (pure noise)."""
    text = _MD_IMG.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    return _MD.sub("", text)


_ENTAIL_MIN = 0.5  # support needs a real entailment signal at the sentence level


def _best_entail(passage_text: str, claim: str, nli: _NLILike) -> tuple[str, float]:
    """Entailment of `claim` from a passage, scored at the SENTENCE level: NLI is
    sentence-pair trained, so a whole retrieved chunk dilutes the signal. The
    verdict comes from the MOST-RELEVANT sentence (the one that best entails the
    claim): a passage merely SILENT on a claim is weak (neutral); `unsupported` is
    reserved for when that on-topic sentence actually refutes it, not for a
    tangential different-fact sentence (which independent-sentence NLI mislabels
    as a contradiction). Markdown image/link artifacts are stripped first."""
    cleaned = _clean_premise(passage_text)
    sentences = [s.strip() for s in _SENT.split(cleaned) if len(s.strip()) > 15]
    sentences = sentences[:12] or [cleaned]  # bound NLI calls per claim
    best_score, best_label = -1.0, "neutral"
    for sent in sentences:
        score = nli.score(sent, claim)
        if score > best_score:
            best_score, best_label = score, nli.entail(sent, claim)
    if best_score >= _ENTAIL_MIN:
        return "entail", best_score
    if best_label == "contradict":  # the best-matching sentence itself refutes it
        return "contradict", max(best_score, 0.0)
    return "neutral", max(best_score, 0.0)


class _NLILike(Protocol):
    """The structural NLI type research needs: 3-way entailment + a score. Both
    the live SidecarNLIVerifier and the stub satisfy it."""

    def entail(self, premise: str, hypothesis: str) -> str: ...
    def score(self, premise: str, hypothesis: str) -> float: ...


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


def _to_blocks(text: str) -> list[dict]:
    """Split the generated markdown into the frontend's AnswerBlock shape. Minimal:
    headings (#..), fenced code, else prose; prose keeps its [[id]] markers and the
    cited_passage_ids parsed from them."""
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

    def is_table_divider(line: str) -> bool:
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            return False
        # Must contain at least one dash/colon per cell
        parts = [p.strip() for p in line.split("|")][1:-1]
        return all(re.match(r"^[:\- ]+$", p) and "-" in p for p in parts)

    def parse_table_row(line: str) -> list[str]:
        return [p.strip() for p in line.strip().split("|")][1:-1]

    while i < len(lines):
        line = lines[i]
        # 1. Fenced code
        if line.strip().startswith("```"):
            flush_prose()
            lang = line.strip().lstrip("`").strip() or "text"
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            code_str = "\n".join(code_lines)
            if lang == "chart":
                try:
                    import json

                    payload = json.loads(code_str)
                    if isinstance(payload, dict) and "chart_type" in payload:
                        blocks.append(
                            {
                                "kind": "chart",
                                "id": f"b{n}",
                                "chart_type": payload.get("chart_type"),
                                "data": payload.get("data"),
                                "title": payload.get("title"),
                                "x_label": payload.get("x_label"),
                                "y_label": payload.get("y_label"),
                                "cited_passage_ids": sorted(set(_CITE.findall(code_str))),
                            }
                        )
                        n += 1
                        continue
                except Exception:  # noqa: BLE001
                    pass  # fall back to code block
            blocks.append(
                {"kind": "code", "id": f"b{n}", "language": lang, "code": code_str}
            )
            n += 1
            continue
        # 2. GFM Table
        if line.strip().startswith("|") and i + 2 < len(lines):
            # Potential table: header | divider | data
            if is_table_divider(lines[i + 1]):
                header = parse_table_row(lines[i])
                if header:
                    j = i + 2
                    rows = []
                    while j < len(lines) and lines[j].strip().startswith("|"):
                        row = parse_table_row(lines[j])
                        if len(row) > 0:
                            # Pad or truncate row to match header length
                            if len(row) < len(header):
                                row += [""] * (len(header) - len(row))
                            else:
                                row = row[: len(header)]
                            rows.append(row)
                            j += 1
                        else:
                            break
                    if rows:
                        flush_prose()
                        # Extract citations from all cells (header + all rows)
                        cells = header + [c for r in rows for c in r]
                        table_text = " ".join(cells)
                        cited_ids = sorted(set(_CITE.findall(table_text)))
                        blocks.append(
                            {
                                "kind": "table",
                                "id": f"b{n}",
                                "columns": header,
                                "rows": rows,
                                "cited_passage_ids": cited_ids,
                            }
                        )
                        n += 1
                        i = j
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


def _verify_claims(text: str, by_id: dict[str, Passage], nli: _NLILike) -> list[dict]:
    """Each cited sentence → a verified claim (NLI against its cited passage). A
    claim is the sentence that PRECEDES a citation cluster — the marker may sit
    after the sentence's period, so we re-attach it to that sentence."""
    out: list[dict] = []
    for m in _CLUSTER.finditer(text):
        lead, cluster = m.group(1), m.group(2)
        ids = _CITE.findall(cluster)
        if not ids:
            continue
        # the claim is the LAST sentence of the lead (earlier uncited sentences
        # in the lead belong to their own clusters / are not verifiable claims).
        sentences = [s for s in _SENT.split(lead) if s.strip()]
        # take only the last physical line so a heading sitting directly above the
        # sentence ("### Capital City\nCanberra is…") doesn't bleed into the claim.
        last = (sentences[-1] if sentences else lead).splitlines()[-1]
        claim_text = _CITE.sub("", last).strip(" .\n")
        if not claim_text:
            continue
        hypothesis = _plain(claim_text)  # NLI sees clean prose, not markdown
        best_id, best_score, best_verdict = None, -1.0, "neutral"
        for pid in ids:
            passage = by_id.get(pid)
            if passage is None:
                continue
            verdict, score = _best_entail(passage.text, hypothesis, nli)
            if score > best_score:
                best_id, best_score, best_verdict = pid, score, verdict
        out.append(
            {
                "claim": {"text": claim_text, "cited_passage_ids": ids},
                "verdict": _VERDICT.get(best_verdict, "weak"),
                "best_passage_id": best_id,
                "entailment_score": max(best_score, 0.0),
            }
        )
    return out


def _drop_weak(answer: dict) -> dict:
    """Re-scope 'drop weak': keep only supported claims and strip the citation
    markers of passages cited by the dropped (weak/unsupported) claims, so the
    prose no longer points at sources that didn't hold up. Mirrors the offline
    fixture's applyScope so live and offline behave identically."""
    drop_ids = {
        pid
        for c in answer["claims"]
        if c["verdict"] != "supported"
        for pid in c["claim"]["cited_passage_ids"]
    }
    if not drop_ids:
        return answer
    marker = re.compile(r"\s*\[\[(?:" + "|".join(re.escape(i) for i in drop_ids) + r")\]\]")
    blocks = []
    for b in answer["blocks"]:
        if b.get("kind") == "prose":
            kept = [i for i in b.get("cited_passage_ids", []) if i not in drop_ids]
            blocks.append({**b, "text": marker.sub("", b["text"]), "cited_passage_ids": kept})
        else:
            blocks.append(b)
    return {
        **answer,
        "blocks": blocks,
        "claims": [c for c in answer["claims"] if c["verdict"] == "supported"],
        "unsupported_count": 0,
    }


async def _follow_ups(router: LLMRouter, query: str, answer: str) -> list[str]:
    try:
        req = CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.SUMMARIZER),
            messages=[
                LLMMessage(
                    role="user",
                    content=(
                        f"Given this question and answer, suggest 3 short follow-up questions, "
                        f"one per line, no numbering.\n\nQ: {query}\n\nA: {answer[:1500]}"
                    ),
                )
            ],
            temperature=0.3,
            max_tokens=160,
        )
        resp = await router.complete(req)
        lines = [ln.strip(" -•\t") for ln in resp.text.splitlines() if ln.strip()]
        return lines[:3]
    except Exception:  # noqa: BLE001 — follow-ups are optional polish
        return []


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

    When `rewriter` is provided and `max_research_rounds > 1`, the pipeline
    detects a no-answer result (zero supported claims after NLI verification) and
    reformulates the query for up to `min(max_research_rounds - 1, 2)` extra
    rounds. Token frames are buffered and only emitted for the accepted round so
    the user never sees a doomed draft. A lightweight `{"type":"phase",
    "phase":"reformulating"}` frame is emitted between rounds so the UI shows
    activity. The OFF-path (≥1 supported claim on round 0, or the default
    `max_research_rounds=1`) is byte-identical to the pre-F1 code — the retry
    block is unreachable when no extra rounds are allowed.

    ``seed_passages`` and ``corpus_ids`` add local evidence before rerank."""
    # F1: cap extra rounds at 2 regardless of the caller's value.
    bounded_extra = min(max(max_research_rounds - 1, 0), 2)

    yield {"type": "state", "status": "running"}
    try:
        exclude_urls: frozenset[str] = frozenset()
        current_query = query

        for round_idx in range(1 + bounded_extra):
            is_last_round = round_idx == bounded_extra

            # 1. discovery (honoring the denied domains from re-scope)
            hits = await search.search(
                current_query, limit=discover_limit, domains_deny=domains_deny or None
            )
            if not hits:
                yield {"type": "error", "message": "No search results were found for this query."}
                return

            # 2. extraction (with provenance + honest failure status). Extraction
            # failures cluster under load (Crawl4AI does real browser crawls), so a
            # whole batch can blip to empty — retry once with a wider net (more hits)
            # before giving up, since the next results are usually readable too.
            # On re-search rounds, skip URLs already extracted in prior rounds so we
            # fetch fresh sources.
            candidate_hits = [h for h in hits if h.url not in exclude_urls] or hits
            urls = [h.url for h in candidate_hits[:extract_cap]]
            docs = await extraction.extract_many(urls)
            passages = [p for d in docs if d.fetched_ok for p in d.passages]
            if not passages and len(candidate_hits) > extract_cap:
                extra = await extraction.extract_many(
                    [h.url for h in candidate_hits[extract_cap:discover_limit]]
                )
                docs = docs + extra
                passages = [p for d in docs if d.fetched_ok for p in d.passages]
            status_by_url = {d.url: d.status for d in docs}
            all_hits = [{**h.model_dump(), "status": status_by_url.get(h.url)} for h in hits]
            this_round_urls = frozenset(h.url for h in hits)

            # 3a. Inject local evidence BEFORE the empty-passage early-exit so
            # uploads and Spaces can answer even when web extraction fails.
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
                yield {
                    "type": "error",
                    "message": _unreadable_sources_message(hits, docs),
                }
                return

            # 3b. rerank to the working set (web passages + any seeds that
            # survived the dedup above).
            top = await reranker.rerank(current_query, passages, top_k=top_k)
            by_id = {p.id: p for p in top}

            # 4. constrained generation — buffer tokens; only emit on the accepted round.
            # This keeps a doomed first draft off the wire while still letting the final
            # accepted answer stream token-by-token to the frontend.
            token_frames: list[dict[str, Any]] = []
            answer_text = ""
            in_think = False
            carry = ""
            async for chunk in router.stream_complete(
                CompletionRequest(
                    profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                    messages=_answer_prompt(current_query, top),
                    temperature=0.0,
                    # `think=False` (default) turns the reasoning model's thinking OFF so it
                    # answers directly and cites. Otherwise a reasoning model (Qwen3.6 et al)
                    # spends its whole budget in `reasoning_content`, hits the token ceiling
                    # mid-thought, and `content` comes back EMPTY — no claims, empty prose
                    # (the canary's "up but not grounding" failure). `think=True` keeps it on
                    # with a bigger budget for a deeper, reasoned answer.
                    enable_thinking=think,
                    max_tokens=4096 if think else 1200,
                    stream=True,
                )
            ):
                if chunk.delta_text:
                    answer_text += chunk.delta_text
                    # Streaming think-guard: withhold <think>…</think> spans from the
                    # wire (a leaked reasoning preamble must never paint as answer).
                    carry += chunk.delta_text
                    emit = ""
                    while carry:
                        if in_think:
                            end = carry.lower().find("</think>")
                            if end == -1:
                                carry = carry[-16:]  # keep a tail in case the tag splits
                                break
                            carry = carry[end + len("</think>"):]
                            in_think = False
                            continue
                        start = carry.lower().find("<think>")
                        if start == -1:
                            # hold back a small tail in case "<think>" straddles chunks
                            keep = max(0, len(carry) - 8)
                            emit += carry[:keep]
                            carry = carry[keep:]
                            break
                        emit += carry[:start]
                        carry = carry[start + len("<think>"):]
                        in_think = True
                    if emit:
                        token_frames.append(
                            {"type": "token", "token": emit, "block_id": "answer"}
                        )

            # 5. structure + verify. The NLI verifier is SYNC (blocking httpx), so run
            # it in a thread — otherwise its many calls freeze the event loop and the
            # WebSocket's keepalive pings time out, dropping the connection mid-answer.
            answer_text = _strip_think_spans(answer_text)
            blocks = _to_blocks(answer_text)
            claims = await asyncio.to_thread(_verify_claims, answer_text, by_id, nli)

            # F1 no-answer signal: zero supported claims after NLI verification.
            # Also catches an all-unsupported/neutral answer (equally "not grounded").
            supported = sum(1 for c in claims if c["verdict"] == "supported")

            if supported > 0 or rewriter is None or is_last_round:
                # Accepted round — emit the buffered tokens, then the final frames.
                for frame in token_frames:
                    yield frame
                follow_ups = await _follow_ups(router, query, answer_text)
                answer = {
                    "query": query,
                    "blocks": blocks,
                    "claims": claims,
                    "passages": [p.model_dump() for p in top],
                    "all_hits": all_hits,
                    "unsupported_count": sum(1 for c in claims if c["verdict"] == "unsupported"),
                    "follow_ups": follow_ups,
                }
                if drop_weak:
                    answer = _drop_weak(answer)
                yield {"type": "final", "answer": answer}
                yield {"type": "state", "status": "finished"}
                return

            # No supported claims + rewriter available + rounds remain → reformulate.
            exclude_urls = exclude_urls | this_round_urls
            yield {"type": "phase", "phase": "reformulating"}
            new_qs = await rewriter.rewrite(current_query, n=1)
            current_query = new_qs[0] if new_qs else current_query
            # Buffered token_frames from the doomed round are discarded here.

    except EncoderUnavailable as exc:
        # RAM guard: the encoder pre-check stopped a model load that would OOM the
        # process.  Emit an honest, actionable error frame so the UI shows the real
        # reason rather than a dead socket or a silent empty-answer page.
        yield {"type": "error", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface the real reason, don't swallow
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
