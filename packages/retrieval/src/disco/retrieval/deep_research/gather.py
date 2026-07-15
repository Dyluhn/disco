"""Retrieve-reason-refine — the iterative gather loop, per sub-question.

For each sub-question, run UP TO `max_rounds_per_subq` rounds of:

    retrieve(subq_or_followup) → passages
    reason about coverage gaps  → either "sufficient" or follow-up queries
    embed + upsert passages into the per-run vector store (corpus accumulation)

Stop when the gap-reasoner says "sufficient" OR the round cap is hit OR the
whole-run source cap is hit. Each retrieval round emits one event (via the
`emit` callback the agent-server installs) so the UI's activity feed shows the
process — "Searching X", "Refining: looking for Y", "Gathered N sources" —
in real time.

The gap reasoner is a small QUERY_REWRITER call that takes the sub-question +
short summaries of what was found, and returns a structured "sufficient or
not + follow-up queries". This is the *reason* in retrieve-reason-refine: a
quality check between rounds, not blind multi-round.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

from disco.core import LLMMessage
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from ..engine import RetrievalEngine
from ..models import Passage, RetrievalRequest, SearchHit
from ..ranking import Embedder
from ..vectorstore import VectorStore
from .decompose import SubQuestion
from .depth import DepthBound


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
    context=call_context)` call.

    Defined at module scope (not under `if TYPE_CHECKING:`) because
    `cast(_RouterWithCtx, router)` is *evaluated* at runtime, and the
    symbol must resolve in the module's globals for the call site to
    even execute. The Protocol class is otherwise inert at runtime
    (no `__init__`, no methods that fire on import) so the cost is
    one extra class object per process — negligible."""

    async def complete(
        self, req: CompletionRequest, *, context: CallContext | None = None
    ) -> CompletionResponse: ...


# A small async hook the agent-server installs to write Action/Observation
# events to the conversation log as the gather progresses. The engine never
# touches the store directly — the agent-server owns persistence.
EmitFn = Callable[[str, dict[str, object]], Awaitable[None]]


@dataclass(frozen=True)
class GatherLegContext:
    """The ISOLATED sub-context for ONE gather leg.

    Every concurrent gather leg in `DeepResearchRun.run` gets its OWN
    `GatherLegContext`. No sibling leg's intermediate message / view /
    cost state bleeds in — the leg is a self-contained unit of work, and
    its accumulated state is merged with the others ONLY at the synthesis
    boundary (`synthesize_section` after the leg's gather task returns).

    The contract is enforced in three places:

      (a) The leg's `LLMMessage` list is built INDEPENDENTLY per call
          inside `_gap_reason` — a fresh `[LLMMessage(...)]` per call,
          never a shared mutable list, and seeded only with the system
          framing + that leg's task. No sibling messages are appended.

      (b) `call_context` (a per-leg `CallContext`) is passed to every
          `router.complete()` call inside the leg, so the router's cost
          tracker and per-conversation `model_override` are scoped to
          this leg. A leg that exhausts its budget cannot trip the hard
          cap out from under a sibling.

      (c) The leg's intermediate state — `seen_passage_ids`, `seen_urls`,
          `current_queries`, `result.passages`, `result.all_hits`,
          `result.issued_queries` — lives in leg-locals inside
          `gather_for_subquestion`. Nothing here is shared with siblings.

    Concurrency is preserved: each leg's `asyncio.create_task` is
    dispatched in the producer phase; the consumer phase `await`s them
    in plan order, but the leg's INTERNAL rounds run concurrently with
    other legs' rounds. The merge into the final report happens
    exclusively in `DeepResearchRun.run`'s reduce step, never inside
    the leg."""

    subq_id: str
    namespace: str
    call_context: CallContext


@dataclass
class SubQuestionResult:
    """What gathering produced for one sub-question: the passages relevant to
    it (across all rounds), the raw search hits seen (for the All-Searched
    panel), and the list of issued queries (for audit + progress display).

    `bounded_by_rounds` lights up when the round cap was hit before the gap
    reasoner declared sufficient — the section will be synthesized from what
    was found, with the bound surfaced honestly on the report."""

    subq: SubQuestion
    passages: list[Passage] = field(default_factory=list)
    all_hits: list[SearchHit] = field(default_factory=list)
    issued_queries: list[str] = field(default_factory=list)
    rounds_run: int = 0
    bounded_by_rounds: bool = False


