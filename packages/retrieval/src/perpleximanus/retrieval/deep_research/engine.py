"""DeepResearchRun — the orchestrator that wires decompose → gather → synth.

This is what the agent-server's runtime calls after the user approves the plan
in the AWAITING_PLAN_APPROVAL gate. The plan IS the list of sub-questions
returned by `decompose_query`; the gate / approval / re-plan flow is owned by
the agent loop's plan-mode intercept (we reuse the Build plan mode without
modification).

The run is bounded: the first cap hit (`sources`, `rounds`, `wall_clock`,
`subquestions`) terminates with a partial-but-honest report and `bounded_by`
set on the ReportEvent. Progress events flow through the injected `emit`
callback so the agent-server appends them as ActionEvents / ObservationEvents
to the conversation log.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from perpleximanus.core import ReportEvent, ReportSection
from perpleximanus.core.llm import LLMRouter

from ..engine import RetrievalEngine
from ..models import Passage as RetrievalPassage
from ..ranking import Embedder
from ..vectorstore import VectorStore
from .decompose import SubQuestion
from .depth import DepthBound, DepthTier, bounds_for
from .gather import SubQuestionResult, gather_for_subquestion
from .synthesis import coherence_pass, synthesize_section

# Same EmitFn shape across the submodule. The agent-server installs a callback
# that takes (event_kind: str, payload: dict) and appends an ActionEvent or
# ObservationEvent to the conversation log so the UI sees progress live.
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class ReportFromRun:
    """A small assembled-result transport used between the run and the
    agent-server. The agent-server turns this into a ReportEvent it appends
    to the conversation log. Kept separate from the Core ReportEvent so the
    engine can carry retrieval-typed Passages without leaking the model into
    core's event shape."""

    def __init__(
        self,
        *,
        query: str,
        summary: str,
        sections: list[ReportSection],
        cited_passages: list[RetrievalPassage],
        all_hits: list[Any],
        unsupported_count: int,
        bounded_by: str | None,
        depth_tier: str,
    ) -> None:
        self.query = query
        self.summary = summary
        self.sections = sections
        self.cited_passages = cited_passages
        self.all_hits = all_hits
        self.unsupported_count = unsupported_count
        self.bounded_by = bounded_by
        self.depth_tier = depth_tier

    def to_event(self) -> ReportEvent:
        """Convert into the Core ReportEvent for persistence on the log.
        Retrieval-typed Passage/SearchHit are dumped to plain dicts so `core`
        stays free of any `retrieval` import."""
        return ReportEvent(
            query=self.query,
            summary=self.summary,
            sections=self.sections,
            passages=[p.model_dump() for p in self.cited_passages],
            all_hits=[h.model_dump() for h in self.all_hits],
            unsupported_count=self.unsupported_count,
            bounded_by=self.bounded_by,
            depth_tier=self.depth_tier,
        )


