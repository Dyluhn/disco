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
from typing import Any, Literal, cast

from disco.core import ReportEvent, ReportSection
from disco.core.llm import CallContext, LLMRouter

from ..engine import RetrievalEngine
from ..models import Passage as RetrievalPassage
from ..ranking import Embedder
from ..vectorstore import VectorStore
from .claims import extract_section_claims
from .decompose import SubQuestion
from .depth import DepthBound, DepthTier, bounds_for
from .gather import GatherLegContext, SubQuestionResult, gather_for_subquestion
from .iterate import run_iterative_refinement
from .judge import ClaimVerdict, _Completer, judge_claims
from .synthesis import coherence_pass, synthesize_section

# Callable types for mid-run steer / inject hooks (D3).
# Both default to None in every public-API call → the OFF-path is byte-identical
# to a run without hooks installed.
PopSteersFn = Callable[[], list[str]] | None
PopInjectedSourcesFn = Callable[[], list[RetrievalPassage]] | None

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
        upload_passages: list[Any] | None = None,
        iterative: bool = False,
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
        # G1/DR-4 F2: pre-attached upload passages to seed every gather leg.
        # None / [] → OFF path (byte-identical to pre-DR-4 code).
        self._upload_passages: list[Any] = upload_passages or []
        # A4.4: iterative-research flag. When False (the default) NOTHING new
        # runs — the judge/refine loop is never entered, so a standard run is
        # byte-identical. Gated on this flag at the single call site in `run()`.
        self._iterative = iterative
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
                    # G1/DR-4 F2: seed each leg with any pre-attached upload
                    # passages so they are available during synthesis.
                    extra_passages=list(self._upload_passages),
                )
            )
            gather_tasks.append((subq, task, subq_id, subq_namespace, leg_context))
        return gather_tasks

    def _start_one_steer_task(
        self,
        subq: SubQuestion,
        source_budget: int,
        *,
        emit: EmitFn,
        extra_passages: list[Any] | None = None,
    ) -> tuple[
        SubQuestion,
        asyncio.Task[SubQuestionResult],
        str,
        str,
        GatherLegContext,
    ]:
        """Start a single gather task for a mid-run steer sub-question.

        Uses the same leg-construction logic as `_start_gather_tasks` but
        accepts an explicit `source_budget` so the caller can give the steer
        leg a proportional fraction of the total budget (rather than the full
        `max_sources` amount). The semaphore from the parent run is NOT
        re-installed here — steer legs are created sequentially at a section
        boundary, never in a burst, so peak-memory contention is the same as
        a single normal leg.

        `extra_passages` (A4.4) seeds the leg's corpus. It defaults to
        `self._upload_passages` (the D3 steer behaviour — unchanged), but the
        iterative-refine path passes a section's ORIGINAL cited passages instead
        so a re-search leg sees the COMBINED corpus (fresh evidence + the
        section's prior sources) when it re-synthesizes."""
        if extra_passages is None:
            extra_passages = list(self._upload_passages)
        subq_hash = hashlib.sha256(subq.title.encode()).hexdigest()[:8]
        subq_namespace = f"{self._namespace}/{subq_hash}"
        subq_id = f"s{subq_hash}"
        leg_call_context = CallContext(
            conversation_id=f"{self._namespace}/{subq_id}",
        )
        leg_context = GatherLegContext(
            subq_id=subq_id,
            namespace=subq_namespace,
            call_context=leg_call_context,
        )

        async def _gather_leg(_subq: SubQuestion, **kw: Any) -> SubQuestionResult:
            return await gather_for_subquestion(_subq, **kw)

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
                remaining_source_budget=source_budget,
                leg_context=leg_context,
                recency_window=self._recency_window,
                # G1/DR-4 F2: seed steer legs with upload passages by default;
                # the A4.4 refine path overrides this with a section's originals.
                extra_passages=extra_passages,
            )
        )
        return (subq, task, subq_id, subq_namespace, leg_context)

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
        pop_steers: PopSteersFn = None,
        pop_injected_sources: PopInjectedSourcesFn = None,
    ) -> tuple[list[SubQuestionResult], str | None, list[RetrievalPassage]]:
        """RP-04 consumer: drain the gather tasks in order, synthesizing each
        section immediately (a durable checkpoint) before consuming the next.
        Honors Stop (`should_cancel`) and the wall-clock bound at every
        sub-question boundary, cancelling the still-running legs when either
        trips. Appends completed sections to `sections` in place; returns
        `(results, bounded_by, injected_passages)`.

        D3 — mid-run steer / inject hooks (both default None → OFF-path is
        byte-identical to a run without hooks):
        - `pop_steers`: drained at each boundary; each returned string becomes
          a new gather leg appended to `gather_tasks` (the index-based while
          loop naturally picks them up). Emits an observation event so the
          steer is visible in the activity feed.
        - `pop_injected_sources`: drained at each boundary; accumulated passages
          are folded into every subsequent section's candidate set (both the
          `fallback_passages` path and the vector-store retrieval path when the
          embedder is available). Returned alongside `results` so `run()` can
          add them to `carried_passages` for the final report assembly."""
        # 3. Consume results serially (Consumer).
        # LLM work (synthesis) MUST stay a single-depth queue (one at a time).
        # The MERGE point: the leg's `SubQuestionResult` (its independent
        # output) is appended to `results` here, and the synthesized
        # `ReportSection` is appended to `sections`. No other leg's state
        # touches the leg's accumulated passages / hits / queries — those
        # arrive here as immutable frozen-shape objects only.
        #
        # The loop is index-based (not a `for` over the list) so steer hooks
        # can extend `gather_tasks` in-place and the new entries are consumed
        # in the same drain pass. When both hooks are None (the OFF-path) the
        # list is never extended, so the behaviour is byte-identical to the
        # original `for` loop.
        results: list[SubQuestionResult] = []
        _injected_passages: list[RetrievalPassage] = []  # accumulated inject-source passages

        i = 0
        while i < len(gather_tasks):
            _subq, task, subq_id, subq_namespace, leg_context = gather_tasks[i]
            i += 1

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

            # D3 steer checkpoint — only active when `pop_steers` is installed.
            # For each returned steer string we spin up a fresh gather leg and
            # append it to `gather_tasks`; the while-loop drains it naturally.
            # OFF-path (pop_steers is None): this block is never entered.
            if pop_steers is not None:
                steer_budget = max(1, self._bound.max_sources // max(1, len(gather_tasks)))
                for steer_text in pop_steers():
                    new_subq = SubQuestion(title=steer_text)
                    await emit(
                        "observation",
                        {
                            "subquestion": steer_text,
                            "ok": True,
                            "detail": (
                                f"mid-run steer: adding research section '{steer_text}'"
                            ),
                        },
                    )
                    new_task_tuple = self._start_one_steer_task(
                        new_subq, steer_budget, emit=emit
                    )
                    gather_tasks.append(new_task_tuple)

            # D3 inject-source checkpoint — only active when hook is installed.
            # Newly-injected passages are accumulated so every subsequent
            # section can see them. For the embedder path they are upserted
            # into the current sub-question's namespace so `_retrieve_for_section`
            # finds them via cosine similarity; InMemoryVectorStore.upsert
            # deduplicates by passage id, so repeat upserts across sections are
            # idempotent. OFF-path: block never entered.
            if pop_injected_sources is not None:
                new_injected = pop_injected_sources()
                if new_injected:
                    _injected_passages.extend(new_injected)
                    if self._embedder is not None:
                        try:
                            vecs = await self._embedder.embed(
                                [p.text for p in new_injected]
                            )
                            await self._vector_store.upsert(
                                subq_namespace, new_injected, vecs
                            )
                        except Exception:  # noqa: BLE001
                            pass  # fallback_passages path covers it

            try:
                sub_result = await task
            except asyncio.CancelledError:
                break
            except Exception:
                for _, t, _, _, _ in gather_tasks:
                    t.cancel()
                raise

            # Fold any accumulated injected passages into this section's
            # candidate set (the fallback_passages path in _retrieve_for_section).
            # `sub_result` is a mutable dataclass — extending its `passages` list
            # is safe. This ensures the passages are visible even when the
            # embedder is unavailable (hermetic tests). OFF-path: no-op.
            if _injected_passages:
                sub_result.passages.extend(_injected_passages)

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
        return results, bounded_by, _injected_passages

    async def run(
        self,
        plan_steps: list[str],
        *,
        emit: EmitFn,
        should_cancel: Callable[[], bool] | None = None,
        resume_sections: list[ReportSection] | None = None,
        resume_passages: list[RetrievalPassage] | None = None,
        resume_all_hits: list[Any] | None = None,
        pop_steers: PopSteersFn = None,
        pop_injected_sources: PopInjectedSourcesFn = None,
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
        a resumed Deep Research run continue instead of redoing completed sections.

        D3 mid-run steer / inject:
        - `pop_steers`: callable that returns any pending steer strings. Called at
          each section boundary; each string starts a new gather leg. Default None
          → OFF-path, byte-identical to a run without hooks.
        - `pop_injected_sources`: callable that returns any pending injected
          passages (pre-converted from WS frame text). Called at each boundary;
          passages are folded into subsequent sections' candidate sets. Default
          None → OFF-path, byte-identical."""
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
        results, bounded_by, injected_passages = await self._drain_and_synthesize(
            gather_tasks,
            sections=sections,
            started=started,
            bounded_by=bounded_by,
            should_cancel=should_cancel,
            emit=emit,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )
        # D3: add any injected passages to carried_passages so they participate in
        # _assemble_report's cited-passage collection (deduped by id there).
        # OFF-path: injected_passages is always [] when pop_injected_sources is None.
        if injected_passages:
            carried_passages = carried_passages + injected_passages

        # ---- A4.4 iterative refinement (gated; OFF-path byte-identical) ----
        # When `self._iterative` is False (the default) this block is never
        # entered — the judge/refine loop never runs, nothing new is emitted,
        # and `sections` is exactly what `_drain_and_synthesize` produced.
        if self._iterative:
            sections, refine_passages, refine_hits = await self._iterative_refine(
                sections, results, carried_passages, emit
            )
            # Fold the refine legs' fresh passages into the report's passage set so
            # a section's fresh `[[id]]` citations resolve to source cards in
            # `_assemble_report` (deduped by id there). OFF-path: never reached.
            if refine_passages:
                carried_passages = carried_passages + refine_passages
            # Fold the refine legs' discovered urls into carried_hits so the report's
            # all-searched audit trail stays the COMPLETE discovery set (deduped in
            # _assemble_report).
            if refine_hits:
                carried_hits = carried_hits + refine_hits

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

    async def _iterative_refine(
        self,
        sections: list[ReportSection],
        results: list[SubQuestionResult],
        carried_passages: list[RetrievalPassage],
        emit: EmitFn,
    ) -> tuple[list[ReportSection], list[RetrievalPassage], list[Any]]:
        """A4.4 — the iterative-research loop, wired to the real engine.

        Consumes the A4.0/A4.1/A4.2 core: judge every section's claims, re-search
        the weak ones (seeded with that section's ORIGINAL passages so re-synthesis
        sees the COMBINED corpus), re-synthesize, and re-judge — up to the loop's
        round cap, stopping once enough claims are SUPPORTED. Only reached when
        `self._iterative` is True; the OFF path never calls this.

        Returns `(refined_sections, fresh_passages)`. `fresh_passages` are the
        passages the refine legs newly gathered; `run()` folds them into the
        report's passage set so a section's fresh `[[id]]` citations resolve to
        source cards (without them, `_assemble_report` could not see them)."""
        # Index every passage we have by id (across all sub-results + carried).
        # `passages_by_id_obj` keeps the Passage objects (to re-seed a re-search
        # leg's corpus); `passages_text` is the {id: text} the judge/extractor need.
        passages_by_id_obj: dict[str, RetrievalPassage] = {}
        for r in results:
            for p in r.passages:
                passages_by_id_obj.setdefault(p.id, p)
        for p in carried_passages:
            passages_by_id_obj.setdefault(p.id, p)
        passages_text = {pid: p.text for pid, p in passages_by_id_obj.items()}
        # Passages the refine legs newly gather — returned so they reach the final
        # report (so fresh `[[id]]` citations resolve to source cards). Keyed by id
        # to dedup across rounds; insertion order preserved for stable assembly.
        fresh_passages: dict[str, RetrievalPassage] = {}
        # Refine legs also DISCOVER urls; collect their all_hits so the report's
        # "All-Searched" audit trail stays the COMPLETE discovery set (else iterative
        # runs would silently omit the refine legs' searches).
        fresh_hits: list[Any] = []

        async def judge_section(sec: ReportSection) -> list[ClaimVerdict]:
            claims = extract_section_claims(sec.markdown, passages_text)
            # The judge's `_Completer` protocol requires the `context=` kwarg the
            # public `LLMRouter` Protocol omits but the concrete DefaultLLMRouter
            # accepts (same omission gather.py/synthesis.py cast around). The cast
            # is a typing-only narrowing — identity at runtime.
            return await judge_claims(claims, router=cast(_Completer, self._router))

        async def refine_section(
            sec: ReportSection, weak: list[ClaimVerdict]
        ) -> ReportSection:
            # Seed the re-search leg with the section's ORIGINAL cited passages so
            # re-synthesis sees the COMBINED corpus (fresh evidence + originals) —
            # this is how the combined-corpus requirement is met without any
            # per-section context retention in the loop.
            orig = [
                passages_by_id_obj[i]
                for i in sec.cited_passage_ids
                if i in passages_by_id_obj
            ]
            # Build a targeted sub-question: the section topic + its weak claims.
            weak_titles = " | ".join(v.claim for v in weak[:3])
            title = f"{sec.title}: verify — {weak_titles}" if weak_titles else sec.title
            new_subq = SubQuestion(title=title)
            # One fresh gather leg, seeded with `orig` (NOT the upload passages).
            steer_budget = max(1, self._bound.max_sources // max(1, len(sections)))
            _subq, task, _sid, namespace, leg_context = self._start_one_steer_task(
                new_subq, steer_budget, emit=emit, extra_passages=orig
            )
            # Defect-1 fix (combined corpus under an embedder): the gather leg only
            # upserts its FRESH passages into `namespace`, and `_retrieve_for_section`
            # queries that namespace when an embedder is present — so the seeded
            # `orig` (which rides only in `SubQuestionResult.passages`, the
            # embedder-absent fallback path) would be DROPPED from re-synthesis.
            # Upsert `orig` into the same namespace here so the namespace query
            # returns orig+fresh — the true COMBINED corpus. No-op when there is no
            # embedder (synthesis then uses the fallback passages, which include
            # `orig` already), and it touches only the refine leg's namespace.
            if self._embedder is not None and orig:
                try:
                    _orig_vecs = await self._embedder.embed([p.text for p in orig])
                    await self._vector_store.upsert(namespace, orig, _orig_vecs)
                except Exception:  # noqa: BLE001 — fallback path still carries orig
                    pass
            try:
                new_result = await task
            except Exception:  # noqa: BLE001 — a failed re-search must not regress
                return sec
            try:
                new_section = await synthesize_section(
                    new_result,
                    router=self._router,
                    embedder=self._embedder,
                    vector_store=self._vector_store,
                    namespace=namespace,
                    nli=self._nli,
                    section_id=sec.id,
                    top_k_for_section=self._bound.rerank_top_k,
                    emit=emit,
                    leg_context=leg_context,
                    recency_window=self._recency_window,
                )
            except Exception:  # noqa: BLE001 — never let a synth failure regress
                return sec
            # Keep the section's heading stable (the leg's title was a probe).
            new_section = new_section.model_copy(update={"title": sec.title})
            # If the re-search produced an empty/degraded section, keep the
            # original so the loop's no-improvement break fires (never regress).
            if not new_section.cited_passage_ids or "[[" not in new_section.markdown:
                return sec
            # Defect-2 fix (fresh evidence must not be discarded): merge the refine
            # leg's passages into the shared index so (i) the NEXT judge round can
            # see fresh-cited claims (extract_section_claims skips ids with no text,
            # so without this a fresh citation vanishes and the section scores as
            # vacuously "converged"), and (ii) `fresh_passages` carries them out to
            # the final report so `_assemble_report` resolves the new `[[id]]`
            # citations to source cards.
            for p in new_result.passages:
                if p.id not in passages_by_id_obj:
                    passages_by_id_obj[p.id] = p
                passages_text.setdefault(p.id, p.text)
                fresh_passages.setdefault(p.id, p)
            # The refine leg's discovered urls join the report's all-searched set.
            fresh_hits.extend(getattr(new_result, "all_hits", None) or [])
            return new_section

        await emit("phase", {"phase": "iterate"})
        res = await run_iterative_refinement(
            sections,
            judge_section=judge_section,
            refine_section=refine_section,
            emit=emit,
        )
        await emit(
            "phase",
            {
                "phase": "iterate",
                "rounds": res.rounds,
                "supported": res.final_supported,
                "converged": res.converged,
            },
        )
        return res.sections, list(fresh_passages.values()), fresh_hits


