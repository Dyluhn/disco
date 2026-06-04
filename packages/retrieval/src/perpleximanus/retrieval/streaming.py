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

from perpleximanus.core.events import LLMMessage
from perpleximanus.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)

from .models import Passage
from .providers import ExtractionProvider, SearchProvider
from .ranking import Reranker

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

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush_prose()
            lang = line.strip().lstrip("`").strip() or "text"
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append(
                {"kind": "code", "id": f"b{n}", "language": lang, "code": "\n".join(code)}
            )
            n += 1
            continue
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


async def stream_research_answer(
    query: str,
    *,
    router: LLMRouter,
    search: SearchProvider,
    extraction: ExtractionProvider,
    reranker: Reranker,
    nli: _NLILike,
    discover_limit: int = 10,
    extract_cap: int = 6,
    top_k: int = 6,
) -> AsyncIterator[dict[str, Any]]:
    """Yield the frontend's research frames for a live, grounded answer."""
    yield {"type": "state", "status": "running"}
    try:
        # 1. discovery
        hits = await search.search(query, limit=discover_limit)
        if not hits:
            yield {"type": "error", "message": "No search results were found for this query."}
            return
        # 2. extraction (with provenance + honest failure status). Extraction
        # failures cluster under load (Crawl4AI does real browser crawls), so a
        # whole batch can blip to empty — retry once with a wider net (more hits)
        # before giving up, since the next results are usually readable too.
        urls = [h.url for h in hits[:extract_cap]]
        docs = await extraction.extract_many(urls)
        passages = [p for d in docs if d.fetched_ok for p in d.passages]
        if not passages and len(hits) > extract_cap:
            extra = await extraction.extract_many([h.url for h in hits[extract_cap:discover_limit]])
            docs = docs + extra
            passages = [p for d in docs if d.fetched_ok for p in d.passages]
        status_by_url = {d.url: d.status for d in docs}
        all_hits = [{**h.model_dump(), "status": status_by_url.get(h.url)} for h in hits]
        if not passages:
            unreadable = sum(1 for d in docs if d.status in ("blocked", "paywalled", "not_found"))
            yield {
                "type": "error",
                "message": (
                    f"Found {len(hits)} sources but couldn't read any of them right now "
                    f"({unreadable} blocked or paywalled, the rest failed to fetch). This is "
                    "usually a temporary extraction hiccup — try the search again."
                ),
            }
            return
        # 3. rerank to the working set
        top = await reranker.rerank(query, passages, top_k=top_k)
        by_id = {p.id: p for p in top}

        # 4. constrained, streamed generation
        answer_text = ""
        async for chunk in router.stream_complete(
            CompletionRequest(
                profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                messages=_answer_prompt(query, top),
                temperature=0.0,
                max_tokens=1200,
                stream=True,
            )
        ):
            if chunk.delta_text:
                answer_text += chunk.delta_text
                yield {"type": "token", "token": chunk.delta_text, "block_id": "answer"}

        # 5. structure + verify. The NLI verifier is SYNC (blocking httpx), so run
        # it in a thread — otherwise its many calls freeze the event loop and the
        # WebSocket's keepalive pings time out, dropping the connection mid-answer.
        blocks = _to_blocks(answer_text)
        claims = await asyncio.to_thread(_verify_claims, answer_text, by_id, nli)
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
        yield {"type": "final", "answer": answer}
        yield {"type": "state", "status": "finished"}
    except Exception as exc:  # noqa: BLE001 — surface the real reason, don't swallow
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
