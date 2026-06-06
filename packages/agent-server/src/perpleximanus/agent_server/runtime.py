"""The conversation runtime — runs the agent loop with real inference (Stage 2).

Phase 0 only appended events. This wires `AgentLoop` (core) + a real Qwen-backed
router into the request path: a user message KICKS the loop, which runs in the
background, calls the model via the OpenAI adapter, and appends events — which the
store already publishes to the WebSocket the UI consumes (history-then-live).

The Research surface defaults: `NeverConfirm` (no human gate) + no tools (the
model answers directly). The grounded research pipeline (Stage 4) is exposed
separately via `research_stream()` — it is NOT the agent loop; it is the
rewrite→search→extract→rerank→generate→verify pipeline streamed as the UI's
grounded-answer frames. The Build surface's `ConfirmRisky` stays dormant.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any

from perpleximanus.core import (
    DEFAULT_OWNER_ID,
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    LLMSummarizingCondenser,
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
from perpleximanus.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    DriverPrompts,
    ModelRole,
    OperatingMode,
    RouterSummarizer,
    SandboxSettings,
    SecretStore,
)
from perpleximanus.core.llm.config import RouterConfig
from perpleximanus.core.llm.secrets import OPENROUTER_API_KEY_ENV
from perpleximanus.core.llm.wiring import build_providers
from perpleximanus.core.loop import AgentLoop, ConfirmRisky, NeverConfirm, RouterAgent
from perpleximanus.core.security import RuleBasedAnalyzer
from perpleximanus.core.store.sqlite import SqliteEventStore
from perpleximanus.retrieval.deep_research import (
    DeepResearchRun,
    DepthTier,
    decompose_query,
)
from perpleximanus.retrieval.wiring import retrieval_capability_handlers
from perpleximanus.tools import (
    Capability,
    CapabilityBroker,
    DefaultToolExecutor,
    ProcessSandboxService,
    SandboxService,
    SandboxSession,
    SandboxSpec,
    agent_scope,
    build_default_registry,
)
from perpleximanus.tools.projects import (
    ProjectStore,
    StorageStatus,
    rehydrate_workspace,
    snapshot_workspace,
)
from perpleximanus.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    SandboxConfig,
)
from perpleximanus.tools.sandbox._container import PREVIEW_PORT


def build_sandbox_service(settings: SandboxSettings) -> SandboxService:
    """Map the persisted SandboxSettings → the concrete SandboxBackend — the ONE place
    that knows the backend↔config mapping (settings drive the active backend). Podman is
    real, verified backend code, but a STUB in THIS environment (VM 202 destroyed) — it
    constructs but isn't live/verifiable here; completed at the Meta deployment."""
    cfg = SandboxConfig(
        backend=settings.backend,
        docker_socket=settings.docker_socket,
        podman_url=settings.podman_url,
        runtime=settings.runtime,
        image=settings.image,
        workspace_root=settings.workspace_root,
        # the host previews are reachable at — set PMX_PREVIEW_HOST to a LAN/tailnet IP so
        # previews work from other devices, not just the agent-server's host (else derived).
        preview_host=os.environ.get("PMX_PREVIEW_HOST", ""),
    )
    if settings.backend == "gvisor":
        return GvisorSandboxService(cfg)
    if settings.backend == "local":
        return LocalSandboxService(cfg)
    if settings.backend == "podman":
        return PodmanSandboxService(cfg)  # stub-in-this-env (see docstring)
    return ProcessSandboxService()  # "process"/unknown → the dev backend (runs on host)


def _model_label(model_id: str) -> str:
    """A short human label from a model_id (drops the gguf/quant noise + provider path)."""
    base = model_id.split("/")[-1].removesuffix(".gguf")
    for suffix in ("-UD-Q5_K_XL", "-UD-Q4_K_XL", "-Q5_K_M", "-Q4_K_M", "-Q4_K_S", "-IQ4_XS"):
        base = base.replace(suffix, "")
    return base


class _NoToolExecutor:
    """A read-only surface with no tools: the model answers directly. Any tool the
    model hallucinates fails loudly as an observation (it has none to call)."""

    def available_tools(self) -> list:
        return []

    async def execute(self, call) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content="",
            error="no tools are available on this surface",
        )


