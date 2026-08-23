"""Deep Research planning, retrieval, execution, and reporting."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    NoOpCondenser,
    PlanEvent,
    ReportEvent,
)
from disco.core.llm import (
    DefaultLLMRouter,
    ModelExecutionPolicy,
    OperatingMode,
    RouterSummarizer,
)
from disco.core.loop import AgentLoop, NeverConfirm, RouterAgent
from disco.core.security import RuleBasedAnalyzer
from disco.core.think import strip_think_spans
from disco.retrieval.deep_research import (
    DepthTier,
    decompose_query,
)
from disco.retrieval.models import Passage
from disco.retrieval.ranking import Embedder

from .build_loop_components import _NoToolExecutor
from .deep_research_provider import DeepResearchProvider
from .deep_research_state import DeepResearchLiveState, DeepResearchState

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore

    from .driver_runtime import DriverPreflight, DriverRuntime
    from .lifecycle_command_service import LifecycleCommandService
    from .run_registry import CancellationRegistry
    from .runtime_settings import RuntimeSettings
    from .space_service import SpaceService

_LOG = logging.getLogger(__name__)

_FOLLOW_UP_SOURCE_CHARS = 12_000
_FOLLOW_UP_REPORT_CHARS = 12_000


def _clean_model_text(text: str | None) -> str:
    return strip_think_spans(text or "")


def _bounded_context_blocks(blocks: list[tuple[str, str]], max_chars: int) -> str:
    """Fit every labeled item into one bounded prompt block.

    Text is apportioned across the remaining items, so one oversized early item
    can never hide the source labels and excerpts that follow it.
    """
    if not blocks or max_chars <= 0:
        return ""
    separator = "\n---\n"
    separator_chars = len(separator) * (len(blocks) - 1)
    label_budget = max(1, (max_chars - separator_chars) // len(blocks) - 1)
    normalized = [(label.strip()[: min(384, label_budget)], text.strip()) for label, text in blocks]
    fixed_chars = sum(len(label) + 1 for label, _text in normalized)
    fixed_chars += separator_chars
    remaining = max(0, max_chars - fixed_chars)
    rendered: list[str] = []
    for index, (label, body) in enumerate(normalized):
        slots_left = len(normalized) - index
        body_budget = remaining // slots_left
        excerpt = _bounded_excerpt(body, body_budget)
        rendered.append(f"{label}\n{excerpt}".rstrip())
        remaining -= len(excerpt)
    return separator.join(rendered)[:max_chars]


def _bounded_excerpt(body: str, budget: int) -> str:
    """Keep both ends of long evidence so late qualifications survive."""

    if budget <= 0:
        return ""
    if len(body) <= budget:
        return body
    marker = "\n[excerpt middle omitted]\n"
    if budget <= len(marker):
        return body[:budget]
    available = budget - len(marker)
    head = available * 2 // 3
    tail = available - head
    return f"{body[:head].rstrip()}{marker}{body[-tail:].lstrip()}"


def _build_grounding_block(passages: list[dict[str, Any]]) -> str:
    """Build a bounded, fairly apportioned block from the saved corpus."""
    blocks = []
    for passage in passages:
        pid = str(passage.get("id", ""))[:128]
        source = str(passage.get("source_title") or passage.get("source_url") or "")[:240]
        blocks.append((f"[[{pid}]] ({source})", str(passage.get("text", ""))))
    corpus_header = f"Saved source corpus: {len(blocks)} passages."
    body_budget = max(0, _FOLLOW_UP_SOURCE_CHARS - len(corpus_header) - 1)
    body = _bounded_context_blocks(blocks, body_budget)
    return f"{corpus_header}\n{body}"[:_FOLLOW_UP_SOURCE_CHARS]


def _build_report_block(report: ReportEvent) -> str:
    """Keep the report's own sections visible when a follow-up asks about them."""
    blocks = [(f"## {section.title[:300]}", section.markdown) for section in report.sections]
    return _bounded_context_blocks(blocks, _FOLLOW_UP_REPORT_CHARS)


def _build_follow_up_prompt(
    prior_report: ReportEvent,
    passages: list[dict[str, Any]],
    follow_up_query: str,
    *,
    history: str = "",
) -> str:
    grounding = _build_grounding_block(passages)
    report_sections = _build_report_block(prior_report)
    history_block = (
        f"--- EARLIER FOLLOW-UP EXCHANGES ---\n{history}\n--- END EARLIER EXCHANGES ---\n\n"
        if history
        else ""
    )
    return (
        f"You are answering a follow-up question about a research report. "
        f"The original query was: {prior_report.query}\n\n"
        f"The report summary: {prior_report.summary}\n\n"
        f"Below are the report sections the user is referring to.\n\n"
        f"--- REPORT SECTIONS ---\n{report_sections}\n"
        f"--- END REPORT ---\n\n"
        f"Below are the source passages the report was grounded on. "
        f"Use them to answer the follow-up question. Cite sources "
        f"with [[passage_id]] markers.\n\n"
        f"--- SOURCE PASSAGES ---\n{grounding}\n"
        f"--- END SOURCES ---\n\n"
        f"{history_block}"
        f"Follow-up question: {follow_up_query}"
    )


