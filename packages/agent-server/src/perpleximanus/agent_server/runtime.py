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
from collections.abc import AsyncIterator
from typing import Any

from perpleximanus.core import NoOpCondenser, ToolResult
from perpleximanus.core.llm import (
    ConfigStore,
    DefaultLLMRouter,
    OperatingMode,
    RouterSummarizer,
)
from perpleximanus.core.llm.config import RouterConfig
from perpleximanus.core.llm.wiring import build_providers
from perpleximanus.core.loop import AgentLoop, NeverConfirm, RouterAgent
from perpleximanus.core.security import RuleBasedAnalyzer
from perpleximanus.core.store.sqlite import SqliteEventStore


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
        router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
        research_providers: dict[str, Any] | None = None,
    ) -> None:
        self._store = store
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

    def _router_now(self) -> DefaultLLMRouter:
        """The router for the CURRENT assignments. Cheap to rebuild (providers are
        plain objects; the HTTP client is created per call), so we reload the config
        each request rather than cache a stale router."""
        if self._injected_router is not None:
            return self._injected_router
        cfg = self._config_store.load()
        providers = build_providers(cfg, enable_thinking=self._enable_thinking)
        return DefaultLLMRouter(cfg, providers)

    def _loop_for(self, conversation_id: str) -> AgentLoop:
        loop = self._loops.get(conversation_id)
        if loop is None:
            # A conversation pins the assignments it started with (consistency);
            # new conversations pick up later reassignments via _router_now().
            router = self._router_now()
            agent = RouterAgent(router, conversation_id=conversation_id)
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

    # ---- research surface (Stage 4) -----------------------------------------

    def _research(self) -> dict[str, Any]:
        if self._research_providers is None:
            # Lazy import: the live providers pull httpx; only do so on first use.
            from perpleximanus.retrieval.live import build_live_retrieval

            self._research_providers = build_live_retrieval()
        return self._research_providers

    def research_stream(self, query: str) -> AsyncIterator[dict[str, Any]]:
        """Stream a live grounded answer as the UI's research frames (state →
        token… → final). Composes the shared router with the live retrieval
        providers; this is the Stage-4 pipeline, not the AgentLoop."""
        from perpleximanus.retrieval.streaming import stream_research_answer

        deps = self._research()
        return stream_research_answer(
            query,
            router=self._router_now(),  # honor the CURRENT model assignments
            search=deps["search"],
            extraction=deps["extraction"],
            reranker=deps["reranker"],
            nli=deps["nli"],
        )

    def kick(self, conversation_id: str) -> None:
        """Schedule the loop to run (idempotent: a no-op if already running). The
        loop itself decides if there's unprocessed work and streams events."""
        existing = self._tasks.get(conversation_id)
        if existing is not None and not existing.done():
            return  # already running; the new message is picked up at the next step
        loop = self._loop_for(conversation_id)
        self._tasks[conversation_id] = asyncio.create_task(loop.run())

    async def aclose(self) -> None:
        for task in self._tasks.values():
            task.cancel()