_GAP_PROMPT = (
    "You are auditing research progress on one sub-question. "
    "Decide if the gathered material is SUFFICIENT to answer it, or if a "
    "follow-up search is needed to fill a gap.\n\n"
    "Sub-question: {subq}\n\n"
    "What's been gathered so far ({n_passages} passages):\n{summaries}\n\n"
    "Respond on EXACTLY two lines:\n"
    "  Line 1: either 'SUFFICIENT' or 'GAP: <one-sentence description of the gap>'\n"
    "  Line 2: one follow-up search query (or 'none' if sufficient)\n"
    "Do not add any other text."
)


async def _gap_reason(
    router: LLMRouter,
    subq: SubQuestion,
    passages: list[Passage],
    call_context: CallContext,
) -> tuple[bool, list[str], str]:
    """Returns (sufficient, follow_up_queries, rationale). One model call per
    round between rounds — cheap and pointed. Defaults are conservative: a
    malformed response is treated as 'sufficient' so we don't spin.

    Isolation note: the `messages=` list is built FRESH each call — a brand
    new list seeded only with the system framing (injected by the router)
    + this leg's task prompt. NO sibling leg's intermediate messages are
    appended; there is no shared mutable message list across legs. The
    per-leg `call_context` threads the leg's identity to the router so cost
    tracking / model_override are scoped to this leg."""
    if not passages:
        # Nothing yet — there's no coverage to be sufficient with. Drive one more
        # round with the original sub-question.
        return False, [subq.title], "no passages gathered yet"
    # short summaries — first ~140 chars of each, capped at 12 so the prompt
    # doesn't bloat. The reasoner only needs a sense of what's been covered.
    summaries = "\n".join(f"- [{p.id}] {p.text[:140].strip()}" for p in passages[:12])
    instruction = _GAP_PROMPT.format(subq=subq.title, n_passages=len(passages), summaries=summaries)
    # Build the message list INDEPENDENTLY for this call (no shared mutable
    # list). Seeded with this leg's task only — system framing is added by
    # the router's `_inject_prompt`. No sibling leg's messages appear here.
    leg_messages: list[LLMMessage] = [LLMMessage(role="user", content=instruction)]
    req = CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.QUERY_REWRITER),
        messages=leg_messages,
        temperature=0.0,
    )
    try:
        resp = await cast(_RouterWithCtx, router).complete(req, context=call_context)
    except Exception:  # noqa: BLE001 — gap-reason failure is recoverable
        return True, [], "gap reasoner failed; stopping further rounds"
    gap_text = strip_think_spans(resp.text)
    lines = [ln.strip() for ln in gap_text.splitlines() if ln.strip()]
    if not lines:
        return True, [], "empty gap-reason response; stopping"
    first = lines[0].upper()
    if first.startswith("SUFFICIENT"):
        return True, [], "gap reasoner: sufficient"
    rationale = re.sub(r"^GAP:\s*", "", lines[0], flags=re.IGNORECASE).strip()
    next_query = lines[1] if len(lines) > 1 else ""
    if not next_query or next_query.lower() == "none":
        # Said GAP but didn't provide a follow-up — stop, don't loop with no
        # new query.
        return True, [], rationale or "gap reasoner provided no follow-up query"
    return False, [next_query], rationale


