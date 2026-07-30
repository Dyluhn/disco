"""Deep Research planning, retrieval, execution, and reporting."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Literal

from disco.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    NoOpCondenser,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    ReportEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelExecutionPolicy,
    ModelRole,
    OperatingMode,
    RouterSummarizer,
)
from disco.core.loop import AgentLoop, NeverConfirm, RouterAgent
from disco.core.security import RuleBasedAnalyzer
from disco.core.think import strip_think_spans
from disco.retrieval.deep_research import (
    DeepResearchRun,
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


def _clean_model_text(text: str | None) -> str:
    return strip_think_spans(text or "")


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

    # ── A4.4 iterative research toggle ────────────────────────────────────────

    def _iterative_for(self, conversation_id: str) -> bool:
        return self._state.iterative_for(conversation_id)

    def set_iterative(self, conversation_id: str, enabled: bool) -> None:
        self._state.set_iterative(conversation_id, enabled)

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
        token… → final). Composes the shared router with the live retrieval
        providers, honoring the re-scope controls: the pill picks the answerer
        model, `domains_deny` filters discovery, `drop_weak` prunes the answer, and
        `think` runs the answerer in reasoning mode (it thinks, then the answer
        streams; the reasoning is never emitted as answer tokens).

        W-35/W-33: before streaming, pre-flight the resolved driver (one cheap call)
        and the required encoders (reranker + NLI). On failure, emit a single
        ``{type: error, message}`` frame NAMING the unreachable driver/encoder and
        stop — instead of a doomed stream on a dead model or a silently-wrong answer
        from a degraded reranker/verifier.

        G1/DR-4 F3: when ``conversation_id`` is provided and the conversation has
        pre-attached upload passages (text files the user uploaded in the initial
        box), they are threaded into the rerank step as ``seed_passages`` so they
        compete for ``top_k`` slots alongside live-web content and can be cited.
        OFF-path (no cid or no uploads): byte-identical to the pre-DR-4 code."""
        from disco.retrieval import RouterQueryRewriter
        from disco.retrieval.streaming import stream_research_answer

        search_override = self._search_override_for_sources(sources)
        deps = self._research(search_override=search_override)
        # W-35 + P1-3: pre-flight the model the ANSWER STREAM actually uses for
        # generation — RAG_ANSWERER (streaming.py), NOT AGENT_DRIVER. With those
        # roles assigned to different endpoints, probing AGENT_DRIVER would both
        # FALSE-BLOCK a healthy answerer and MISS a dead one. When a pill is set
        # the override pins every role to the same model, so RAG_ANSWERER still
        # resolves to the picked model.
        driver_reason = await self._preflight.check(
            conversation_id, override=model_override, role=ModelRole.RAG_ANSWERER
        )
        if driver_reason is not None:
            yield {"type": "error", "message": driver_reason}
            return
        requested_space_ids = space_ids or self._spaces.get_space_ids(conversation_id)
        space_owner_id = owner_id
        if space_owner_id is None and conversation_id:
            space_owner_id = self._store.conversation_owner_id_sync(conversation_id)
        requested_space_ids, forbidden_space_ids = self._validated_space_ids(
            requested_space_ids,
            owner_id=space_owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )
        if forbidden_space_ids:
            yield {
                "type": "error",
                "message": "space_forbidden",
                "space_ids": list(forbidden_space_ids),
            }
            return
        # W-33: the live answer grounds on the reranker + NLI verifier. Space
        # grounding also needs the embedder for vector lookup.
        required_encoders = (
            ("reranker", "nli", "embedder") if requested_space_ids else ("reranker", "nli")
        )
        enc_reason = await self._preflight_encoders(deps, required=required_encoders)
        if enc_reason is not None:
            yield {"type": "error", "message": enc_reason}
            return
        # RP-05b §3: MCP retrieval providers join the citation path here too — the
        # composite hands MCP-discovered hits to the SAME GroundingPipeline.
        search, extraction = self._provider.compose_mcp_retrieval(deps)
        router = self._drivers.router(
            pick=model_override,
            enable_thinking=think,
        )
        # G1/DR-4 F3: load seed passages from the upload corpus for this cid.
        # Empty list when no text files were attached (the OFF path).
        seed_passages = self.get_upload_passages(conversation_id) if conversation_id else []
        async for frame in stream_research_answer(
            query,
            router=router,
            search=search,
            extraction=extraction,
            reranker=deps["reranker"],
            nli=deps["nli"],
            # F1: keep-searching on no-answer. Pass the rewriter and allow up to 2
            # extra rounds when the first answer has zero supported claims.  The
            # RouterQueryRewriter reuses the existing QUERY_REWRITER role prompt so
            # no new prompt is introduced.  max_research_rounds=1 is the default, so
            # all existing callers remain byte-identical until this wiring opts them in.
            rewriter=RouterQueryRewriter(router),
            max_research_rounds=2,
            domains_deny=domains_deny,
            drop_weak=drop_weak,
            think=think,
            # G1/DR-4 F3: seed the rerank step with any pre-attached upload passages.
            seed_passages=seed_passages,
            corpus_ids=requested_space_ids,
            embedder=deps.get("embedder"),
            vector_store=self._spaces.space_vector_store(),
        ):
            yield frame

    async def _maybe_run_deep_research(self, conversation_id: str) -> None:
        """The Deep Research driver. Inspects the conversation state to decide
        what to do this turn:
        - No PlanEvent yet + a USER message → decompose + emit synthetic
          PlanEvent + AWAITING_PLAN_APPROVAL. Wait for the user to approve.
        - PlanEvent exists + status is RUNNING with detail="plan_approved" +
          no ReportEvent yet → run the engine, emit progress events, emit
          ReportEvent + StatusEvent(FINISHED).
        - Anything else → no-op (waiting on the user, or already finished).

        All actions persist via the event store; the WS surface streams them.
        Failures surface as ErrorEvent on the log — never raise out of the
        background task."""
        events = await self._store.get_events(conversation_id)
        state = await self._store.get_state(conversation_id)
        plans = [e for e in events if isinstance(e, PlanEvent)]
        reports = [e for e in events if isinstance(e, ReportEvent)]

        # Phase 1: no plan yet → decompose + propose
        if not plans:
            await self._propose_deep_research_plan(conversation_id, events)
            return

        # Phase 1R: PLAN REVISION (live STUCK 2026-07-07). A plan exists but is
        # NOT approved and the user sent revision text. Two ingress shapes:
        #   * the PlanPanel "Send revision" → request_plan → loop.enter_planning
        #     emits RUNNING/'planning' (the status this branch keys on);
        #   * a plain message typed while AWAITING_PLAN_APPROVAL (fresh user
        #     message newer than the latest plan).
        # Without this branch the dispatcher fell through EVERY phase (Phase 2
        # requires detail=='plan_approved') and returned having done nothing —
        # the runtime backstop then marked the conversation STUCK within
        # milliseconds ('loop ended without reaching a terminal state').
        # Re-decompose with every post-question user message folded in as plan
        # constraints and re-propose (revision = prev+1).
        if not reports:
            last_status = next((e for e in reversed(events) if isinstance(e, StatusEvent)), None)
            in_revision_planning = (
                state.execution_status == ConversationStatus.RUNNING
                and last_status is not None
                and last_status.detail == "planning"
            )
            latest_plan_seq = plans[-1].seq or 0
            fresh_msg_while_awaiting = (
                state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
                and any(
                    isinstance(e, MessageEvent)
                    and e.source == EventSource.USER
                    and (e.seq or 0) > latest_plan_seq
                    and (e.message.content or "").strip()
                    for e in events
                )
            )
            if in_revision_planning or fresh_msg_while_awaiting:
                await self._propose_deep_research_plan(conversation_id, events)
                return

        # Phase 2b: RESUME a stopped run (status PAUSED) → continue from the
        # checkpoint. The plan is already approved; flip back to RUNNING and execute,
        # carrying the partial ReportEvent's completed sections so the engine skips
        # the sub-questions that already finished (instead of redoing them). The new
        # full ReportEvent supersedes the partial one from the stop.
        if state.execution_status == ConversationStatus.PAUSED:
            partial = reports[-1] if reports else None
            await self._lifecycle_commands.append_status(
                conversation_id,
                ConversationStatus.RUNNING,
                detail="plan_approved",
            )
            await self._execute_deep_research(conversation_id, plans[-1], resume_from=partial)
            return

        # Phase 2: plan approved, no report yet → run the engine
        if state.execution_status == ConversationStatus.RUNNING and not reports:
            # Distinguish "RUNNING because plan was just approved" from "RUNNING
            # because we're already deep in the engine and the task re-fired."
            # The marker: the last StatusEvent's detail is "plan_approved".
            last_status = next((e for e in reversed(events) if isinstance(e, StatusEvent)), None)
            if last_status is not None and last_status.detail == "plan_approved":
                await self._execute_deep_research(conversation_id, plans[-1])

        # Phase 3: FOLLOW-UP — a FINISHED report exists AND there's a new user
        # message since the last report. Run a follow-up synthesis that reuses
        # the existing report's passages as grounding (RP-13).
        elif reports and self._has_fresh_user_message(events, reports):
            await self._follow_up_deep_research(conversation_id, events, reports[-1])

    async def _propose_deep_research_plan(self, conversation_id: str, events: list) -> None:
        """Decompose the user's query into sub-questions and emit a synthetic
        PlanEvent + AWAITING_PLAN_APPROVAL. Same shape Build's plan gate uses —
        the UI reuses the existing approve_plan / request_plan affordances
        without modification.

        REVISION-AWARE (live STUCK 2026-07-07): the research QUESTION is the
        FIRST user message; every LATER user message is plan feedback ("do not
        include X", "focus on Y"). The old code took the LATEST user message as
        the query, so a re-propose after a revision would have planned the
        REVISION TEXT itself ("do not include any models not released within H1
        of 2026") instead of the revised question. Fold later messages in as
        explicit constraints; the engine's query (first message, see
        _execute_deep_research) stays consistent."""
        user_texts = [
            (e.message.content or "").strip()
            for e in events
            if isinstance(e, MessageEvent)
            and e.source == EventSource.USER
            and (e.message.content or "").strip()
        ]
        if not user_texts:
            return  # nothing to plan; wait
        query = user_texts[0]
        if len(user_texts) > 1:
            constraints = "\n".join(f"- {t}" for t in user_texts[1:])
            query = (
                f"{query}\n\n"
                f"The user revised the research plan with these instructions — "
                f"the sub-questions MUST honor them:\n{constraints}"
            )
        prior_plans = sum(1 for e in events if isinstance(e, PlanEvent))

        # P1-2: the DR INITIAL KICK previously bypassed the driver pre-flight (it
        # lived only in the build path of _run_with_persistence and in the
        # post-approval _execute_deep_research). A dead/unauthed model would set
        # RUNNING below, fail inside decompose_query, and leave the conversation
        # stuck RUNNING with only a system-reminder. Pre-flight the role the run
        # uses for generation (RAG_ANSWERER) BEFORE setting RUNNING; on failure
        # emit a NAMED StatusEvent(ERROR) and do NOT start.
        override = self._settings._get_model_override(conversation_id)
        preflight_reason = await self._preflight.check(
            conversation_id, override=override, role=ModelRole.RAG_ANSWERER
        )
        if preflight_reason is not None:
            await self._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.ERROR, detail=preflight_reason[:200]
            )
            return

        # W-35-fu: the NEXT call (decompose_query) runs via QUERY_REWRITER, NOT
        # RAG_ANSWERER. The RAG_ANSWERER pre-flight above does NOT cover a
        # DIFFERENT, black-holed QUERY_REWRITER model: an immediate exception is
        # caught below and surfaced as ERROR, but a TRUE black-hole (endpoint
        # accepts the socket and never responds) would stall the DR kick
        # unboundedly with the conversation pinned RUNNING. Pre-flight
        # QUERY_REWRITER too (bounded by the DriverPreflight timeout inside
        # _preflight_driver) BEFORE setting RUNNING so EVERY role on the kick
        # path is genuinely bounded. When override pins all roles to one model
        # this is a cache hit (no added latency).
        rewriter_reason = await self._preflight.check(
            conversation_id, override=override, role=ModelRole.QUERY_REWRITER
        )
        if rewriter_reason is not None:
            await self._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.ERROR, detail=rewriter_reason[:200]
            )
            return

        await self._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.RUNNING
        )
        tier = self._depth_for(conversation_id)
        from disco.retrieval.deep_research import bounds_for

        bound = bounds_for(tier)
        # Decompose via QUERY_REWRITER. The decompose call IS the planning
        # step; we emit the result as a PlanEvent directly (no LLM "planning
        # mode" loop needed — the engine owns the work).
        # Honor the model pill here too: the post-approval engine already passes
        # the override (see the DeepResearchRun compose below) — without it HERE,
        # a user's pick silently applied to gather/synthesis but NOT to the
        # decompose/plan step (the exact half-applied-pill bug).
        router = self._drivers.router(
            pick=self._settings._get_model_override(conversation_id)
        )
        recency_window = self._recency_for(conversation_id)
        try:
            subqs = await decompose_query(
                router,
                query,
                max_subq=bound.max_subquestions,
                recency_window=recency_window,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a system reminder
            await self._store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            f"Plan decomposition failed: {type(exc).__name__}: {exc}. "
                            "Try a more specific query.\n"
                            "</system-reminder>"
                        ),
                    ),
                ),
            )
            # P1-2: a decompose failure (e.g. a dead/unauthed QUERY_REWRITER the
            # RAG_ANSWERER pre-flight above did not cover) must take the
            # conversation to ERROR — NOT leave it stuck RUNNING with only the
            # reminder above (the conversation would otherwise spin forever).
            await self._lifecycle_commands.append_status(
                conversation_id,
                ConversationStatus.ERROR,
                detail=(f"Plan decomposition failed: {type(exc).__name__}: {exc}")[:200],
            )
            return

        steps = [PlanStep(title=s.title) for s in subqs]
        summary = (
            f"Multi-section research report on: {query.strip()[:140]}. "
            f"Will gather sources across {len(steps)} sub-questions "
            f"(tier: {tier.value}; cap: {bound.max_sources} sources, "
            f"{bound.max_rounds_per_subq} rounds/subq)."
        )
        plan = PlanEvent(
            summary=summary,
            steps=steps,
            revision=prior_plans + 1,
            context=(
                f"**Query:** {query.strip()}\n\n"
                f"**Depth tier:** {tier.value}\n\n"
                f"**Sub-questions** (each becomes a section of the report):\n\n"
                + "\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(subqs))
            ),
        )
        await self._store.append(conversation_id, plan)
        if self._settings._effective_autonomous(conversation_id):
            # Headless/autonomous DR: no human to approve the plan. Auto-approve
            # inline (emit the same RUNNING/plan_approved StatusEvent approve_plan
            # would) and run the engine directly — otherwise the run stalls forever
            # at AWAITING_PLAN_APPROVAL. Mirrors the Build loop's autonomous
            # plan auto-approve (engine.py).
            await self._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.RUNNING, detail="plan_approved"
            )
            await self._execute_deep_research(conversation_id, plan)
            return
        await self._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.AWAITING_PLAN_APPROVAL, detail=plan.id
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
        synthesis, ending with a ReportEvent + StatusEvent(FINISHED).

        `resume_from` is a prior stopped run's partial ReportEvent: its completed
        sections (+ their cited passages / discovered hits) are carried into the
        engine so resume continues from the checkpoint instead of redoing the
        sub-questions that already finished."""
        from .runtime import _release_process_memory

        # Find the query from the user's last (pre-plan) message.
        events = await self._store.get_events(conversation_id)
        query = next(
            (
                e.message.content
                for e in events
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            plan.summary,
        )
        plan_steps = [s.title for s in plan.steps]
        tier = self._depth_for(conversation_id)
        recency_window = self._recency_for(conversation_id)

        # Build the engine. Providers come from the existing research-stream
        # plumbing (search, extract, reranker, embedder, nli); the vectorstore
        # is per-run (InMemoryVectorStore). The router honors model_override
        # from the leader pill (the existing _router_now path).
        from disco.retrieval import (
            DefaultRetrievalEngine,
            InMemoryVectorStore,
            RouterQueryRewriter,
        )

        research_sources = self._settings.get_research_sources(conversation_id)
        search_override = self._search_override_for_sources(research_sources)
        deps = self._research(search_override=search_override)
        space_ids = self._spaces.get_space_ids(conversation_id)
        space_owner_id = self._store.conversation_owner_id_sync(conversation_id)
        space_ids, forbidden_space_ids = self._validated_space_ids(
            space_ids,
            owner_id=space_owner_id,
        )
        if forbidden_space_ids:
            _LOG.warning(
                "dropping unowned space_ids for %s: %s",
                conversation_id,
                ", ".join(forbidden_space_ids),
            )
        # W-35/W-33: pre-flight the driver + the REQUIRED encoders BEFORE the engine
        # starts. A dead driver or a degraded/empty remote reranker/NLI would
        # otherwise run a long, expensive job that silently produces a wrong report.
        # On failure: emit a NAMED StatusEvent(ERROR) + ErrorEvent and DO NOT run.
        from disco.core import ErrorEvent

        override = self._settings._get_model_override(conversation_id)
        # P1-3: the DR engine synthesises/judges with RAG_ANSWERER — probe THAT
        # role, not AGENT_DRIVER (which DR generation does not use).
        required_encoders = ("reranker", "nli", "embedder") if space_ids else ("reranker", "nli")
        preflight_reason = await self._preflight.check(
            conversation_id, override=override, role=ModelRole.RAG_ANSWERER
        ) or await self._preflight_encoders(deps, required=required_encoders)
        if preflight_reason is not None:
            await self._store.append(
                conversation_id,
                ErrorEvent(code="deep_research_preflight", detail=preflight_reason),
            )
            await self._lifecycle_commands.append_status(
                conversation_id, ConversationStatus.ERROR, detail=preflight_reason[:200]
            )
            return
        # RP-05b §3: deep research's discovery/extraction also flows MCP providers
        # through the SAME engine, so deep-research citations can come from the MCP
        # tier identically to bundled providers.
        search, extraction = self._provider.compose_mcp_retrieval(deps)
        router = self._drivers.router(
            pick=self._settings._get_model_override(conversation_id)
        )
        retrieval_engine = DefaultRetrievalEngine(
            search=search,
            extraction=extraction,
            reranker=deps["reranker"],
            embedder=deps.get("embedder"),
            rewriter=RouterQueryRewriter(router),
            vector_store=self._spaces.space_vector_store(),
        )
        # RAM-aware peak-memory guard (OOM fix): bound how many gather legs run
        # their fetch/extract/embed/rerank body at once. Applies on EVERY tier —
        # not OOMing is correctness, not a free-tier perk. K is derived from
        # available RAM, so a big box runs every leg (effectively unbounded) and a
        # small box is protected. The encoder mode only TUNES the per-leg RAM
        # estimate (in-process FastEmbed legs are heavier than remote-encoder
        # ones); it does NOT gate the cap on/off.
        from disco.retrieval.deep_research.concurrency import gather_concurrency_for

        in_process_encoders = self._provider.in_process_encoders()
        gather_cap = gather_concurrency_for(
            in_process_encoders=in_process_encoders,
            n_subquestions=len(plan_steps),
            env=os.environ,
        )
        # G1/DR-4 F2: load any pre-attached upload passages (text files the user
        # attached in the initial box before submitting).  Empty when no text
        # files were uploaded (the OFF path — byte-identical to pre-DR-4 code).
        upload_passages = self.get_upload_passages(conversation_id)

        run = DeepResearchRun(
            query=query,
            router=router,
            retrieval_engine=retrieval_engine,
            embedder=deps.get("embedder"),
            vector_store=InMemoryVectorStore(),
            nli=deps["nli"],
            depth=tier,
            conversation_id=conversation_id,
            gather_concurrency=gather_cap,
            recency_window=recency_window,
            # G1/DR-4 F2: seed every gather leg with upload passages.
            upload_passages=upload_passages or None,
            corpus_ids=space_ids,
            # A4.4: per-cid iterative-research toggle (default False → OFF path).
            iterative=self._iterative_for(conversation_id),
        )

        # Emit callback: every engine event becomes an Action/Observation pair
        # on the conversation log so the UI's activity feed reflects progress.
        #
        # CORRELATION: gather runs sub-questions CONCURRENTLY, so "the last
        # ActionEvent appended" is usually some OTHER sub-question's search by the
        # time an observation arrives — which left 5 of 6 searches permanently
        # "running" in the UI (the observation pointed at the wrong action). The
        # engine's payloads already carry `subquestion` (+ `round` for search/
        # observation), so we correlate per sub-question here, in this run-scoped
        # map — no engine/protocol change.
        action_by_key: dict[str, str] = {}

        def _corr_key(payload: dict[str, Any]) -> str | None:
            subq = payload.get("subquestion")
            if subq is None:
                return None
            rnd = payload.get("round")
            # gap_reason has no round → correlate to the sub-question's latest search.
            return f"{subq}|{rnd}" if rnd is not None else f"{subq}|latest"

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            # Treat phase + search + synthesize_section as actions (the agent
            # "doing something"), observation kinds as observations (results).
            if kind == "observation" or kind == "gap_reason":
                key = _corr_key(payload)
                action_id = action_by_key.get(key or "") or action_by_key.get(
                    f"{payload.get('subquestion')}|latest", ""
                )
                if not action_id:
                    # No subquestion in the payload (or pre-correlation emit) —
                    # fall back to the previous best-effort "last action".
                    last_action = next(
                        (
                            e
                            for e in reversed(await self._store.get_events(conversation_id))
                            if isinstance(e, ActionEvent)
                        ),
                        None,
                    )
                    action_id = last_action.id if last_action else ""
                await self._store.append(
                    conversation_id,
                    ObservationEvent(
                        tool_result=ToolResult(
                            call_id=f"call_{kind}",
                            tool_name=kind,
                            success=bool(payload.get("ok", True)),
                            content=str({k: v for k, v in payload.items() if k != "ok"}),
                            structured=payload,
                        ),
                        action_id=action_id,
                    ),
                )
                return
            # action-shaped events
            event = ActionEvent(
                thought=f"Deep Research: {kind}",
                tool_call=ToolCall(tool_name=kind, arguments=payload),
            )
            await self._store.append(conversation_id, event)
            key = _corr_key(payload)
            if key is not None:
                action_by_key[key] = event.id
                action_by_key[f"{payload.get('subquestion')}|latest"] = event.id

        # Checkpointed resume: rebuild the retrieval-typed passages/hits from the
        # prior partial ReportEvent's plain dicts so the engine can carry its
        # completed sections forward (and skip those sub-questions).
        resume_sections = list(resume_from.sections) if resume_from else None
        resume_passages = None
        resume_all_hits = None
        if resume_from:
            from disco.retrieval.models import Passage, SearchHit

            resume_passages = [Passage.model_validate(p) for p in resume_from.passages]
            resume_all_hits = [SearchHit.model_validate(h) for h in resume_from.all_hits]

        # Fresh cancel flag for this execution; the engine polls it at each
        # sub-question/section boundary so Stop actually halts the run.
        flag = self._cancellations.begin(conversation_id)

        # D3: initialise per-cid steer/inject queues on the research owner.
        # The WS handler enqueues into these; pop_* closures drain them at
        # each section boundary. Queues are removed in `finally` below so the
        # presence of a key = "a DR run is currently in flight for this cid".
        self._live_state.begin(conversation_id)

        def pop_steers() -> list[str]:
            """Drain the steer queue (called at each section boundary)."""
            return self._live_state.pop_steers(conversation_id)

        def pop_injected_sources() -> list[Passage]:
            """Drain the inject-source queue (called at each section boundary)."""
            return self._live_state.pop_injected_sources(conversation_id)

        try:
            result = await run.run(
                plan_steps,
                emit=emit,
                should_cancel=flag.is_set,
                resume_sections=resume_sections,
                resume_passages=resume_passages,
                resume_all_hits=resume_all_hits,
                pop_steers=pop_steers,
                pop_injected_sources=pop_injected_sources,
            )
        except Exception as exc:  # noqa: BLE001 — surface as ErrorEvent
            from disco.core import ErrorEvent

            await self._store.append(
                conversation_id,
                ErrorEvent(
                    code="deep_research_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
            return
        finally:
            self._cancellations.clear(conversation_id)
            # D3: remove per-cid queues so the WS handler knows no DR run is
            # active for this cid (it tests `cid in _dr_steer` for routing).
            self.forget(conversation_id)
            # Return the run's transient working set (fetch buffers + ONNX batch
            # temporaries, already freed by the time run.run() returned) back to
            # the OS, so a long-lived server doesn't accumulate a permanent RSS
            # floor from bursty DR runs. The retention half of the OOM fix.
            _release_process_memory()

        # Emit the partial-or-final ReportEvent. If the user pressed Stop, the run
        # halted at a checkpoint (bounded_by="stopped") with the partial report
        # preserved → emit PAUSED (resumable), NOT FINISHED.
        await self._store.append(conversation_id, result.to_event())
        stopped = getattr(result, "bounded_by", None) == "stopped"
        await self._lifecycle_commands.append_status(
            conversation_id,
            ConversationStatus.PAUSED if stopped else ConversationStatus.FINISHED,
            detail="stopped" if stopped else None,
        )

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
        """Run a follow-up synthesis on an existing deep-research report.

        Reuses the prior report's corpus (passages) as grounding context so
        the follow-up answer is source-backed. The user's follow-up question
        is the most recent USER message after the report. The answer is
        emitted as message events (agent response) on the conversation log,
        and a new lightweight ReportEvent captures the follow-up.

        This is the RP-13 report-follow-up path — same event-stream-append
        pattern as RP-08's scheduled-task re-injection."""
        # Find the follow-up question (most recent USER message since the report).
        last_report_seq = prior_report.seq or 0
        follow_up_query = next(
            (
                e.message.content
                for e in reversed(events)
                if isinstance(e, MessageEvent)
                and e.source == EventSource.USER
                and (e.seq or 0) > last_report_seq
            ),
            None,
        )
        if not follow_up_query or not follow_up_query.strip():
            return

        await self._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.RUNNING, detail="follow_up"
        )

        # Reuse the prior report's passages as the grounding corpus.
        passages = prior_report.passages or []
        if not passages:
            # No corpus to ground on — just answer directly.
            # WALK-12: emit a phase signal so the UI can show "Writing answer…"
            # instead of appearing frozen (the backend call is otherwise opaque).
            await self._store.append(
                conversation_id,
                ActionEvent(
                    thought="Follow-up: synthesizing answer (no passage corpus)",
                    tool_call=ToolCall(tool_name="phase", arguments={"phase": "synthesizing"}),
                ),
            )
            router = self._drivers.router()
            try:
                answer = await router.complete(
                    CompletionRequest(
                        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                        messages=[LLMMessage(role="user", content=follow_up_query)],
                        temperature=0.0,
                        max_tokens=1400,
                    )
                )
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(
                            role="assistant",
                            content=_clean_model_text(answer.text),
                        ),
                    ),
                )
            except Exception as exc:
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"Follow-up failed: {type(exc).__name__}: {exc}\n"
                                "</system-reminder>"
                            ),
                        ),
                    ),
                )
                await self._lifecycle_commands.append_status(
                    conversation_id,
                    ConversationStatus.ERROR,
                )
                return
        else:
            # WALK-12: emit "reading" phase so the UI shows "Reading sources…"
            # while we build the grounding block from the report corpus.
            await self._store.append(
                conversation_id,
                ActionEvent(
                    thought="Follow-up: reading grounding passages from report corpus",
                    tool_call=ToolCall(tool_name="phase", arguments={"phase": "reading"}),
                ),
            )
            # Build a grounding block from the report's cited passages so the
            # answerer can cite them. Limit to a reasonable context window.
            MAX_PASSAGE_CHARS = 12_000
            passage_blocks: list[str] = []
            total = 0
            for p in passages:
                pid = str(p.get("id", ""))
                ptext = str(p.get("text", ""))
                src = str(p.get("source_title", p.get("source_url", "")))
                block = f"[[{pid}]] ({src})\n{ptext}\n"
                if total + len(block) > MAX_PASSAGE_CHARS:
                    break
                passage_blocks.append(block)
                total += len(block)

            grounding = "\n---\n".join(passage_blocks)
            prompt = (
                f"You are answering a follow-up question about a research report. "
                f"The original query was: {prior_report.query}\n\n"
                f"The report summary: {prior_report.summary}\n\n"
                f"Below are the source passages the report was grounded on. "
                f"Use them to answer the follow-up question. Cite sources "
                f"with [[passage_id]] markers.\n\n"
                f"--- SOURCE PASSAGES ---\n{grounding}\n"
                f"--- END SOURCES ---\n\n"
                f"Follow-up question: {follow_up_query}"
            )

            # WALK-12: emit "synthesizing" phase so the UI shows "Writing answer…"
            await self._store.append(
                conversation_id,
                ActionEvent(
                    thought="Follow-up: synthesizing grounded answer",
                    tool_call=ToolCall(tool_name="phase", arguments={"phase": "synthesizing"}),
                ),
            )
            router = self._drivers.router()
            try:
                answer = await router.complete(
                    CompletionRequest(
                        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
                        messages=[LLMMessage(role="user", content=prompt)],
                        temperature=0.0,
                        max_tokens=1400,
                    )
                )
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.AGENT,
                        message=LLMMessage(
                            role="assistant",
                            content=_clean_model_text(answer.text),
                        ),
                    ),
                )
            except Exception as exc:
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "<system-reminder>\n"
                                f"Follow-up failed: {type(exc).__name__}: {exc}\n"
                                "</system-reminder>"
                            ),
                        ),
                    ),
                )
                await self._lifecycle_commands.append_status(
                    conversation_id,
                    ConversationStatus.ERROR,
                )
                return

        await self._lifecycle_commands.append_status(
            conversation_id, ConversationStatus.FINISHED, detail="follow_up_complete"
        )

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
        payload, media_type, ext = _export(report, fmt)  # md, pdf — in-process
        return payload, media_type, ext
