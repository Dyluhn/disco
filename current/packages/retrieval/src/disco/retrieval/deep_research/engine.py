"""DeepResearchRun — the orchestrator that wires decompose → gather → synth.

This is what the agent-server's runtime calls after the user approves the plan
in the AWAITING_PLAN_APPROVAL gate. The plan IS the list of sub-questions
returned by `decompose_query`; the gate / approval / re-plan flow is owned by
the agent loop's plan-mode intercept (we reuse the Build plan mode without
modification).

The run is bounded: any cap hit (`sources`, `rounds`, `wall_clock`,
`subquestions`) terminates with a partial-but-honest report and the strongest
terminal reason is set as `bounded_by` on the ReportEvent. Progress events flow
through the injected `emit` callback so the agent-server appends them as
ActionEvents / ObservationEvents to the conversation log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from disco.core import ReportEvent, ReportSection
from disco.core.llm import LLMRouter

from ..engine import RetrievalEngine
from ..models import Passage as RetrievalPassage
from ..ranking import Embedder
from ..vectorstore import VectorStore
from ._budget import SourceBudget
from ._engine_parts._drain import _dominant_bound, drain_and_synthesize
from ._engine_parts._plan import prepare_plan, start_gather_tasks, start_one_steer_task
from ._engine_parts._refine import iterative_refine
from ._engine_parts._report import collect_report_data
from .decompose import SubQuestion
from .depth import DepthBound, DepthTier, bounds_for
from .gather import GatherLegContext, SubQuestionResult, gather_for_subquestion
from .synthesis import coherence_pass, synthesize_section

# `gather_for_subquestion` and `synthesize_section` are not called directly in
# this module anymore (that authority moved to `_engine_parts`), but they stay
# bound here as real module-level names — genuinely referenced below, not a
# lint suppression — because `current/packages/retrieval/tests/
# test_pipeline_invariants.py` and `current/packages/agent-server/tests/
# test_upload_corpus.py` monkeypatch them at
# `disco.retrieval.deep_research.engine.{gather_for_subquestion,
# synthesize_section}`. The `_engine_parts` call sites resolve these through
# `from .. import engine` then `engine.gather_for_subquestion(...)` /
# `engine.synthesize_section(...)` (module-attribute lookup at call time,
# never a captured `from .x import name` binding) specifically so that patch
# is observed — a direct import in the parts module would silently defeat it.
_MONKEYPATCH_TARGETS = (gather_for_subquestion, synthesize_section)

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
        reviewed_passages: list[RetrievalPassage],
        all_hits: list[Any],
        unsupported_count: int,
        bounded_by: str | None,
        depth_tier: str,
    ) -> None:
        self.query = query
        self.summary = summary
        self.sections = sections
        self.cited_passages = cited_passages
        self.reviewed_passages = reviewed_passages
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
            reviewed_passages=[p.model_dump() for p in self.reviewed_passages],
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
        corpus_ids: frozenset[str] = frozenset(),
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
        # Spaces: durable named corpora to include in every RetrievalRequest.
        self._corpus_ids = corpus_ids
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
        self._gather_concurrency = gather_concurrency if (gather_concurrency or 0) > 0 else None
        self._source_budget = SourceBudget(self._bound.max_sources)
        self._scheduled_subquestions = 0

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
        carried_hits)` — the explicit state the rest of the run threads through.
        See `_engine_parts._plan.prepare_plan` for the implementation."""
        return await prepare_plan(
            self,
            plan_steps,
            resume_sections=resume_sections,
            resume_passages=resume_passages,
            resume_all_hits=resume_all_hits,
            emit=emit,
        )

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
        sub-questions and start every gather leg concurrently. Returns the
        ordered task list, each entry `(subq, task, subq_id, subq_namespace,
        leg_context)`. See `_engine_parts._plan.start_gather_tasks` for the
        implementation."""
        return start_gather_tasks(self, pending, emit=emit)

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
        """Start a single gather task for a mid-run steer sub-question. See
        `_engine_parts._plan.start_one_steer_task` for the implementation
        (same leg-construction logic as `_start_gather_tasks`, but with an
        explicit `source_budget` and no concurrency semaphore)."""
        return start_one_steer_task(
            self, subq, source_budget, emit=emit, extra_passages=extra_passages
        )

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
        byte-identical to a run without hooks): see
        `_engine_parts._drain.drain_and_synthesize` for the full contract —
        this method is a thin delegator to it (stop-condition check, D3
        steer/inject checkpoints, leg-result await, and per-leg synthesis are
        each a single-purpose helper there, not folded into one loop body)."""
        return await drain_and_synthesize(
            self,
            gather_tasks,
            sections=sections,
            started=started,
            bounded_by=bounded_by,
            should_cancel=should_cancel,
            emit=emit,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )

    def _seconds_left(self, started: float) -> float:
        return max(0.0, self._bound.max_wall_clock_s - (time.monotonic() - started))

    async def _refine_before_deadline(
        self,
        sections: list[ReportSection],
        results: list[SubQuestionResult],
        carried_passages: list[RetrievalPassage],
        emit: EmitFn,
        started: float,
    ) -> tuple[list[ReportSection], list[RetrievalPassage], list[Any], bool]:
        remaining = self._seconds_left(started)
        if remaining <= 0:
            return sections, [], [], True
        try:
            refined, passages, hits = await asyncio.wait_for(
                self._iterative_refine(sections, results, carried_passages, emit),
                timeout=remaining,
            )
        except TimeoutError:
            return sections, [], [], True
        return refined, passages, hits, False

    async def _coherence_before_deadline(
        self,
        sections: list[ReportSection],
        passages: list[RetrievalPassage],
        started: float,
    ) -> tuple[str, bool]:
        remaining = self._seconds_left(started)
        if remaining <= 0:
            return self._wall_clock_summary(), True
        try:
            summary = await asyncio.wait_for(
                coherence_pass(
                    self._query,
                    sections,
                    router=self._router,
                    passages=passages,
                    nli=self._nli,
                    recency_window=self._recency_window,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            return self._wall_clock_summary(), True
        return summary, False

    @staticmethod
    def _wall_clock_summary() -> str:
        return (
            "Research reached its time limit. The completed sections below contain "
            "the source-grounded findings available before the deadline."
        )

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
        # DeepResearchRun is documented as single-shot, but resetting here makes
        # the execution-scoped cap explicit and keeps defensive test reuse honest.
        self._source_budget = SourceBudget(self._bound.max_sources)
        self._scheduled_subquestions = 0
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
            (
                sections,
                refine_passages,
                refine_hits,
                refine_timed_out,
            ) = await self._refine_before_deadline(
                sections, results, carried_passages, emit, started
            )
            if refine_timed_out:
                bounded_by = _dominant_bound(bounded_by, "wall_clock")
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
            if self._source_budget.remaining <= 0:
                bounded_by = _dominant_bound(bounded_by, "sources")

        # ---- reduce step: coherence pass produces the executive summary ----
        await emit("phase", {"phase": "coherence"})
        summary, coherence_timed_out = await self._coherence_before_deadline(
            sections,
            [
                *carried_passages,
                *(passage for result in results for passage in result.passages),
            ],
            started,
        )
        if coherence_timed_out:
            bounded_by = _dominant_bound(bounded_by, "wall_clock")

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
        section (deduped) and the deduped all_hits (via `collect_report_data`),
        then build the ReportFromRun. Carried (resumed) passages/hits come first
        so a resumed run's citations resolve against the sources its earlier
        sections actually used."""
        cited_passages, reviewed_passages, all_hits, unsupported_total = collect_report_data(
            sections, results, carried_passages, carried_hits
        )
        return ReportFromRun(
            query=self._query,
            summary=summary,
            sections=sections,
            cited_passages=cited_passages,
            reviewed_passages=reviewed_passages,
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
        """A4.4 — the iterative-research loop, wired to the real engine. Only
        reached when `self._iterative` is True; the OFF path never calls this.
        See `_engine_parts._refine.iterative_refine` for the full
        implementation (judge every section's claims, re-search the weak
        ones seeded with that section's ORIGINAL passages so re-synthesis
        sees the COMBINED corpus, re-synthesize, and re-judge — up to the
        loop's round cap)."""
        return await iterative_refine(self, sections, results, carried_passages, emit)