class DeepResearchRun:
    """One Deep Research run, end-to-end. Constructed per conversation by the
    agent-server runtime with all dependencies injected. Single-shot:
    `await run.run(plan_steps)` produces the assembled ReportFromRun.

    The plan steps come from the existing PlanEvent the agent loop emitted
    (the human has approved them — possibly with edits — by the time we run).
    We trust them as the section titles; we don't re-decompose."""

    def __init__(
        self,
        *,
        query: str,
        router: LLMRouter,
        retrieval_engine: RetrievalEngine,
        embedder: Embedder | None,
        vector_store: VectorStore,
        nli: Any,
        depth: DepthTier | str = DepthTier.STANDARD_DEEP,
        conversation_id: str = "deep_research",
    ) -> None:
        self._query = query
        self._router = router
        self._engine = retrieval_engine
        self._embedder = embedder
        self._vector_store = vector_store
        self._nli = nli
        self._bound: DepthBound = bounds_for(depth)
        self._depth = depth if isinstance(depth, str) else depth.value
        self._namespace = conversation_id

    @property
    def bound(self) -> DepthBound:
        return self._bound

    async def run(
        self,
        plan_steps: list[str],
        *,
        emit: EmitFn,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ReportFromRun:
        """Execute the run. Returns the assembled report. `emit` is awaited
        between phases so the agent-server can write events to the conversation
        log; we never write to the store directly.

        `should_cancel` makes Stop REAL: it is polled at each sub-question and each
        section boundary; when it returns true the run halts at that checkpoint and
        returns the partial report so far with `bounded_by="stopped"` (resumable).
        Without it the run is uninterruptible (the old, broken behavior)."""
        started = time.monotonic()
        bounded_by: str | None = None
        # honor the depth bound on plan width too
        if len(plan_steps) > self._bound.max_subquestions:
            plan_steps = plan_steps[: self._bound.max_subquestions]
            bounded_by = "subquestions"
        subqs = [SubQuestion(title=t) for t in plan_steps]
        await emit("phase", {"phase": "gather", "subquestions": len(subqs)})

        # ---- gather phase: retrieve-reason-refine per sub-question ----------
        results: list[SubQuestionResult] = []
        remaining = self._bound.max_sources
        for subq in subqs:
            if should_cancel is not None and should_cancel():
                bounded_by = "stopped"  # user pressed Stop — halt at this checkpoint
                break
            if (time.monotonic() - started) > self._bound.max_wall_clock_s:
                bounded_by = bounded_by or "wall_clock"
                break
            if remaining <= 0:
                bounded_by = bounded_by or "sources"
                break
            sub_result = await gather_for_subquestion(
                subq,
                engine=self._engine,
                router=self._router,
                embedder=self._embedder,
                vector_store=self._vector_store,
                namespace=self._namespace,
                bound=self._bound,
                emit=emit,
                remaining_source_budget=remaining,
            )
            results.append(sub_result)
            remaining -= len(sub_result.passages)
            if sub_result.bounded_by_rounds and bounded_by is None:
                bounded_by = "rounds"
        await emit(
            "phase",
            {
                "phase": "synthesize",
                "sections": len(results),
                "passages_total": sum(len(r.passages) for r in results),
            },
        )

        # ---- synthesize phase: map step per section ------------------------
        sections: list[ReportSection] = []
        for i, sub_result in enumerate(results):
            if should_cancel is not None and should_cancel():
                bounded_by = "stopped"  # Stop pressed during synthesis — halt here
                break
            if (time.monotonic() - started) > self._bound.max_wall_clock_s:
                bounded_by = bounded_by or "wall_clock"
                break
            section = await synthesize_section(
                sub_result,
                router=self._router,
                embedder=self._embedder,
                vector_store=self._vector_store,
                namespace=self._namespace,
                nli=self._nli,
                section_id=f"s{i}",
                top_k_for_section=self._bound.rerank_top_k,
                emit=emit,
            )
            sections.append(section)

        # ---- reduce step: coherence pass produces the executive summary ----
        await emit("phase", {"phase": "coherence"})
        summary = await coherence_pass(self._query, sections, router=self._router)

        # ---- assemble: passages cited by some section (dedup) + all_hits ---
        cited_ids: set[str] = set()
        for s in sections:
            cited_ids.update(s.cited_passage_ids)
        all_passages: list[RetrievalPassage] = []
        seen: set[str] = set()
        for r in results:
            for p in r.passages:
                if p.id in cited_ids and p.id not in seen:
                    seen.add(p.id)
                    all_passages.append(p)
        all_hits = []
        hit_urls: set[str] = set()
        for r in results:
            for h in r.all_hits:
                if h.url not in hit_urls:
                    hit_urls.add(h.url)
                    all_hits.append(h)

        unsupported_total = sum(s.unsupported_count for s in sections)
        return ReportFromRun(
            query=self._query,
            summary=summary,
            sections=sections,
            cited_passages=all_passages,
            all_hits=all_hits,
            unsupported_count=unsupported_total,
            bounded_by=bounded_by,
            depth_tier=self._depth,
        )