async def gather_for_subquestion(
    subq: SubQuestion,
    *,
    engine: RetrievalEngine,
    router: LLMRouter,
    embedder: Embedder | None,
    vector_store: VectorStore,
    namespace: str,
    bound: DepthBound,
    emit: EmitFn,
    remaining_source_budget: int,
    leg_context: GatherLegContext,
    recency_window: Literal["month", "week"] | None = None,
    corpus_ids: frozenset[str] = frozenset(),
    extra_passages: list[Passage] = [],  # noqa: B006 — read-only default; safe
) -> SubQuestionResult:
    """Run the retrieve-reason-refine loop for one sub-question. Returns the
    accumulated result. Stops on: (a) gap-reasoner sufficient, (b) round cap,
    (c) source budget exhausted (run-level cap). Emits one event per round so
    the UI's activity feed sees the rhythm of the process.

    `leg_context` is the ISOLATED sub-context for this leg. It carries this
    leg's `CallContext` (used for the gap_reasoner LLM call) and is the only
    way the router sees this leg's identity. ALL intermediate state for the
    leg (`seen_passage_ids`, `seen_urls`, `current_queries`, `result.*`)
    lives in this function's locals — nothing is shared with sibling legs.
    The leg's `SubQuestionResult` is the only thing that escapes, and it
    escapes to the synthesis boundary in `DeepResearchRun.run`, never into
    a sibling leg.

    G1/DR-4 F2: ``extra_passages`` seeds this leg's working set with
    pre-attached upload passages so they are available for synthesis alongside
    web-retrieved passages. ``corpus_ids`` scopes durable Space retrieval through
    the engine's existing RetrievalRequest path. The OFF-path (empty list/default)
    is byte-identical to the pre-DR-4 code."""
    # G1/DR-4 F2: seed the leg's working set with upload passages (if any).
    # Dedup by id so a passage the retrieval engine also finds isn't doubled.
    result = SubQuestionResult(subq=subq)
    if extra_passages:
        result.passages.extend(extra_passages)
    current_queries = [subq.title]
    seen_passage_ids: set[str] = {p.id for p in extra_passages}
    seen_urls: set[str] = set()

    for round_idx in range(bound.max_rounds_per_subq):
        if remaining_source_budget <= 0:
            break
        # one search per (current_query) — limited to one query per round for
        # bounded cost on local models; multi-query expansion is the
        # retrieval engine's own concern, not ours.
        query = current_queries[0]
        result.issued_queries.append(query)
        _search_payload: dict[str, object] = {
            "subquestion": subq.title,
            "query": query,
            "round": round_idx + 1,
            "rounds_max": bound.max_rounds_per_subq,
        }
        if subq.label is not None:
            _search_payload["label"] = subq.label
        await emit("search", _search_payload)
        try:
            req = RetrievalRequest(
                query=query,
                depth="standard",  # we already do the multi-round shape
                top_k=min(bound.rerank_top_k, remaining_source_budget),
                # DR-3 E2: thread recency_window so the engine's search call
                # applies a time filter when the user selected one.
                recency_window=recency_window,
                corpus_ids=corpus_ids,
            )
            retrieval = await engine.retrieve(req)
        except Exception as exc:  # noqa: BLE001 — retrieval failure is recoverable
            await emit(
                "observation",
                {
                    "subquestion": subq.title,
                    "round": round_idx + 1,
                    "ok": False,
                    "detail": f"retrieval failed: {type(exc).__name__}: {exc}",
                },
            )
            break

        # accumulate fresh passages + hits (dedup by id / url)
        fresh: list[Passage] = []
        for p in retrieval.passages:
            if p.id in seen_passage_ids:
                continue
            seen_passage_ids.add(p.id)
            fresh.append(p)
        for h in retrieval.all_hits:
            if h.url not in seen_urls:
                seen_urls.add(h.url)
                result.all_hits.append(h)

        # upsert the fresh passages into the per-run vector store so the
        # synthesis phase can retrieve relevant corpus subsets per section.
        # Embedder is optional — if absent (hermetic tests) we still accumulate
        # in `result.passages` but the synth phase will fall back to per-subq
        # passages directly (no global retrieve).
        if embedder is not None and fresh:
            try:
                vectors = await embedder.embed([p.text for p in fresh])
                await vector_store.upsert(namespace, fresh, vectors)
            except Exception:  # noqa: BLE001 — embedding failure is non-fatal
                pass
        result.passages.extend(fresh)
        # budget bookkeeping
        added = len(fresh)
        remaining_source_budget -= added
        result.rounds_run = round_idx + 1
        await emit(
            "observation",
            {
                "subquestion": subq.title,
                "round": round_idx + 1,
                "ok": True,
                "added": added,
                "total_for_subq": len(result.passages),
                "remaining_budget": remaining_source_budget,
            },
        )
        if remaining_source_budget <= 0:
            break

        # gap reasoning between rounds (skip on the last round — no time to act)
        if round_idx + 1 >= bound.max_rounds_per_subq:
            result.bounded_by_rounds = True
            break
        # Per-leg isolation: pass this leg's CallContext so the router tracks
        # this leg's cost/model independently. _gap_reason builds its own
        # message list internally (no shared mutable list).
        sufficient, follow_ups, rationale = await _gap_reason(
            router, subq, result.passages, leg_context.call_context
        )
        await emit(
            "gap_reason",
            {
                "subquestion": subq.title,
                "sufficient": sufficient,
                "rationale": rationale,
                "follow_ups": follow_ups,
            },
        )
        if sufficient:
            break
        current_queries = follow_ups or [subq.title]

    return result
