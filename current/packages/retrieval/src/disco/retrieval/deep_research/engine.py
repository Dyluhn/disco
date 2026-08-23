"""DeepResearchRun — the orchestrator that wires decompose → gather → report.

This is what the agent-server's runtime calls after the user approves the plan
in the AWAITING_PLAN_APPROVAL gate. The plan IS the list of starting research
directions returned by `decompose_query`; the gate / approval / re-plan flow
is owned by the agent loop's plan-mode intercept.

The run has exactly three outcomes: a report produced by the one pipeline
(adaptive gathering → evidence-led outline → per-section synthesis with claim
verification → executive summary), an exception (surfaced by the agent-server
as an ErrorEvent + ERROR status), or a Stop checkpoint (`bounded_by=
"stopped"`, no report prose) the user can resume. Research caps (`sources`,
`rounds`, `wall_clock`, `subquestions`) bound GATHERING and are surfaced
honestly via `bounded_by`; the writing phase is bounded by the outline's
section count and each call's own bounded retries, never a shrinking global
deadline. Progress events flow through the injected `emit` callback so the
agent-server appends them as ActionEvents / ObservationEvents.
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
from ._engine_parts._adaptive import gather_adaptive_batches
from ._engine_parts._compiler import compile_report_sections
from ._engine_parts._drain import drain_and_synthesize, seconds_left
from ._engine_parts._finish import finish_report
from ._engine_parts._plan import prepare_plan, start_gather_tasks, start_one_steer_task
from ._engine_parts._report import collect_report_data
from .controller import ResearchController
from .decompose import SubQuestion
from .depth import DepthBound, DepthTier, bounds_for
from .gather import GatherLegContext, SubQuestionResult, gather_for_subquestion
from .report_compiler import collect_claim_ledger, collect_global_evidence, compile_report_plan
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
_MONKEYPATCH_TARGETS = (
    gather_for_subquestion,
    synthesize_section,
    coherence_pass,
    compile_report_plan,
)

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
        claims: list[dict[str, Any]] | None = None,
        completed_probes: list[str] | None = None,
        pending_probes: list[str] | None = None,
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
        self.claims = list(claims or [])
        self.completed_probes = list(completed_probes or [])
        self.pending_probes = list(pending_probes or [])
        # Synthesis owns how much of this envelope the evidence can support;
        # retrieval never borrows it.  The concrete tier values are available
        # from ``DeepResearchRun.bound.report_spec`` and this transport carries
        # the same shape for downstream report assembly.
        self.report_spec: dict[str, int] | None = None

    def to_event(self) -> ReportEvent:
        """Convert into the Core ReportEvent for persistence on the log.
        Retrieval-typed Passage/SearchHit are dumped to plain dicts so `core`
        stays free of any `retrieval` import."""
        from ._report_event import fit_report_event

        event = ReportEvent(
            query=self.query,
            summary=self.summary,
            sections=self.sections,
            passages=[p.model_dump() for p in self.cited_passages],
            reviewed_passages=[p.model_dump() for p in self.reviewed_passages],
            all_hits=[h.model_dump() for h in self.all_hits],
            claims=self.claims,
            unsupported_count=self.unsupported_count,
            bounded_by=self.bounded_by,
            depth_tier=self.depth_tier,
            completed_probes=self.completed_probes,
            pending_probes=self.pending_probes,
        )
        return fit_report_event(event)


class DeepResearchRun:
    """One Deep Research run, end-to-end. Constructed per conversation by the
    agent-server runtime with all dependencies injected. Single-shot:
    `await run.run(plan_steps)` produces the assembled ReportFromRun.

    The plan steps come from the approved PlanEvent, but they are starting
    research directions rather than a table of contents. The final headings
    are compiled only after the evidence pool is assembled."""

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
        self._controller: ResearchController | None = None

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
        resume_completed_probes: list[str] | None,
        resume_pending_probes: list[str] | None,
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
            resume_completed_probes=resume_completed_probes,
            resume_pending_probes=resume_pending_probes,
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
        started: float,
        bounded_by: str | None,
        should_cancel: Callable[[], bool] | None,
        emit: EmitFn,
        pop_steers: PopSteersFn = None,
        pop_injected_sources: PopInjectedSourcesFn = None,
    ) -> tuple[list[SubQuestionResult], str | None, list[RetrievalPassage]]:
        """RP-04 consumer: drain the gather tasks in order, honoring Stop
        (`should_cancel`) and the research wall-clock bound at every
        sub-question boundary. Returns `(results, bounded_by,
        injected_passages)` — see `_engine_parts._drain.drain_and_synthesize`
        for the full contract; this method is a thin delegator to it."""
        return await drain_and_synthesize(
            self,
            gather_tasks,
            started=started,
            bounded_by=bounded_by,
            should_cancel=should_cancel,
            emit=emit,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )

    def _seconds_left(self, started: float) -> float:
        return seconds_left(self, started)

    async def _compile_report_sections(
        self,
        results: list[SubQuestionResult],
        carried_passages: list[RetrievalPassage],
        *,
        emit: EmitFn,
    ) -> list[ReportSection]:
        return await compile_report_sections(
            self,
            results,
            carried_passages,
            emit=emit,
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
        resume_completed_probes: list[str] | None = None,
        resume_pending_probes: list[str] | None = None,
        pop_steers: PopSteersFn = None,
        pop_injected_sources: PopInjectedSourcesFn = None,
    ) -> ReportFromRun:
        """Execute the run. Returns the assembled report. `emit` is awaited
        between phases so the agent-server can write events to the conversation
        log; we never write to the store directly.

        Research gathers evidence in adaptive batches; report writing starts
        only after gathering completes. `should_cancel` makes Stop REAL: it is
        polled at each sub-question boundary during gathering; when it returns
        true the run halts there and returns a CHECKPOINT (`bounded_by=
        "stopped"`, no report prose) the agent-server persists as a resumable
        PAUSED state. Once writing has started the run completes normally.

        Resume (checkpointed): pass the prior stopped run's evidence
        (`resume_passages` / `resume_all_hits`) and probe state. Completed
        probes are skipped; the final report is recompiled globally from the
        combined evidence pool.

        D3 mid-run steer / inject:
        - `pop_steers`: callable that returns any pending steer strings. Called
          at each probe boundary; each string becomes a research probe. Default
          None → OFF-path, byte-identical to a run without hooks.
        - `pop_injected_sources`: callable that returns any pending injected
          passages (pre-converted from WS frame text). Called at each boundary;
          passages are folded into the evidence pool. Default None → OFF-path,
          byte-identical."""
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
            resume_completed_probes=resume_completed_probes,
            resume_pending_probes=resume_pending_probes,
            emit=emit,
        )

        results, bounded_by, carried_passages = await gather_adaptive_batches(
            self,
            pending,
            sections,
            carried_passages,
            started=started,
            bounded_by=bounded_by,
            should_cancel=should_cancel,
            emit=emit,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
        )

        if bounded_by == "stopped":
            # A Stop checkpoint, not a report: keep the gathered evidence and
            # any sections carried in from a prior checkpoint, write no prose.
            return self._assemble_report(
                sections=sections,
                results=results,
                carried_passages=carried_passages,
                carried_hits=carried_hits,
                summary="",
                bounded_by="stopped",
            )

        return await finish_report(
            self,
            results,
            carried_passages,
            carried_hits,
            bounded_by,
            emit=emit,
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
        evidence = collect_global_evidence(results, carried_passages)
        claims = [
            claim.to_event_dict()
            for claim in collect_claim_ledger(sections, evidence, self._nli)
        ]
        completed_probes = (
            self._controller.completed_queries if self._controller is not None else []
        )
        pending_probes = (
            self._controller.pending_queries if self._controller is not None else []
        )
        report = ReportFromRun(
            query=self._query,
            summary=summary,
            sections=sections,
            cited_passages=cited_passages,
            reviewed_passages=reviewed_passages,
            all_hits=all_hits,
            unsupported_count=unsupported_total,
            bounded_by=bounded_by,
            depth_tier=self._depth,
            claims=claims,
            completed_probes=completed_probes,
            pending_probes=pending_probes,
        )
        report.report_spec = self._bound.report_spec
        return report
