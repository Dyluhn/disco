"""DeepResearchRun — the orchestrator wiring the research agent to the writer.

v2 (PKG-35): research is an agentic loop (`agent.run_research_agent`) — the
lead model sees the evidence map every turn and decides searches, pivots, and
sufficiency; the system enforces only hard budgets. Writing is one
whole-report pass with a fixed-rubric review (`writer.write_report`). There
is no plan gate: research starts immediately and the model's brief is the
first streamed output.

The run has exactly three outcomes: a complete report, an exception surfaced
by the agent-server as an ErrorEvent + ERROR status, or a Stop checkpoint
(`bounded_by="stopped"`, no report prose) the user can resume.
Hard research bounds are surfaced honestly via `bounded_by`; the writing
phase is bounded by its own bounded retries, never a shrinking deadline.
Progress events flow through the injected `emit` callback so the agent-server
appends them as ActionEvents / ObservationEvents.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from disco.core import ReportEvent, ReportSection, ResearchCheckpointEvent
from disco.core.llm import LLMRouter

from ..engine import RetrievalEngine
from ..models import Passage as RetrievalPassage
from ..ranking import Embedder
from ..vectorstore import VectorStore
from ._agent_state import _AgentState
from ._engine_parts._report import collect_report_data
from ._progress_events import RESEARCH_POOL_ACTION
from ._recovery_state import LoopCursor, RecoveryCheckpoint
from ._run_checkpoint import RunCheckpoints
from ._stop import ResearchStopped, stop_requested
from ._turn_accounting import turn_accounting_rollup
from ._untested_angles import untested_angles as trail_untested_angles
from ._untested_angles import untested_angles_trail_entry
from .agent import ResearchOutcome, run_research_agent
from .depth import DepthBound, DepthTier, bounds_for
from .pool import ResearchPoolError, write_pool
from .writer import REVIEW_OUTCOME_VERDICT, WrittenReport, write_report

_LOG = logging.getLogger(__name__)

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
        review_notes: list[str] | None = None,
        unverified_sentences: list[str] | None = None,
        research_trail: list[dict[str, Any]] | None = None,
        recency_window: Literal["month", "week"] | None = None,
        verifier_failures: int = 0,
    ) -> None:
        self.query = query
        self.recovery_reference: dict[str, Any] | None = None
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
        # Soft editorial notes from the writer — metadata only, never prose
        # edits.
        self.review_notes = list(review_notes or [])
        # Sentences the final claim ledger could not support against the
        # passages they cite. The report shipped with them (a run error is for
        # provider failure and structural impossibility), so they are declared
        # verbatim — kept beside `review_notes`, never blended into them.
        self.unverified_sentences = list(unverified_sentences or [])
        # Grounding checks that no-op'd to neutral on a verifier transport
        # failure. Surfaced in the report meta so partial grounding is never
        # indistinguishable from a clean pass.
        self.verifier_failures = verifier_failures
        self.research_trail = list(research_trail or [])
        self.recency_window: Literal["month", "week"] | None = recency_window
        # Synthesis owns how much of this envelope the evidence can support;
        # retrieval never borrows it.  The concrete tier values are available
        # from ``DeepResearchRun.bound.report_spec`` and this transport carries
        # the same shape for downstream report assembly.
        self.report_spec: dict[str, int] | None = None
        # Queries the search or extraction layer never put to the world, so the
        # pool holds nothing about them for reasons that have nothing to do
        # with the subject. Set by the assembler beside `report_spec` (the
        # constructor is at its collaborator cap) and carried verbatim in the
        # report meta, so a reader is never left mistaking an outage for a
        # finding about the world.
        self.untested_angles: list[str] = []
        # How the turn budget was really spent: model turns vs turns that cost
        # nothing because the infrastructure ate them. The audit trail carries
        # this per turn, but the trail rides on a Stop CHECKPOINT and this is a
        # finished REPORT — without the rollup a reader cannot tell a run that
        # spent every turn researching from one whose pool was cooling.
        self.turn_accounting: dict[str, int] = {}
        # Whether the fixed-rubric MODEL review graded the report that shipped
        # ("verdict") or never returned one ("unavailable"). Set by the
        # assembler beside `untested_angles` (the constructor is at its
        # collaborator cap) and surfaced in the report meta only when a review
        # did NOT happen, so an unreviewed ship is never indistinguishable
        # from a reviewed one.
        self.review_outcome: str = REVIEW_OUTCOME_VERDICT

    def to_event(self) -> ReportEvent | ResearchCheckpointEvent:
        """Convert into the Core ReportEvent for persistence on the log.
        Retrieval-typed Passage/SearchHit are dumped to plain dicts so `core`
        stays free of any `retrieval` import."""
        from ._report_event import fit_report_event

        if self.bounded_by == "stopped":
            return self._checkpoint_event()

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
        meta: dict[str, Any] = {}
        if counts := _grounding_counts(self.claims):
            meta["grounding_counts"] = counts
        if self.review_notes:
            meta["review_notes"] = list(self.review_notes)
        if self.unverified_sentences:
            meta["unverified_sentences"] = list(self.unverified_sentences)
        if self.untested_angles:
            meta["untested_angles"] = list(self.untested_angles)
        if self.turn_accounting:
            meta["turn_accounting"] = dict(self.turn_accounting)
        if self.verifier_failures:
            meta["verifier_failures"] = self.verifier_failures
        if self.review_outcome != REVIEW_OUTCOME_VERDICT:
            meta["review_outcome"] = self.review_outcome
        if meta:
            event = event.model_copy(update={"meta": {**event.meta, **meta}})
        return fit_report_event(event)

    def _checkpoint_event(self) -> ResearchCheckpointEvent:
        from ._report_event import fit_research_checkpoint_event

        checkpoint = ResearchCheckpointEvent(
            query=self.query,
            passages=[p.model_dump() for p in [*self.cited_passages, *self.reviewed_passages]],
            all_hits=[h.model_dump() for h in self.all_hits],
            trail=self.research_trail,
            completed_queries=self.completed_probes,
            depth_tier=self.depth_tier,
            recency_window=self.recency_window,
            meta={"research_recovery_ref": self.recovery_reference}
            if self.recovery_reference
            else {},
        )
        return fit_research_checkpoint_event(checkpoint)


def _grounding_counts(claims: list[dict[str, Any]]) -> dict[str, int]:
    """Only summarize reports carrying the explicit four-state measurements."""
    statuses = ("supported", "contradicted", "unresolved", "unavailable")
    if not claims or any(claim.get("verification_status") not in statuses for claim in claims):
        return {}
    return {
        status: sum(claim["verification_status"] == status for claim in claims)
        for status in statuses
    }


def _trail_queries(trail: list[dict[str, Any]]) -> list[str]:
    """The deduped, ordered queries the agent issued — the checkpoint's
    `completed_probes`, which feed `resume_trail` on resume."""
    queries = [
        str(entry["query"])
        for entry in trail
        if entry.get("kind") == "search" and isinstance(entry.get("query"), str)
    ]
    return list(dict.fromkeys(queries))


def _resume_trail(
    trail: list[dict[str, Any]] | None, queries: list[str] | None
) -> list[dict[str, Any]]:
    if trail:
        return list(trail)
    return [{"kind": "search", "query": query, "resumed": True} for query in queries or []]


class DeepResearchRun:
    """One Deep Research run, end-to-end. Constructed per conversation by the
    agent-server runtime with all dependencies injected. Single-shot:
    `await run.run()` produces the assembled ReportFromRun."""

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
        checkpointing: bool = False,
        recovery: RecoveryCheckpoint | None = None,
    ) -> None:
        self._query = query
        self._router = router
        self._engine = retrieval_engine
        # The v2 agent loop retrieves per-query and the writer reads the full
        # pool directly; embedder/vector_store/gather_concurrency stay on the
        # constructor for wiring compatibility with the agent-server runtime.
        self._embedder = embedder
        self._vector_store = vector_store
        self._gather_concurrency = gather_concurrency
        self._nli = nli
        self._bound: DepthBound = recovery.bound if recovery else bounds_for(depth)
        self._depth = depth if isinstance(depth, str) else depth.value
        self._namespace = conversation_id
        self._recency_window: Literal["month", "week"] | None = recency_window
        # G1/DR-4 F2: pre-attached upload passages seed the evidence pool.
        # None / [] → OFF path.
        self._upload_passages: list[Any] = upload_passages or []
        # Spaces: durable named corpora to include in every RetrievalRequest.
        self._corpus_ids = corpus_ids
        self._recovery = recovery
        self._checkpointing = checkpointing
        self._checkpoints = RunCheckpoints(
            conversation_id,
            query,
            self._depth,
            recency_window,
            recovery,
        )

    @property
    def bound(self) -> DepthBound:
        return self._bound

    async def run(
        self,
        plan_steps: list[str] | None = None,
        *,
        emit: EmitFn,
        should_cancel: Callable[[], bool] | None = None,
        resume_sections: list[ReportSection] | None = None,
        resume_passages: list[RetrievalPassage] | None = None,
        resume_all_hits: list[Any] | None = None,
        resume_completed_probes: list[str] | None = None,
        resume_pending_probes: list[str] | None = None,
        resume_research_trail: list[dict[str, Any]] | None = None,
        pop_steers: PopSteersFn = None,
        pop_injected_sources: PopInjectedSourcesFn = None,
    ) -> ReportFromRun:
        """Execute the run. Returns the assembled report. `emit` is awaited
        between phases so the agent-server can write events to the
        conversation log; we never write to the store directly.

        `plan_steps` is a legacy input from the retired plan-approval flow:
        accepted for call-site compatibility, ignored beyond logging — the
        agent decides its own searches (decision #7). `should_cancel` makes
        Stop REAL at every boundary the run has: each research turn, each pass
        of a hold, before the writer opens its first stream, and then after the
        draft, each continuation, each review verdict and each rework pass.
        Whichever one sees it, the run halts there and returns a CHECKPOINT
        (`bounded_by="stopped"`, no report prose) the agent-server persists as
        a resumable PAUSED state.

        Resume (checkpointed): the prior stopped run's evidence
        (`resume_passages` / `resume_all_hits`) seeds the pool, and its
        `resume_completed_probes` seed the audit trail; the report is written
        fresh from the combined pool. `resume_sections` /
        `resume_pending_probes` are accepted for signature compatibility (v2
        recompiles the whole report from evidence, never from prior prose).

        D3 mid-run steer / inject: `pop_steers` strings become priority
        guidance in the agent's next turn; `pop_injected_sources` passages
        are admitted directly to the pool. Both default None → OFF-path."""
        if plan_steps:
            _LOG.debug("ignoring %d legacy plan steps (agentic v2 loop)", len(plan_steps))
        if resume_sections or resume_pending_probes:
            _LOG.debug("resume carries evidence forward; prior sections are recompiled")
        resume_trail = _resume_trail(resume_research_trail, resume_completed_probes)

        async def checkpoint(state: _AgentState, cursor: LoopCursor) -> None:
            await self._checkpoints.record(state, cursor, self._bound, emit)

        if self._recovery is not None and self._recovery.stage == "writing":
            await emit("phase", {"phase": "writing"})
            saved = self._recovery.state
            outcome = ResearchOutcome(
                brief=saved.brief,
                passages=saved.passages,
                all_hits=saved.all_hits,
                trail=saved.trail,
                bounded_by=self._recovery.bounded_by,
                coverage=saved.coverage,
            )
            await self._checkpoints.commit(self._recovery, emit)
        else:
            outcome = await run_research_agent(
                self._query,
                router=self._router,
                retrieval_engine=self._engine,
                bound=self._bound,
                namespace=self._namespace,
                emit=emit,
                should_cancel=should_cancel,
                pop_steers=pop_steers,
                pop_injected_sources=pop_injected_sources,
                recency_window=self._recency_window,
                corpus_ids=self._corpus_ids,
                upload_passages=list(self._upload_passages) or None,
                resume_passages=resume_passages,
                resume_trail=resume_trail or None,
                recovery=self._recovery,
                checkpoint=checkpoint if self._checkpointing else None,
            )
        carried_hits: list[Any] = list(resume_all_hits or [])
        # A Stop pressed while the last research turn was settling lands here,
        # before the writer opens its first stream — the cheapest boundary the
        # run has, so it is checked before one is spent.
        if outcome.bounded_by == "stopped" or stop_requested(should_cancel):
            return self._assemble_checkpoint(outcome, carried_hits)
        await self._save_pool(outcome, emit=emit)
        if self._checkpointing:
            await self._checkpoints.writing(outcome.bounded_by, emit)
        try:
            return await self._write_and_assemble(
                outcome, emit=emit, should_cancel=should_cancel, carried_hits=carried_hits
            )
        except ResearchStopped as stopped:
            # Publish no partial report. The checkpoint reference retains the
            # latest committed writer boundary as well as the research pool.
            _LOG.info("deep research stopped in the writer at %s", stopped.boundary)
            return self._assemble_checkpoint(outcome, carried_hits)

    async def write_from_pool(self, outcome: ResearchOutcome, *, emit: EmitFn) -> ReportFromRun:
        """Write the report from a SAVED pool — the same writer call and the
        same assembly a finished run does, with the research loop skipped.

        The agent-server's ``/research/write-from-pool`` calls this so a writer
        change can be judged against a run that already happened. Stop is not
        wired: a replay has no evidence to checkpoint, so there is nothing for
        a Stop to produce that re-running this entry point does not."""
        return await self._write_and_assemble(
            outcome, emit=emit, should_cancel=None, carried_hits=[]
        )

    async def _write_and_assemble(
        self,
        outcome: ResearchOutcome,
        *,
        emit: EmitFn,
        should_cancel: Callable[[], bool] | None,
        carried_hits: list[Any],
    ) -> ReportFromRun:
        """The writing half of a run: one whole-report pass, then assembly.
        Shared verbatim by a live run and a replay so the two can never write
        the same pool into two different reports."""

        async def save_writer(state: Any) -> None:
            await self._checkpoints.writer(state, emit)

        written = await write_report(
            self._query,
            outcome,
            router=self._router,
            nli=self._nli,
            bound=self._bound,
            emit=emit,
            recency_window=self._recency_window,
            conversation_id=self._namespace,
            should_cancel=should_cancel,
            checkpoint=save_writer if self._checkpointing else None,
            resume=self._checkpoints.latest.writer if self._checkpoints.latest else None,
        )
        return self._assemble_report(outcome, written, carried_hits)

    async def _save_pool(self, outcome: ResearchOutcome, *, emit: EmitFn) -> None:
        """Save the writer's whole input beside the run, and say where.

        This is the only moment the whole outcome exists — the report event
        keeps a truncated, reordered subset of it — so a writer change is
        evaluated by replaying this file instead of researching again. A pool
        that cannot be written is a warning, never the end of the run: the
        report is the product and this file is a tool for the next change."""
        try:
            path, size = write_pool(
                self._namespace,
                query=self._query,
                depth_tier=self._depth,
                recency_window=self._recency_window,
                outcome=outcome,
            )
        except ResearchPoolError:
            _LOG.warning("could not save the research pool for %s", self._namespace, exc_info=True)
            return
        _LOG.info("saved research pool %s (%d bytes)", path, size)
        await emit(RESEARCH_POOL_ACTION, {"pool_id": self._namespace, "bytes": size})

    def _assemble_checkpoint(
        self, outcome: ResearchOutcome, carried_hits: list[Any]
    ) -> ReportFromRun:
        """A Stop checkpoint, not a report: keep the gathered pool (as
        reviewed passages) and the audit trail (as completed probes for
        resume), write no prose."""
        _cited, reviewed, all_hits = collect_report_data(
            [], outcome.passages, carried_hits, outcome.all_hits
        )
        report = ReportFromRun(
            query=self._query,
            summary="",
            sections=[],
            cited_passages=[],
            reviewed_passages=reviewed,
            all_hits=all_hits,
            unsupported_count=0,
            bounded_by="stopped",
            depth_tier=self._depth,
            claims=[],
            completed_probes=_trail_queries(outcome.trail),
            pending_probes=[],
            research_trail=outcome.trail,
            recency_window=self._recency_window,
        )
        report.recovery_reference = self._checkpoints.reference
        report.report_spec = self._bound.report_spec
        return report

    def _assemble_report(
        self, outcome: ResearchOutcome, written: WrittenReport, carried_hits: list[Any]
    ) -> ReportFromRun:
        """Assemble the final report: split the pool into cited vs reviewed
        against the written sections, dedupe hits (carried first so a resumed
        run's citations resolve), and carry the writer's ledger, its trail
        rows, and its declared unverified sentences."""
        # The writer's own rows — continuation, structure re-ask, review,
        # rework, unverified count — join the run's trail in the order they
        # happened, so the trace and the report metadata tell one story.
        outcome.trail.extend(written.trail)
        if written.verifier_failures:
            # The run's own audit trail records the degradation too, so the
            # trace and the report metadata tell the same story.
            outcome.trail.append(
                {"kind": "verifier_degraded", "failures": written.verifier_failures}
            )
        unreached = list(trail_untested_angles(outcome.trail))
        if unreached:
            # …and how many angles the run asked for and never reached. The
            # queries themselves travel in the report meta; the trail row is
            # the count, the shape every other audit row here uses.
            outcome.trail.append(untested_angles_trail_entry(len(unreached)))
        cited, reviewed, all_hits = collect_report_data(
            written.sections,
            outcome.passages,
            carried_hits,
            outcome.all_hits,
            additional_cited_ids=written.summary_cited_passage_ids,
        )
        report = ReportFromRun(
            query=self._query,
            summary=written.summary,
            sections=written.sections,
            cited_passages=cited,
            reviewed_passages=reviewed,
            all_hits=all_hits,
            unsupported_count=written.unsupported_count,
            bounded_by=outcome.bounded_by,
            depth_tier=self._depth,
            claims=written.claims,
            completed_probes=_trail_queries(outcome.trail),
            pending_probes=[],
            review_notes=written.review_notes,
            unverified_sentences=written.unverified_sentences,
            verifier_failures=written.verifier_failures,
            research_trail=outcome.trail,
        )
        report.report_spec = self._bound.report_spec
        report.untested_angles = unreached
        report.turn_accounting = turn_accounting_rollup(
            outcome.trail, total_turns=self._bound.max_research_turns
        )
        report.review_outcome = written.review_outcome
        return report
