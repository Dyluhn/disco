"""The conversation runtime — runs the agent loop with real inference (Stage 2).

Phase 0 only appended events. This wires `AgentLoop` (core) + a real Qwen-backed
router into the request path: a user message KICKS the loop, which runs in the
background, calls the model via the OpenAI adapter, and appends events — which the
store already publishes to the WebSocket the UI consumes (history-then-live).

The Research surface defaults: `NeverConfirm` (no human gate) + no tools (the
model answers directly). Tools (search/extract) + the grounded research pipeline
arrive in Stages 3–4; the Build surface's `ConfirmRisky` stays dormant.
"""

from __future__ import annotations

import asyncio

from perpleximanus.core import NoOpCondenser, ToolResult
from perpleximanus.core.llm import DefaultLLMRouter, OperatingMode, RouterSummarizer, default_config
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
    """Owns the shared router (built once) and one AgentLoop per conversation.
    `kick(cid)` schedules the loop to run in the background if it isn't already."""

    def __init__(
        self,
        store: SqliteEventStore,
        *,
        config: RouterConfig | None = None,
        router: DefaultLLMRouter | None = None,
        enable_thinking: bool = False,
        mode: OperatingMode = OperatingMode.INTERACTIVE,
    ) -> None:
        self._store = store
        if router is not None:
            self._router = router  # injected (tests use a fake-backed router)
        else:
            cfg = config or default_config()
            # Live providers (one OpenAIProvider per endpoint). enable_thinking=False
            # → Qwen answers directly (faster, streams immediately) rather than
            # reasoning first; flip per-surface later if chain-of-thought is wanted.
            providers = build_providers(cfg, enable_thinking=enable_thinking)
            self._router = DefaultLLMRouter(cfg, providers)
        self._mode = mode
        self._loops: dict[str, AgentLoop] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def _loop_for(self, conversation_id: str) -> AgentLoop:
        loop = self._loops.get(conversation_id)
        if loop is None:
            agent = RouterAgent(self._router, conversation_id=conversation_id)
            loop = AgentLoop(
                conversation_id,
                self._store,
                agent,
                _NoToolExecutor(),
                self._router,
                RuleBasedAnalyzer(),
                NeverConfirm(),  # Research surface: no human gate
                NoOpCondenser(),
                RouterSummarizer(self._router),
                mode=self._mode,
            )
            self._loops[conversation_id] = loop
        return loop

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
