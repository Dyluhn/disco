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
    ConversationStatus,
    EventSource,
    LLMSummarizingCondenser,
    NoOpCondenser,
    StatusEvent,
    ToolResult,
)
from perpleximanus.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    ModelRole,
    OperatingMode,
    RouterSummarizer,
    SecretStore,
)
from perpleximanus.core.llm.config import RouterConfig
from perpleximanus.core.llm.secrets import OPENROUTER_API_KEY_ENV
from perpleximanus.core.llm.wiring import build_providers
from perpleximanus.core.loop import AgentLoop, ConfirmRisky, NeverConfirm, RouterAgent
from perpleximanus.core.security import RuleBasedAnalyzer
from perpleximanus.core.store.sqlite import SqliteEventStore
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
        # The Build surface runs tools through this backend (gVisor/Podman/local —
        # the proven SandboxBackends). Injectable; defaults to the dev `process`
        # backend (tool-sandbox §5) so a fresh checkout runs without a container host.
        # PRODUCTION should inject a real isolating backend.
        self._sandbox_service = sandbox_service or ProcessSandboxService()
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
        return DefaultLLMRouter(cfg, providers)

    def set_surface(self, conversation_id: str, surface: str) -> None:
        """Select a conversation's surface ("research" | "build") before it runs. The
        Build surface composes tools + sandbox + the ConfirmRisky gate; Research stays
        read-only + ungated. Idempotent until the loop is built."""
        self._surface[conversation_id] = "build" if surface == "build" else "research"

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
            if self._surface.get(conversation_id) == "build":
                loop = self._compose_build_loop(conversation_id, router, agent)
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
            self._sandbox_service, build_spec, conversation_id=conversation_id
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
            mode=self._mode,
        )

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
        loop itself decides if there's unprocessed work and streams events."""
        existing = self._tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return  # already running; the new message is picked up at the next step
        loop = self._loop_for(conversation_id)
        self._tasks[conversation_id] = asyncio.create_task(loop.run())

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
