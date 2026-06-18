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

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from disco.core import ReportEvent, ReportSection
from disco.core.llm import CallContext, LLMRouter

from ..engine import RetrievalEngine
from ..models import Passage as RetrievalPassage
from ..ranking import Embedder
from ..vectorstore import VectorStore
from .decompose import SubQuestion
from .depth import DepthBound, DepthTier, bounds_for
from .gather import GatherLegContext, SubQuestionResult, gather_for_subquestion
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
        gather_concurrency: int | None = None,
        recency_window: Literal["month", "week"] | None = None,
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
        # DR-3 E2: recency window for time-filtered search + date prompt injection.
        self._recency_window = recency_window
        # RAM-aware peak-memory guard (OOM fix) — applies on EVERY tier, because
        # not OOMing is a correctness guarantee, not a free-tier compensation.
        # Each concurrent gather leg holds its own fetch buffers + extracted
        # markdown + per-leg passages (+ embed/rerank batches on the bundled
        # in-process tier), so an exhaustive run's 12 simultaneous legs multiply
        # peak RSS ~12× and can OOM. Bounding legs to K-at-a-time caps that to
        # ~K× while keeping most of the latency win. The runtime derives K from
        # available RAM (a big box → K high enough to run every leg; a small box
        # → protected) and passes it on every tier. `None` (the default, used by
        # hermetic tests) = UNBOUNDED.
        self._gather_concurrency = (
            gather_concurrency if (gather_concurrency or 0) > 0 else None
        )

    @property
    def bound(self) -> DepthBound:
        return self._bound

    async def _prepare_plan(
        self,
        plan_steps: list[str],
        *,
        resume_sections: list[ReportSection] | None,
        resume_passages: list[RetrievalPassage] | None,
        resume_all_hits: list[Any] | None,
        emit: EmitFn,
    ) -> tuple[
        str | None,
        list[SubQuestion],
        list[ReportSection],
        list[RetrievalPassage],
        list[Any],
    ]:
        """Bound the plan width to the depth tier, carry forward already-completed
        (resumed) sections, and emit the gather-phase + low-RAM transparency
        events. Returns `(bounded_by, pending, sections, carried_passages,
        carried_hits)` — the explicit state the rest of the run threads through."""
        bounded_by: str | None = None
        # honor the depth bound on plan width too
        if len(plan_steps) > self._bound.max_subquestions:
            plan_steps = plan_steps[: self._bound.max_subquestions]
            bounded_by = "subquestions"

        # Carry forward already-completed sections (resume). Match by title — the
        # plan step titles ARE the section titles, so a section present means that
        # sub-question is done and must not be re-gathered.
        sections: list[ReportSection] = list(resume_sections or [])
        done_titles = {s.title for s in sections}
        carried_passages: list[RetrievalPassage] = list(resume_passages or [])
        carried_hits: list[Any] = list(resume_all_hits or [])
        pending = [SubQuestion(title=t) for t in plan_steps if t not in done_titles]
        # Graceful low-RAM transparency (#26): the gather concurrency was derived
        # from available RAM. Surface it — and whether it's actually *constraining*
        # parallelism (cap < legs) — so a memory-reduced run is VISIBLE, never a
        # silent degradation. `concurrency=None` means unbounded (big box / remote).
        memory_bounded = (
            self._gather_concurrency is not None
            and self._gather_concurrency < len(pending)
        )
        await emit(
            "phase",
            {
                "phase": "gather",
                "subquestions": len(pending),
                "resumed_sections": len(sections),
                "concurrency": self._gather_concurrency,
                "memory_bounded": memory_bounded,
            },
        )
        if memory_bounded:
            await emit(
                "observation",
                {
                    "subquestion": None,
                    "ok": True,
                    "detail": (
                        f"running {self._gather_concurrency} of {len(pending)} "
                        f"research legs at a time to stay within the available "
                        f"memory budget (reduced parallelism, not reduced coverage)"
                    ),
                },
            )
        return bounded_by, pending, sections, carried_passages, carried_hits

    def _start_gather_tasks(
        self,
        pending: list[SubQuestion],
        *,
        emit: EmitFn,
    ) -> list[
        tuple[
            SubQuestion,
            asyncio.Task[SubQuestionResult],
            str,
            str,
            GatherLegContext,
        ]
    ]:
        """RP-04 producer: partition the source budget across the pending
        sub-questions and start every gather leg concurrently. When
        `self._gather_concurrency` is set (bundled in-process-encoder tier) a
        Semaphore caps how many legs run the heavy body at once — peak-memory
        guard; `None` ⇒ fully concurrent. All tasks are created up-front so the
        consumer can drain them in order. Returns the ordered task list, each
        entry `(subq, task, subq_id, subq_namespace, leg_context)`."""
        # The source budget governs the NEW gathering this invocation does; carried
        # passages were already budgeted in the prior run, so resume gets a fresh
        # allowance to make progress on its remaining sub-questions.
        remaining = self._bound.max_sources

        # RP-04: Pipeline restructure.
        # 1. Partition budget upfront (Race Fix).
        n_pending = len(pending)
        budget_per = remaining // n_pending if n_pending > 0 else 0
        extra_budget = remaining % n_pending if n_pending > 0 else 0

        # 2. Start all GATHER tasks concurrently (Producer).
        # Retrieval work (search, fetch, extract, embed, rerank) runs concurrently.
        #
        # BUNDLED-tier peak-memory guard: when `self._gather_concurrency` is set
        # (in-process-encoder tier only), a Semaphore caps how many legs run the
        # heavy body AT ONCE. All tasks are still created up-front so the
        # consumer below can drain them in order; the ones past the cap simply
        # block on `sem.acquire()` until a slot frees — bounding peak working
        # set to ~K legs instead of all `n_pending`. `None` ⇒ no semaphore ⇒
        # fully concurrent (paid / self-host / remote-encoder tier — unchanged).
        leg_sem = (
            asyncio.Semaphore(self._gather_concurrency)
            if self._gather_concurrency is not None
            else None
        )

        async def _gather_leg(_subq: SubQuestion, **kw: Any) -> SubQuestionResult:
            if leg_sem is not None:
                async with leg_sem:
                    return await gather_for_subquestion(_subq, **kw)
            return await gather_for_subquestion(_subq, **kw)

        gather_tasks: list[
            tuple[
                SubQuestion,
                asyncio.Task[SubQuestionResult],
                str,
                str,
                GatherLegContext,
            ]
        ] = []
        for i, subq in enumerate(pending):
            subq_budget = budget_per + (1 if i < extra_budget else 0)

            # Race Fix: section_id and namespace collision defense. Stability for resume.
            subq_hash = hashlib.sha256(subq.title.encode()).hexdigest()[:8]
            subq_namespace = f"{self._namespace}/{subq_hash}"
            subq_id = f"s{subq_hash}"

            # Per-leg ISOLATED sub-context (C14). Each leg gets its OWN:
            #   - subq_id     — leg's identifier (matches the synthesized section)
            #   - namespace   — leg's vector-store namespace (already per-leg)
            #   - call_context — per-leg CallContext for the router, with a
            #                   unique conversation_id so cost tracking /
            #                   model_override / hard-budget enforcement are
            #                   scoped to THIS leg. No sibling leg can
            #                   exhaust the budget out from under another.
            # The leg's message list is built independently per LLM call
            # (no shared mutable list); the leg's intermediate state lives
            # in `gather_for_subquestion` locals. The only thing that
            # escapes the leg is its `SubQuestionResult`, and it escapes
            # ONLY to the synthesis boundary below — never into a sibling
            # leg's context. See `GatherLegContext` docstring for the
            # full contract.
            leg_call_context = CallContext(
                conversation_id=f"{self._namespace}/{subq_id}",
            )
            leg_context = GatherLegContext(
                subq_id=subq_id,
                namespace=subq_namespace,
                call_context=leg_call_context,
            )

            task = asyncio.create_task(
                _gather_leg(
                    subq,
                    engine=self._engine,
                    router=self._router,
                    embedder=self._embedder,
                    vector_store=self._vector_store,
                    namespace=subq_namespace,
                    bound=self._bound,
                    emit=emit,
                    remaining_source_budget=subq_budget,
                    leg_context=leg_context,
                    recency_window=self._recency_window,
                )
            )
            gather_tasks.append((subq, task, subq_id, subq_namespace, leg_context))
        return gather_tasks

    async def _drain_and_synthesize(
        self,
        gather_tasks: list[
            tuple[
                SubQuestion,
                asyncio.Task[SubQuestionResult],
                str,
                str,
                GatherLegContext,
            ]
        ],
        *,
        sections: list[ReportSection],
        started: float,
        bounded_by: str | None,
        should_cancel: Callable[[], bool] | None,
        emit: EmitFn,
    ) -> tuple[list[SubQuestionResult], str | None]:
        """RP-04 consumer: drain the gather tasks in order, synthesizing each
        section immediately (a durable checkpoint) before consuming the next.
        Honors Stop (`should_cancel`) and the wall-clock bound at every
        sub-question boundary, cancelling the still-running legs when either
        trips. Appends completed sections to `sections` in place; returns
        `(results, bounded_by)`."""
        # 3. Consume results serially (Consumer).
        # LLM work (synthesis) MUST stay a single-depth queue (one at a time).
        # The MERGE point: the leg's `SubQuestionResult` (its independent
        # output) is appended to `results` here, and the synthesized
        # `ReportSection` is appended to `sections`. No other leg's state
        # touches the leg's accumulated passages / hits / queries — those
        # arrive here as immutable frozen-shape objects only.
        results: list[SubQuestionResult] = []
        for _subq, task, subq_id, subq_namespace, leg_context in gather_tasks:
            if should_cancel is not None and should_cancel():
                bounded_by = "stopped"
                for _, t, _, _, _ in gather_tasks:
                    t.cancel()
                break
            if (time.monotonic() - started) > self._bound.max_wall_clock_s:
                bounded_by = bounded_by or "wall_clock"
                still_running = [t for _, t, _, _, _ in gather_tasks if not t.done()]
                if still_running:
                    for t in still_running:
                        t.cancel()
                    break

            try:
                sub_result = await task
            except asyncio.CancelledError:
                break
            except Exception:
                for _, t, _, _, _ in gather_tasks:
                    t.cancel()
                raise

            results.append(sub_result)
            if sub_result.bounded_by_rounds and bounded_by is None:
                bounded_by = "rounds"

            # Synthesize THIS section immediately so it survives a later Stop.
            # Thread the leg's per-leg CallContext into the synthesis call so
            # the leg's identity is preserved through gather→synth.
            await emit(
                "phase",
                {"phase": "synthesize", "section": len(sections) + 1},
            )
            try:
                section = await synthesize_section(
                    sub_result,
                    router=self._router,
                    embedder=self._embedder,
                    vector_store=self._vector_store,
                    namespace=subq_namespace,
                    nli=self._nli,
                    section_id=subq_id,
                    top_k_for_section=self._bound.rerank_top_k,
                    emit=emit,
                    leg_context=leg_context,
                    recency_window=self._recency_window,
                )
            except Exception:
                # A synthesis failure must not leak the still-running retrieval
                # tasks into the long-lived server loop.
                for _, t, _, _, _ in gather_tasks:
                    t.cancel()
                raise
            sections.append(section)
            # A checkpoint signal the agent-server persists as incremental progress.
            await emit(
                "section_done",
                {"section_id": section.id, "title": section.title, "done": len(sections)},
            )
        return results, bounded_by

    async def run(
        self,
        plan_steps: list[str],
        *,
        emit: EmitFn,
        should_cancel: Callable[[], bool] | None = None,
        resume_sections: list[ReportSection] | None = None,
        resume_passages: list[RetrievalPassage] | None = None,
        resume_all_hits: list[Any] | None = None,
    ) -> ReportFromRun:
        """Execute the run. Returns the assembled report. `emit` is awaited
        between phases so the agent-server can write events to the conversation
        log; we never write to the store directly.

        Each sub-question is gathered AND synthesized before moving to the next —
        so a completed section is a durable checkpoint. `should_cancel` makes Stop
        REAL: it is polled at each sub-question boundary; when it returns true the
        run halts there and returns the partial report (every section completed so
        far) with `bounded_by="stopped"`. Without it the run is uninterruptible.

        Resume (checkpointed): pass `resume_sections` (+ their `resume_passages` /
        `resume_all_hits`) from a prior stopped run's partial ReportEvent. Their
        sub-questions are skipped — we only gather+synthesize the steps NOT already
        done, then re-run the coherence pass over the full set. This is what makes
        a resumed Deep Research run continue instead of redoing completed sections."""
        started = time.monotonic()
        (
            bounded_by,
            pending,
            sections,
            carried_passages,
            carried_hits,
        ) = await self._prepare_plan(
            plan_steps,
            resume_sections=resume_sections,
            resume_passages=resume_passages,
            resume_all_hits=resume_all_hits,
            emit=emit,
        )

        # ---- per-sub-question: gather → synthesize (a durable checkpoint) ----
        gather_tasks = self._start_gather_tasks(pending, emit=emit)
        results, bounded_by = await self._drain_and_synthesize(
            gather_tasks,
            sections=sections,
            started=started,
            bounded_by=bounded_by,
            should_cancel=should_cancel,
            emit=emit,
        )

        # ---- reduce step: coherence pass produces the executive summary ----
        await emit("phase", {"phase": "coherence"})
        summary = await coherence_pass(
            self._query, sections, router=self._router,
            recency_window=self._recency_window,
        )

        return self._assemble_report(
            sections=sections,
            results=results,
            carried_passages=carried_passages,
            carried_hits=carried_hits,
            summary=summary,
            bounded_by=bounded_by,
        )

    def _assemble_report(
        self,
        *,
        sections: list[ReportSection],
        results: list[SubQuestionResult],
        carried_passages: list[RetrievalPassage],
        carried_hits: list[Any],
        summary: str,
        bounded_by: str | None,
    ) -> ReportFromRun:
        """Assemble the final report: collect the passages actually cited by some
        section (deduped) and the deduped all_hits, then build the ReportFromRun.
        Carried (resumed) passages/hits come first so a resumed run's citations
        resolve against the sources its earlier sections actually used."""
        cited_ids: set[str] = set()
        for s in sections:
            cited_ids.update(s.cited_passage_ids)
        all_passages: list[RetrievalPassage] = []
        seen: set[str] = set()
        for p in carried_passages:
            if p.id in cited_ids and p.id not in seen:
                seen.add(p.id)
                all_passages.append(p)
        for r in results:
            for p in r.passages:
                if p.id in cited_ids and p.id not in seen:
                    seen.add(p.id)
                    all_passages.append(p)
        all_hits = []
        hit_urls: set[str] = set()
        for h in carried_hits:
            if h.url not in hit_urls:
                hit_urls.add(h.url)
                all_hits.append(h)
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