class DeepResearchService:
    """Deep Research + live-research-stream logic.

    The service constructor takes explicit named owners — never a whole
    runtime. The owners are the narrow collaborators the service actually
    uses: the event store, lifecycle commands, driver runtime, settings,
    driver preflight, space service, cancellation registry, the Deep Research
    provider (which owns provider/secret/origin resolution), the project
    runtime service (for space-id validation), and the optional Deep Research
    state.
    """

    def __init__(
        self,
        *,
        store: SqliteEventStore,
        lifecycle_commands: LifecycleCommandService,
        drivers: DriverRuntime,
        settings: RuntimeSettings,
        preflight: DriverPreflight,
        spaces: SpaceService,
        cancellations: CancellationRegistry,
        provider: DeepResearchProvider,
        state: DeepResearchState | None = None,
        live_state: DeepResearchLiveState | None = None,
    ) -> None:
        self._store = store
        self._lifecycle_commands = lifecycle_commands
        self._drivers = drivers
        self._settings = settings
        self._preflight = preflight
        self._spaces = spaces
        self._cancellations = cancellations
        self._provider = provider
        self._state = state or DeepResearchState()
        self._live_state = live_state or DeepResearchLiveState()

    def enqueue_steer(self, conversation_id: str, text: str) -> bool:
        return self._live_state.enqueue_steer(conversation_id, text)

    def inject_source(self, conversation_id: str, passage: Passage) -> bool:
        return self._live_state.inject_source(conversation_id, passage)

    def forget(self, conversation_id: str) -> None:
        self._state.forget(conversation_id)
        self._live_state.forget(conversation_id)

    def add_upload_passages(
        self,
        conversation_id: str,
        passages: list[Passage],
    ) -> None:
        self._state.add_upload_passages(conversation_id, passages)

    def get_upload_passages(self, conversation_id: str) -> list[Passage]:
        return self._state.upload_passages(conversation_id)

    def embedder(self) -> Embedder:
        """Return the live research embedder for Space corpus ingestion."""
        return self._provider.embedder()

    def _validated_space_ids(
        self,
        space_ids: frozenset[str],
        *,
        owner_id: str | None,
        include_unclaimed_legacy: bool = False,
    ) -> tuple[frozenset[str], tuple[str, ...]]:
        return self._spaces.validated_space_ids(
            space_ids,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )

    def _compose_deep_research_loop(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        *,
        driver_context_window: int | None = None,
    ) -> AgentLoop:
        """Compose the loop frame for Deep Research. The loop itself doesn't drive
        the research — `_run_with_persistence` short-circuits `loop.run()` for
        this surface and runs `DeepResearchRun` directly. The AgentLoop exists
        here as the host for the plan-approval gate (we reuse Build's plan
        machinery: same PlanEvent, same AWAITING_PLAN_APPROVAL status, same
        approve_plan WS frame). No tools, no risk gate, no condenser — Deep
        Research's iteration lives in the engine, not the loop."""
        # Deep Research intentionally uses the standard tier: it is a read-only
        # pipeline (no tool execution), so the weak-model assist compensations
        # (F-features) are not applicable. Explicit model_policy= satisfies the
        # production-path EXPLICIT-pass gate (no silent default).
        return AgentLoop(
            conversation_id,
            self._store,
            agent,
            _NoToolExecutor(),
            router,
            RuleBasedAnalyzer(),
            NeverConfirm(),  # no risky action gate — read-only research
            NoOpCondenser(),  # the engine manages its own corpus; no View condense
            RouterSummarizer(router),
            mode=OperatingMode.PLANNING,
            # Configure planning_tools so the loop's mode-tracking is consistent
            # with Build (PLANNING → LONG_HORIZON on approve_plan). The actual
            # plan-event is emitted synthetically by _run_with_persistence; the
            # loop never sees a submit_plan tool call.
            planning_tools=frozenset({"submit_plan"}),
            model_policy=ModelExecutionPolicy.standard(),
            driver_context_window=driver_context_window,
        )

    def _depth_for(self, conversation_id: str) -> DepthTier:
        return self._state.depth_for(conversation_id)

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        self._state.set_depth(conversation_id, tier)

    # ── DR-3 recency window (E2) ──────────────────────────────────────────────

    def set_recency(self, conversation_id: str, window: str | None) -> None:
        self._state.set_recency(conversation_id, window)

    def _recency_for(self, conversation_id: str) -> Literal["month", "week"] | None:
        return self._state.recency_for(conversation_id)

    def _research(self, search_override: Any | None = None) -> dict[str, Any]:
        return self._provider.research(search_override=search_override)

    def _search_override_for_sources(self, sources: Sequence[str] | None) -> Any | None:
        return self._provider.search_override_for_sources(sources)

    async def _preflight_encoders(
        self, deps: dict[str, Any], *, required: tuple[str, ...]
    ) -> str | None:
        """W-33: validate the REQUIRED Deep Research encoders are configured AND
        reachable BEFORE the run starts. Remote encoders degrade SILENTLY — a
        TeiReranker on an HTTP error returns input order, a SidecarNLIVerifier
        returns neutral, a None embedder yields no vectors — so an empty/unreachable
        remote URL (the `encoders.remote=true` + empty-url misconfig) produces a
        quietly-wrong report. Returns None when every required encoder is usable,
        else a VERBOSE reason string NAMING the exact encoder.

        Bundled/in-process encoders have no `probe()` — they load lazily and raise
        EncoderUnavailable on a RAM failure (surfaced at use), so they are treated
        as present here. Only the live HTTP clients (TeiReranker / OpenAIEmbedder /
        SidecarNLIVerifier) carry a `probe()` that we await."""
        from disco.retrieval.local_encoders import EncoderUnavailable

        for name in required:
            client = deps.get(name)
            if client is None:
                return (
                    f"Deep Research needs the {name}, but it isn't connected "
                    f"(no {name} is configured). Set it in Settings -> Encoders, "
                    "or switch encoders to in-process (local)."
                )
            probe = getattr(client, "probe", None)
            if probe is None:
                continue  # bundled/in-process encoder — present (raises on RAM fail)
            try:
                await probe()
            except EncoderUnavailable as exc:
                return str(exc)
        return None

    async def research_stream(
        self,
        query: str,
        *,
        model_override: str | None = None,
        drop_weak: bool = False,
        domains_deny: frozenset[str] = frozenset(),
        think: bool = False,
        conversation_id: str | None = None,
        space_ids: frozenset[str] = frozenset(),
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
        sources: Sequence[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a live grounded answer as the UI's research frames (state →
        token… → final). See ``stream_parts.prepare`` for the pre-stream
        preflight/composition (driver + encoders + MCP + seed passages); this
        method owns only the part that must actually be a generator — the
        token stream itself, plus the re-scope controls (`domains_deny`,
        `drop_weak`, `think`) that shape it."""
        from ._deep_research_service_parts import stream as stream_parts

        error, prepared = await stream_parts.prepare(
            self,
            conversation_id,
            model_override=model_override,
            think=think,
            space_ids=space_ids,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
            sources=sources,
        )
        if error is not None:
            yield error
            return
        assert prepared is not None

        from disco.retrieval import RouterQueryRewriter
        from disco.retrieval.streaming import stream_research_answer

        async for frame in stream_research_answer(
            query,
            router=prepared.router,
            search=prepared.search,
            extraction=prepared.extraction,
            reranker=prepared.deps["reranker"],
            nli=prepared.deps["nli"],
            # F1: keep-searching on no-answer. Pass the rewriter and allow up to 2
            # extra rounds when the first answer has zero supported claims.  The
            # RouterQueryRewriter reuses the existing QUERY_REWRITER role prompt so
            # no new prompt is introduced.  max_research_rounds=1 is the default, so
            # all existing callers remain byte-identical until this wiring opts them in.
            rewriter=RouterQueryRewriter(prepared.router),
            max_research_rounds=2,
            domains_deny=domains_deny,
            drop_weak=drop_weak,
            think=think,
            # G1/DR-4 F3: seed the rerank step with any pre-attached upload passages.
            seed_passages=prepared.seed_passages,
            corpus_ids=prepared.space_ids,
            embedder=prepared.deps.get("embedder"),
            vector_store=self._spaces.space_vector_store(),
        ):
            yield frame

    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
        """The Deep Research phase dispatcher. See ``dispatch_parts.run_phase``
        for the full phase table (propose → maybe-run → execute → follow-up)
        this drives every turn. All actions persist via the event store; the
        WS surface streams them. Failures surface as ErrorEvent on the log —
        never raise out of the background task."""
        from ._deep_research_service_parts import dispatch as dispatch_parts

        await dispatch_parts.run_phase(self, conversation_id)

    async def _propose_deep_research_plan(self, conversation_id: str, events: list) -> None:
        """Decompose the user's query into sub-questions and emit a synthetic
        PlanEvent + AWAITING_PLAN_APPROVAL. See ``plan_parts`` for the
        query/constraint extraction, preflight, and PlanEvent construction —
        the ``decompose_query`` call stays here because tests monkeypatch it
        on this module (``monkeypatch.setattr(drs, "decompose_query", ...)``);
        relocating the call site would silently defeat that patch point."""
        from ._deep_research_service_parts import plan as plan_parts

        query = plan_parts.build_query_with_constraints(events)
        if query is None:
            return  # nothing to plan; wait
        prior_plans = sum(1 for e in events if isinstance(e, PlanEvent))

        override = self._settings._get_model_override(conversation_id)
        preflight_reason = await plan_parts.preflight_plan_roles(self, conversation_id, override)
        if preflight_reason is not None:
            await self._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.ERROR, detail=preflight_reason[:200]
            )
            return

        await self._lifecycle_commands.append_status(conversation_id, ConversationStatus.RUNNING)
        ctx = plan_parts.prepare_decompose_context(self, conversation_id)
        # Decompose via QUERY_REWRITER. The decompose call IS the planning
        # step; we emit the result as a PlanEvent directly (no LLM "planning
        # mode" loop needed — the engine owns the work).
        try:
            subqs = await decompose_query(
                ctx.router,
                query,
                max_subq=ctx.bound.initial_probe_count,
                recency_window=ctx.recency_window,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a system reminder
            await plan_parts.handle_decompose_failure(self, conversation_id, exc)
            return

        await plan_parts.finalize_plan(
            self,
            conversation_id,
            query=query,
            tier=ctx.tier,
            bound=ctx.bound,
            subqs=subqs,
            prior_plans=prior_plans,
        )

    async def _execute_deep_research(
        self,
        conversation_id: str,
        plan: PlanEvent,
        *,
        resume_from: ReportEvent | None = None,
    ) -> None:
        """The post-approval driver: run DeepResearchRun on the approved plan,
        emitting Action/Observation events for every retrieval round + section
        synthesis, ending with a ReportEvent + StatusEvent(FINISHED). See
        ``execute_parts.run_execute`` for the full sequencing.

        `resume_from` is a prior stopped run's partial ReportEvent: its completed
        sections (+ their cited passages / discovered hits) are carried into the
        engine so resume continues from the checkpoint instead of redoing the
        sub-questions that already finished."""
        from ._deep_research_service_parts import execute as execute_parts

        await execute_parts.run_execute(self, conversation_id, plan, resume_from=resume_from)

    @staticmethod
    def _has_fresh_user_message(events: list[Event], reports: list[ReportEvent]) -> bool:
        """True when a USER message arrived AFTER the latest ReportEvent —
        a follow-up question the user asked on a finished report."""
        if not reports:
            return False
        last_report_seq = reports[-1].seq or 0
        for e in reversed(events):
            if (
                isinstance(e, MessageEvent)
                and e.source == EventSource.USER
                and (e.seq or 0) > last_report_seq
            ):
                return True
        return False

    async def _follow_up_deep_research(
        self,
        conversation_id: str,
        events: list[Event],
        prior_report: ReportEvent,
    ) -> None:
        """Run a follow-up synthesis on an existing deep-research report,
        reusing its passages as grounding (RP-13 — same event-stream-append
        pattern as RP-08's scheduled-task re-injection). See
        ``followup_parts.run_follow_up`` for the full sequencing; the
        grounding-block/prompt builders stay in this module (see
        ``_build_follow_up_prompt`` above) alongside the ``[[{pid}]]``
        citation format a source-scan regression test pins to this file."""
        from ._deep_research_service_parts import followup as followup_parts

        await followup_parts.run_follow_up(self, conversation_id, events, prior_report)

    async def export_report(
        self,
        conversation_id: str,
        fmt: str,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> tuple[bytes, str, str] | None:
        """Export the latest ReportEvent from a conversation as MD or PDF.

        Returns (payload_bytes, media_type, filename_extension) on success,
        or None when no ReportEvent exists for this conversation (the caller
        maps None → 404). Raises ValueError for unknown `fmt` (the caller
        maps ValueError → 400).

        The endpoint is generic over ReportEvent — today only deep_research
        conversations emit one; standard research and build do not."""
        from .report_export import export_report as _export

        events = await self._store.get_events(conversation_id)
        reports = [e for e in events if isinstance(e, ReportEvent)]
        if not reports:
            return None
        # The latest report (deep research emits only one; safe for future
        # multi-report conversations).
        report = reports[-1]
        title = await self._store.get_title(conversation_id)
        payload, media_type, ext = _export(report, fmt, title=title)
        return payload, media_type, ext