class ConversationRuntime:
    """Builds a router from the CURRENT persisted config per request, plus one
    AgentLoop per conversation. `kick(cid)` schedules the loop in the background.

    Model assignments live in a shared ConfigStore (PMX_CONFIG) that the Settings
    UI writes; `_router_now()` reloads it each request, so reassigning a role takes
    effect on the next research call / new conversation without a restart."""

    def __init__(
        self,
        store: SqliteEventStore,
        *,
        config: RouterConfig | None = None,
        config_store: ConfigStore | None = None,
        secret_store: SecretStore | None = None,
        router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
        research_providers: dict[str, Any] | None = None,
        sandbox_service: SandboxService | None = None,
        sandbox_spec: SandboxSpec | None = None,
    ) -> None:
        self._store = store
        # The Build surface runs tools through a SandboxBackend. An explicitly injected
        # service is an OVERRIDE (tests / `PMX_SANDBOX` at startup); otherwise the backend
        # is read PER-REQUEST from the persisted SandboxSettings (the Settings selector),
        # mirroring how `_router_now()` reloads model assignments — so a settings change
        # drives the next conversation's sandbox without a restart.
        self._injected_sandbox = sandbox_service
        self._sandbox_spec = sandbox_spec or SandboxSpec()
        # Per-conversation surface ("research" | "build"); set at create time. Kept in
        # memory (no schema migration) — a server restart resets a conversation to the
        # research default until re-selected. The Build executor + its broker are held
        # so the kill switch can revoke them.
        self._surface: dict[str, str] = {}
        self._executors: dict[str, DefaultToolExecutor] = {}
        self._cap_handlers: dict[str, Any] | None = None
        # per-conversation driver model override (the Build chat model picker → the
        # AGENT_DRIVER for that conversation; RouterAgent applies it).
        self._model_override: dict[str, str] = {}
        # per-conversation Deep Research depth tier (set at submit time).
        self._depth: dict[str, str] = {}
        # The encrypted-at-rest secret store (OpenRouter key). Its decrypted key is
        # overlaid into the provider env per request; if it's locked/empty the env
        # value (if any) is used instead.
        self._secret_store = secret_store or SecretStore()
        # The live retrieval/grounding providers for research_stream(). Injected
        # in tests (hermetic fakes); else lazily built from env on first use so
        # importing the runtime doesn't pull httpx until research is actually run.
        self._research_providers = research_providers
        # A statically-injected router (test seam) pins routing; otherwise the
        # router is rebuilt per request from the shared, persisted config store so
        # Settings assignments are actually honored.
        self._injected_router = router
        self._enable_thinking = enable_thinking
        if config_store is not None:
            self._config_store = config_store
        elif config is not None:
            self._config_store = ConfigStore(base_factory=lambda: config)
        else:
            self._config_store = ConfigStore()
        self._mode = mode
        self._loops: dict[str, AgentLoop] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _router_now(
        self, answerer_override: str | None = None, *, enable_thinking: bool | None = None
    ) -> DefaultLLMRouter:
        """The router for the CURRENT assignments. Cheap to rebuild (providers are
        plain objects; the HTTP client is created per call), so we reload the config
        each request rather than cache a stale router. `answerer_override` (the
        research pill) reassigns RAG_ANSWERER for this request only; `enable_thinking`
        overrides the default reasoning mode for this request (the Think toggle)."""
        if self._injected_router is not None:
            return self._injected_router
        cfg = self._config_store.load()
        # The research model pill picks the ANSWERER (not a chat driver), so apply
        # it by reassigning RAG_ANSWERER for this request — the assignment path the
        # router honors for that role — rather than the driver-only CallContext
        # override. Unknown keys are ignored (fail safe to the saved assignment).
        if answerer_override and answerer_override in cfg.models:
            cfg = cfg.model_copy(
                update={
                    "assignments": {**cfg.assignments, ModelRole.RAG_ANSWERER: answerer_override}
                }
            )
        # Overlay the decrypted OpenRouter key into the env build_providers reads,
        # so OR models (api_key_env=PMX_OPENROUTER_API_KEY) authenticate without the
        # secret ever being on disk in plaintext.
        env = dict(os.environ)
        or_key = self._secret_store.get_openrouter_key()
        if or_key:
            env[OPENROUTER_API_KEY_ENV] = or_key
        thinking = self._enable_thinking if enable_thinking is None else enable_thinking
        providers = build_providers(cfg, env=env, enable_thinking=thinking)
        # DriverPrompts gives the AGENT_DRIVER role phase-aware system prompts (the
        # plan→approve→build flow); every other role/mode defers to the default
        # provider, so Research is unaffected.
        return DefaultLLMRouter(cfg, providers, prompt_provider=DriverPrompts())

    # Three surfaces: research (single-pass /ws/research stream), build (agent +
    # tools + plan gate), deep_research (long-horizon plan → iterate → report).
    _VALID_SURFACES: frozenset[str] = frozenset({"research", "build", "deep_research"})

    def set_surface(self, conversation_id: str, surface: str) -> None:
        """Select a conversation's surface before it runs. Build composes tools +
        sandbox + the ConfirmRisky gate; Deep Research composes the plan-gate +
        the long-horizon engine; Research stays read-only + ungated. Idempotent
        until the loop is built."""
        self._surface[conversation_id] = (
            surface if surface in self._VALID_SURFACES else "research"
        )

    def _surface_of(self, conversation_id: str) -> str:
        """The conversation's surface, recovered durably across server restarts.
        In-memory `_surface` is authoritative when set (the create-time POST path
        sets it). When it's missing — typical after a server restart — we DERIVE
        from durable signals on disk / on the event log:
        - a project manifest on disk → Build (Research never snapshots).
        - a ReportEvent on the conversation log → Deep Research.
        Defaults to "research" when no durable signal exists."""
        cached = self._surface.get(conversation_id)
        if cached is not None:
            return cached
        store = self._project_store_now()
        if store is not None and store.status() == StorageStatus.OK:
            try:
                if store.get(conversation_id) is not None:
                    # cache the recovery so subsequent lookups don't re-stat the FS
                    self._surface[conversation_id] = "build"
                    return "build"
            except Exception:  # noqa: BLE001 — best-effort recovery
                pass
        # Deep Research recovery: a ReportEvent on the log is the durable marker
        # (Research/Build never emit ReportEvent). Cheap SQL check — no need to
        # deserialize the whole log just to read the discriminator column.
        try:
            conn = getattr(self._store, "_conn", None)
            if conn is not None:
                kinds = {
                    row["kind"]
                    for row in conn.execute(
                        "SELECT DISTINCT kind FROM events WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                }
                if "report" in kinds:
                    self._surface[conversation_id] = "deep_research"
                    return "deep_research"
                # plan-event without any action-event marks a deep-research paused
                # at its plan gate (Build that survived a restart would also have
                # an on-disk project manifest, caught above).
                if "plan" in kinds and "action" not in kinds:
                    self._surface[conversation_id] = "deep_research"
                    return "deep_research"
        except Exception:  # noqa: BLE001 — best-effort recovery
            pass
        return "research"

    def _sandbox_service_now(self) -> SandboxService:
        """The active sandbox backend: the injected override if present, else built from
        the persisted SandboxSettings (reloaded each time — the Settings selector drives it)."""
        if self._injected_sandbox is not None:
            return self._injected_sandbox
        return build_sandbox_service(self._config_store.load().sandbox)

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None:
        """Pin the driver model for a conversation (the Build chat model picker). The id
        is a catalogue KEY; RouterAgent reassigns AGENT_DRIVER to it. Must be set before
        the loop is built (at create time)."""
        if model_id:
            self._model_override[conversation_id] = model_id

    def driver_models(self) -> dict[str, Any]:
        """The driver-eligible models (live + tool-calling), deduped by underlying model,
        for the Build model picker — with the current default. Cost-legible: provider +
        free flag. Sourced from the live router config (Settings assignments honored)."""
        from perpleximanus.core.llm import ModelRole, Requirement

        cfg = self._config_store.load()
        seen: set[str] = set()
        models: list[dict[str, Any]] = []
        for key, m in cfg.models.items():
            if m.base_url is None or Requirement.TOOL_CALLING not in m.capabilities:
                continue
            if m.model_id in seen:
                continue
            seen.add(m.model_id)
            models.append(
                {
                    "id": key,
                    "label": _model_label(m.model_id),
                    "provider": "openrouter" if m.provider == "openrouter" else "local",
                    "free": m.price_out_per_m == 0.0,
                    "context_window": m.context_window,
                }
            )
        try:
            default = cfg.model_for(ModelRole.AGENT_DRIVER)
        except Exception:  # noqa: BLE001 — no assignment → no default highlight
            default = None
        return {"models": models, "default": default}

    def _retrieval_handlers(self) -> dict[str, Any]:
        """Lazily build the search/extract capability handlers from the research
        providers — so the Build agent toolset's search/extract are REAL, not a false
        affordance, without eagerly importing httpx."""
        if self._cap_handlers is None:
            deps = self._research()
            self._cap_handlers = retrieval_capability_handlers(deps["search"], deps["extraction"])
        return self._cap_handlers

    def _build_broker(self) -> CapabilityBroker:
        """The orchestrator-side capability broker for a Build conversation. search/
        extract are backed by the research providers (lazy); provider keys never reach
        the sandbox (§6). The kill switch calls broker.revoke_all()."""
        broker = CapabilityBroker()

        async def _search(*, query: str, limit: int = 8) -> Any:
            return await self._retrieval_handlers()["search"](query=query, limit=limit)

        async def _extract(*, url: str) -> Any:
            return await self._retrieval_handlers()["extract"](url=url)

        broker.register("search", _search)
        broker.register("extract", _extract)
        return broker

    def _loop_for(self, conversation_id: str) -> AgentLoop:
        loop = self._loops.get(conversation_id)
        if loop is None:
            # A conversation pins the assignments it started with (consistency);
            # new conversations pick up later reassignments via _router_now().
            router = self._router_now()
            # the model picker pins the driver model for this conversation (AGENT_DRIVER).
            agent = RouterAgent(
                router,
                conversation_id=conversation_id,
                model_override=self._model_override.get(conversation_id),
            )
            surface = self._surface_of(conversation_id)
            if surface == "build":
                loop = self._compose_build_loop(conversation_id, router, agent)
            elif surface == "deep_research":
                loop = self._compose_deep_research_loop(conversation_id, router, agent)
            else:
                loop = AgentLoop(
                    conversation_id,
                    self._store,
                    agent,
                    _NoToolExecutor(),
                    router,
                    RuleBasedAnalyzer(),
                    NeverConfirm(),  # Research surface: no human gate
                    NoOpCondenser(),
                    RouterSummarizer(router),
                    mode=self._mode,
                )
            self._loops[conversation_id] = loop
        return loop

    def _compose_build_loop(
        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
    ) -> AgentLoop:
        """[Agent surface] Compose — not reinvent — the loop for Build mode: the agent
        toolset (Prompt 1) over a resilient SandboxSession, the SecurityAnalyzer, the
        ConfirmRisky gate (NOT Research's NeverConfirm), and the real condenser. The
        executor is held so the kill switch can revoke caps + tear down the sandbox."""
        broker = self._build_broker()
        # The Agent toolset includes the `browser`, which needs egress — so the Build
        # sandbox GRANTS network (the cost-legible coupling: selecting Build is visible as
        # network-granting; the gate + analyzer, not the seal, control risky egress here).
        build_spec = self._sandbox_spec.model_copy(
            update={"permitted": self._sandbox_spec.permitted | {Capability.NETWORK}}
        )
        session = SandboxSession(
            self._sandbox_service_now(), build_spec, conversation_id=conversation_id
        )
        executor = DefaultToolExecutor(
            build_default_registry(),
            agent_scope(),
            sandbox=session,
            broker=broker,
            conversation_id=conversation_id,
        )
        self._executors[conversation_id] = executor
        return AgentLoop(
            conversation_id,
            self._store,
            agent,
            executor,
            router,
            RuleBasedAnalyzer(),
            ConfirmRisky(),  # Agent surface: gate risky/UNKNOWN actions before they run
            LLMSummarizingCondenser(),  # real condensation, not the no-op
            RouterSummarizer(router),
            # Build starts in PLANNING. The planner has a context-rich surface — it
            # can READ to explore (file_list/file_read in the workspace, search/extract
            # on the web) before calling `submit_plan`. This mirrors Claude Code's plan
            # mode: writes/edits/shell are off the table until approval, but the
            # planner can gather context first. approve_plan flips to execution; the
            # per-action gate above still governs the build that follows.
            mode=OperatingMode.PLANNING,
            planning_tools=frozenset(
                {"submit_plan", "file_list", "file_read", "search", "extract"}
            ),
        )

    # ---- deep research surface ---------------------------------------------

    def _compose_deep_research_loop(
        self, conversation_id: str, router: DefaultLLMRouter, agent: RouterAgent
    ) -> AgentLoop:
        """Compose the loop frame for Deep Research. The loop itself doesn't drive
        the research — `_run_with_persistence` short-circuits `loop.run()` for
        this surface and runs `DeepResearchRun` directly. The AgentLoop exists
        here as the host for the plan-approval gate (we reuse Build's plan
        machinery: same PlanEvent, same AWAITING_PLAN_APPROVAL status, same
        approve_plan WS frame). No tools, no risk gate, no condenser — Deep
        Research's iteration lives in the engine, not the loop."""
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
        )

    def _depth_for(self, conversation_id: str) -> DepthTier:
        """The depth tier for this conversation. Stored in `_depth` per cid (set
        at create-time by the agent-server's POST /conversations handler).
        Defaults to STANDARD_DEEP — the everyday Deep Research run."""
        raw = self._depth.get(conversation_id) if hasattr(self, "_depth") else None
        if raw is None:
            return DepthTier.STANDARD_DEEP
        try:
            return DepthTier(raw)
        except ValueError:
            return DepthTier.STANDARD_DEEP

    def set_depth(self, conversation_id: str, tier: str | None) -> None:
        """Pin the Deep Research depth tier for this conversation (set at submit
        time by the UI's tier selector). Stored in memory; recovery falls to the
        default after restart, which is acceptable for a follow-up turn (rare
        for Deep Research — the run is the conversation)."""
        if not hasattr(self, "_depth"):
            self._depth: dict[str, str] = {}
        if tier and tier in {t.value for t in DepthTier}:
            self._depth[conversation_id] = tier

    # ---- research surface (Stage 4) -----------------------------------------

    def _research(self) -> dict[str, Any]:
        if self._research_providers is None:
            # Lazy import: the live providers pull httpx; only do so on first use.
            from perpleximanus.retrieval.live import build_live_retrieval

            self._research_providers = build_live_retrieval()
        return self._research_providers

    def research_stream(
        self,
        query: str,
        *,
        model_override: str | None = None,
        drop_weak: bool = False,
        domains_deny: frozenset[str] = frozenset(),
        think: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a live grounded answer as the UI's research frames (state →
        token… → final). Composes the shared router with the live retrieval
        providers, honoring the re-scope controls: the pill picks the answerer
        model, `domains_deny` filters discovery, `drop_weak` prunes the answer, and
        `think` runs the answerer in reasoning mode (it thinks, then the answer
        streams; the reasoning is never emitted as answer tokens)."""
        from perpleximanus.retrieval.streaming import stream_research_answer

        deps = self._research()
        return stream_research_answer(
            query,
            router=self._router_now(answerer_override=model_override, enable_thinking=think),
            search=deps["search"],
            extraction=deps["extraction"],
            reranker=deps["reranker"],
            nli=deps["nli"],
            domains_deny=domains_deny,
            drop_weak=drop_weak,
            think=think,
        )

    def kick(self, conversation_id: str) -> None:
        """Schedule the loop to run (idempotent: a no-op if already running). The
        loop itself decides if there's unprocessed work and streams events.

        Wraps `loop.run()` in a task that — for Build conversations with a valid
        ProjectStore configured — first REHYDRATES the persisted workspace on
        the very first kick, and SNAPSHOTS it after every run that ends in
        FINISHED. Failures are reported as ambient system-reminder events on
        the conversation log (the user + model both see them), never as
        exceptions that crash the task."""
        existing = self._tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return  # already running; the new message is picked up at the next step
        loop = self._loop_for(conversation_id)
        self._tasks[conversation_id] = asyncio.create_task(
            self._run_with_persistence(conversation_id, loop)
        )

    async def _run_with_persistence(
        self, conversation_id: str, loop: AgentLoop
    ) -> Any:
        """Snapshot/rehydrate wrapper around `loop.run()`. Surface-aware:
        - Build → snapshot+rehydrate the workspace as before.
        - Deep Research → short-circuit `loop.run()` on the post-plan-approval
          turn and run the DeepResearchRun engine; emit the ReportEvent +
          StatusEvent(FINISHED) directly.
        - Research → unchanged (the loop runs, no persistence)."""
        surface = self._surface_of(conversation_id)

        if surface == "deep_research":
            # Short-circuit the loop for Deep Research. The plan-mode intercept
            # the loop would otherwise run isn't useful here — Deep Research
            # decomposes the query algorithmically (not via an LLM planning
            # round) and the engine handles the rest. The loop's plan-approval
            # state machine is reused via control-op routing; the loop's
            # `run()` itself is not the right driver.
            await self._maybe_run_deep_research(conversation_id)
            # Return the (possibly-updated) state. The store reflects whatever
            # we emitted.
            return await self._store.get_state(conversation_id)

        # Rehydrate hook: BEFORE the loop runs for the first time, if a project
        # storage path is configured AND a prior snapshot exists for this cid,
        # write its files into the (lazy) sandbox so the agent sees them. Reads
        # are cheap; the rehydrate only writes if there are files on disk.
        if surface == "build":
            await self._maybe_rehydrate(conversation_id)

        state = await loop.run()

        # Snapshot hook: if the run ended in FINISHED, capture the workspace.
        if (
            surface == "build"
            and state.execution_status == ConversationStatus.FINISHED
        ):
            await self._maybe_snapshot(conversation_id)
        return state

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

        # Phase 2: plan approved, no report yet → run the engine
        if (
            state.execution_status == ConversationStatus.RUNNING
            and not reports
        ):
            # Distinguish "RUNNING because plan was just approved" from "RUNNING
            # because we're already deep in the engine and the task re-fired."
            # The marker: the last StatusEvent's detail is "plan_approved".
            last_status = next(
                (e for e in reversed(events) if isinstance(e, StatusEvent)), None
            )
            if last_status is not None and last_status.detail == "plan_approved":
                await self._execute_deep_research(conversation_id, plans[-1])
        # else: waiting at AWAITING_PLAN_APPROVAL, FINISHED, etc. — no-op.

    async def _propose_deep_research_plan(
        self, conversation_id: str, events: list
    ) -> None:
        """Decompose the latest user query into sub-questions and emit a
        synthetic PlanEvent + AWAITING_PLAN_APPROVAL. Same shape Build's plan
        gate uses — the UI reuses the existing approve_plan / request_plan
        affordances without modification."""
        # Find the most recent USER message — the query.
        query = next(
            (
                e.message.content
                for e in reversed(events)
                if isinstance(e, MessageEvent) and e.source == EventSource.USER
            ),
            None,
        )
        if not query or not query.strip():
            return  # nothing to plan; wait

        await self._store.append(
            conversation_id,
            StatusEvent(status=ConversationStatus.RUNNING),
        )
        tier = self._depth_for(conversation_id)
        from perpleximanus.retrieval.deep_research import bounds_for

        bound = bounds_for(tier)
        # Decompose via QUERY_REWRITER. The decompose call IS the planning
        # step; we emit the result as a PlanEvent directly (no LLM "planning
        # mode" loop needed — the engine owns the work).
        router = self._router_now()
        try:
            subqs = await decompose_query(
                router, query, max_subq=bound.max_subquestions
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
            revision=1,
            context=(
                f"**Query:** {query.strip()}\n\n"
                f"**Depth tier:** {tier.value}\n\n"
                f"**Sub-questions** (each becomes a section of the report):\n\n"
                + "\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(subqs))
            ),
        )
        await self._store.append(conversation_id, plan)
        await self._store.append(
            conversation_id,
            StatusEvent(
                status=ConversationStatus.AWAITING_PLAN_APPROVAL, detail=plan.id
            ),
        )

    async def _execute_deep_research(
        self, conversation_id: str, plan: PlanEvent
    ) -> None:
        """The post-approval driver: run DeepResearchRun on the approved plan,
        emitting Action/Observation events for every retrieval round + section
        synthesis, ending with a ReportEvent + StatusEvent(FINISHED)."""
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

        # Build the engine. Providers come from the existing research-stream
        # plumbing (search, extract, reranker, embedder, nli); the vectorstore
        # is per-run (InMemoryVectorStore). The router honors model_override
        # from the leader pill (the existing _router_now path).
        from perpleximanus.retrieval import (
            DefaultRetrievalEngine,
            InMemoryVectorStore,
            RouterQueryRewriter,
        )

        deps = self._research()
        router = self._router_now(
            answerer_override=self._model_override.get(conversation_id)
        )
        retrieval_engine = DefaultRetrievalEngine(
            search=deps["search"],
            extraction=deps["extraction"],
            reranker=deps["reranker"],
            embedder=deps.get("embedder"),
            rewriter=RouterQueryRewriter(router),
        )
        run = DeepResearchRun(
            query=query,
            router=router,
            retrieval_engine=retrieval_engine,
            embedder=deps.get("embedder"),
            vector_store=InMemoryVectorStore(),
            nli=deps["nli"],
            depth=tier,
            conversation_id=conversation_id,
        )

        # Emit callback: every engine event becomes an Action/Observation pair
        # on the conversation log so the UI's activity feed reflects progress.
        async def emit(kind: str, payload: dict[str, Any]) -> None:
            # Treat phase + search + synthesize_section as actions (the agent
            # "doing something"), observation kinds as observations (results).
            if kind == "observation" or kind == "gap_reason":
                # last action_id we appended (best-effort correlation)
                last_action = next(
                    (
                        e
                        for e in reversed(await self._store.get_events(conversation_id))
                        if isinstance(e, ActionEvent)
                    ),
                    None,
                )
                await self._store.append(
                    conversation_id,
                    ObservationEvent(
                        tool_result=ToolResult(
                            call_id=f"call_{kind}",
                            tool_name=kind,
                            success=bool(payload.get("ok", True)),
                            content=str(
                                {k: v for k, v in payload.items() if k != "ok"}
                            ),
                            structured=payload,
                        ),
                        action_id=last_action.id if last_action else "",
                    ),
                )
                return
            # action-shaped events
            await self._store.append(
                conversation_id,
                ActionEvent(
                    thought=f"Deep Research: {kind}",
                    tool_call=ToolCall(tool_name=kind, arguments=payload),
                ),
            )

        try:
            result = await run.run(plan_steps, emit=emit)
        except Exception as exc:  # noqa: BLE001 — surface as ErrorEvent
            from perpleximanus.core import ErrorEvent

            await self._store.append(
                conversation_id,
                ErrorEvent(
                    code="deep_research_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            )
            return

        # Emit the final ReportEvent + FINISHED status.
        await self._store.append(conversation_id, result.to_event())
        await self._store.append(
            conversation_id, StatusEvent(status=ConversationStatus.FINISHED)
        )

    def project_store(self) -> ProjectStore | None:
        """Public accessor for the live project store (used by the agent-server's
        projects endpoints). Returns None if no path is configured; the caller
        checks `.status()` for the full validation classification."""
        return self._project_store_now()

    def _project_store_now(self) -> ProjectStore | None:
        """Build a ProjectStore from the current settings — None if no path is
        configured. Built per-call (cheap; matches the rest of the runtime's
        "reload the config each request" discipline). The validity status is
        checked at the use site so a bad-but-set path can be reported clearly."""
        root = self._config_store.load().projects.projects_root
        if not root.strip():
            return None
        return ProjectStore(root)

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        """Restore the workspace files from a prior snapshot into the live
        sandbox, if a snapshot exists. Idempotent: tracked via an in-process
        flag so a second kick on the same conversation doesn't re-write the
        files."""
        if getattr(self, "_rehydrated", None) is None:
            self._rehydrated: set[str] = set()
        if conversation_id in self._rehydrated:
            return
        self._rehydrated.add(conversation_id)
        store = self._project_store_now()
        if store is None or store.status() != StorageStatus.OK:
            return
        record = None
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 — manifest unreadable: treat as no record
            return
        if record is None or record.files_missing:
            return
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        try:
            await rehydrate_workspace(session, store.path_for(conversation_id))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            await self._emit_persistence_reminder(
                conversation_id,
                f"Could not restore project files: {exc}",
            )

    async def _maybe_snapshot(self, conversation_id: str) -> None:
        """Mirror the live workspace out to disk + update the manifest."""
        store = self._project_store_now()
        if store is None:
            return
        status = store.status()
        if status != StorageStatus.OK:
            await self._emit_persistence_reminder(
                conversation_id,
                f"project storage is {status.value}; this build was NOT saved.",
            )
            return
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        # Title pulled from the conversations table; created_at is the row's
        # creation timestamp. Both are cheap reads we surface in the list view.
        title: str | None = None
        created_at: str | None = None
        owner_id: str | None = None
        try:
            summaries = await self._store.list_conversation_summaries(
                owner_id=DEFAULT_OWNER_ID, limit=500, cursor=None
            )
            row = next((s for s in summaries if s.conversation_id == conversation_id), None)
            if row is not None:
                title = row.title
                created_at = row.created_at
                owner_id = row.owner_id
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass
        try:
            result = await snapshot_workspace(session, store.path_for(conversation_id))
            store.write_manifest(
                conversation_id,
                title=title,
                owner_id=owner_id,
                created_at=created_at,
                file_count=result.file_count,
                total_bytes=result.total_bytes,
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            await self._emit_persistence_reminder(
                conversation_id,
                f"snapshot failed: {exc}",
            )

    async def _emit_persistence_reminder(self, conversation_id: str, body: str) -> None:
        """Surface a project-persistence problem on the event log as an implicit
        system-reminder — same pattern as the loop's other gates, so the model
        and the UI both see what went wrong, named."""
        await self._store.append(
            conversation_id,
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        f"Project persistence note: {body}\n"
                        "</system-reminder>"
                    ),
                ),
            ),
        )

    def preview_upstream(self, conversation_id: str) -> str | None:
        """The URL the AGENT-SERVER can reach the conversation's dev server at (the backend
        owns how — localhost for local, the remote host's tailnet IP for gVisor). The
        browser never touches this; the agent-server proxies it (single origin)."""
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return None
        if getattr(getattr(session, "_service", None), "name", "?") == "podman":
            return None  # stub here
        return session.expose_port(PREVIEW_PORT)

    def preview(self, conversation_id: str) -> dict[str, Any]:
        """Backend-aware live preview availability. The browser iframes the agent-server's
        proxy (/conversations/{id}/preview-app/), which forwards to the active backend's
        dev server — so previews work over the tailnet via the one reachable origin, with no
        random container ports exposed. Podman is an honest labeled stub; never a fake URL."""
        executor = self._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return {"available": False, "reason": "The agent hasn't started a sandbox yet."}
        if getattr(getattr(session, "_service", None), "name", "?") == "podman":
            return {
                "available": False,
                "stub": True,
                "reason": "Preview isn't wired for the Podman backend in this environment "
                "— it's completed at deployment.",
            }
        if session.expose_port(PREVIEW_PORT) is None:
            return {
                "available": False,
                "reason": f"No dev server detected. Run one on port {PREVIEW_PORT} inside the "
                "sandbox to see a live preview.",
            }
        # the browser iframes the agent-server proxy path (built frontend-side from the
        # agent base + this conversation id) — not a raw container port.
        return {"available": True, "proxy": True}

    # ---- control ops: the confirmation gate + kill switch (BoD §13.4/§13.6) -----

    async def confirm(self, conversation_id: str) -> None:
        """Approve the pending action: execute EXACTLY it (the loop's `confirm`), then
        resume the loop for the next steps."""
        loop = self._loops.get(conversation_id)
        if loop is not None:
            await loop.confirm()
            self.kick(conversation_id)  # continue plan→act→observe past the gate

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """Deny the pending action: record the denial (no execution), then resume."""
        loop = self._loops.get(conversation_id)
        if loop is not None:
            await loop.reject(reason)
            self.kick(conversation_id)

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan: flip the loop into execution mode (full tools)
        and run it. The per-action ConfirmRisky gate still governs the build."""
        loop = self._loops.get(conversation_id)
        if loop is not None:
            await loop.approve_plan()
            self.kick(conversation_id)  # start building the approved plan

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        """(Re-)enter plan mode with the user's instruction — the first plan AND the
        re-plan after a build, so focused diff-style changes are planned and re-approved
        instead of free-form steered."""
        loop = self._loops.get(conversation_id)
        if loop is not None:
            await loop.enter_planning(text)
            self.kick(conversation_id)  # produce the (revised) plan

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop (distinct from the hard kill): the loop winds down."""
        loop = self._loops.get(conversation_id)
        if loop is not None:
            await loop.cancel()

    async def kill(self, conversation_id: str) -> None:
        """The KILL SWITCH (BoD §13.6) — the ultimate stop above the three security
        layers. Halts a RUNNING loop promptly (cancel the task mid-step), revokes the
        agent's capabilities + tears down the sandbox session (executor.kill), and
        records a terminal status so the UI reflects the stop."""
        # 1. stop the running loop task promptly — do NOT wait for the current step.
        task = self._tasks.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # 2. revoke capabilities + destroy the sandbox (the executor's kill, §6.4).
        executor = self._executors.get(conversation_id)
        if executor is not None:
            await executor.kill()
        # 3. record the stop so subscribers see it (no STOPPED status in the enum; IDLE
        #    + a 'killed' detail is the contract's terminal-for-now shape).
        await self._store.append(
            conversation_id,
            StatusEvent(
                source=EventSource.SYSTEM, status=ConversationStatus.IDLE, detail="killed"
            ),
        )

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for executor in self._executors.values():
            with contextlib.suppress(Exception):
                await executor.kill()
