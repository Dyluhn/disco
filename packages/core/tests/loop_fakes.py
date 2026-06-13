"""Fakes + builders for the agent-loop tests (agent-loop-contract.md §10).

Everything the loop depends on is faked — no real model, no sandbox — which is
exactly why the loop lives in `core` (BoD §5.1).
"""

from __future__ import annotations

import asyncio

from disco.core import (
    NoOpCondenser,
    SecurityRisk,
    SqliteEventStore,
    ToolResult,
)
from disco.core.llm import (
    CompletionResponse,
    OperatingMode,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)
from disco.core.loop import AgentLoop, AgentStep, NeverConfirm

# ---- fake Agent (scripted) --------------------------------------------------


class ScriptedAgent:
    """Returns pre-scripted AgentSteps. A step item may be an AgentStep, an
    Exception (raised — e.g. LLMContextWindowExceeded), or a callable(view) ->
    AgentStep. `before[i]` runs an async side effect at the start of step i (used
    to simulate a concurrent message arrival)."""

    def __init__(self, steps, *, before=None):
        self._steps = list(steps)
        self._before = before or {}
        self.seen_views = []
        self.seen_tools = []
        self.calls = 0

    async def step(
        self, view, tools, *, mode: OperatingMode, overflow_signal, on_stream=None,
        temperature=None, assist=False,
    ):
        i = self.calls
        if i in self._before:
            await self._before[i]()
        self.seen_views.append(view)
        self.seen_tools.append([getattr(t, "name", None) for t in tools])
        self.calls += 1
        item = self._steps[min(i, len(self._steps) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(view)
        return item


class GatedAgent:
    """Blocks at step `gate_at` until `proceed` is set — for deterministic
    concurrency tests (pause/cancel landing between steps)."""

    def __init__(self, steps, *, gate_at):
        self._steps = list(steps)
        self.gate_at = gate_at
        self.reached = asyncio.Event()
        self.proceed = asyncio.Event()
        self.calls = 0

    async def step(self, view, tools, *, mode, overflow_signal, on_stream=None,
                   temperature=None, assist=False):
        i = self.calls
        self.calls += 1
        if i == self.gate_at:
            self.reached.set()
            await self.proceed.wait()
        return self._steps[min(i, len(self._steps) - 1)]


# ---- fake ToolExecutor ------------------------------------------------------


class FakeExecutor:
    def __init__(self, *, tools=None, result: ToolResult | None = None, raises=None):
        self._tools = tools or [
            ToolSpec(name="shell", description="run a shell command", parameters_schema={})
        ]
        self._result = result
        self._raises = raises
        self.calls = []

    def available_tools(self):
        return self._tools

    async def execute(self, call):
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


# ---- fake SecurityAnalyzer --------------------------------------------------


class FakeAnalyzer:
    def __init__(self, risk: SecurityRisk = SecurityRisk.UNKNOWN):
        self._risk = risk

    def assess(self, action):
        return self._risk


# ---- fake Condenser / Summarizer --------------------------------------------


class FakeCondenser:
    """Scripted condenser: should_condense returns `request`; condense returns
    `tombstone`. Counts calls so tests can assert wiring."""

    def __init__(self, *, request=None, tombstone=None):
        self._request = request
        self._tombstone = tombstone
        self.should_calls = 0
        self.condense_calls = 0

    def should_condense(self, view, *, token_count):
        self.should_calls += 1
        return self._request

    async def condense(self, events, view, *, summarizer):
        self.condense_calls += 1
        return self._tombstone


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    async def summarize(self, messages):
        self.calls += 1
        return "[summary]"


# ---- fake StopHook ----------------------------------------------------------


class ScriptedStopHook:
    """allow_stop returns scripted booleans (repeats last)."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = 0

    async def allow_stop(self, state, events):
        i = self.calls
        self.calls += 1
        return self._verdicts[min(i, len(self._verdicts) - 1)]


# ---- a provider that varies per call (for the §10.9 acceptance gate) --------


class SequenceProvider:
    """A ModelProvider double whose response varies per call. `scripted` is a
    list of dicts: {"text": str, "tool_calls": [ProposedToolCall]}."""

    name = "seq"

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0
        self.seen = []

    async def complete(self, req, *, model):
        self.seen.append(req)
        i = self.calls
        self.calls += 1
        spec = self._scripted[min(i, len(self._scripted) - 1)]
        tool_calls = spec.get("tool_calls", [])
        return CompletionResponse(
            text=spec.get("text", ""),
            tool_calls=tool_calls,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="tool_calls" if tool_calls else "stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        final = await self.complete(req, model=model)
        yield StreamChunk(delta_text=final.text)
        yield StreamChunk(done=True, final=final)

    def supports(self, requirement, *, model):
        return True


# ---- loop builder -----------------------------------------------------------


def build_loop(
    agent,
    *,
    store=None,
    executor=None,
    analyzer=None,
    policy=None,
    condenser=None,
    summarizer=None,
    mode: OperatingMode = OperatingMode.INTERACTIVE,
    max_iterations: int = 500,
    stop_hooks=None,
    stuck_thresholds=None,
    conversation_id: str = "conv",
    planning_tools: frozenset[str] = frozenset(),
    plan_tool: str = "submit_plan",
    execution_mode: OperatingMode = OperatingMode.LONG_HORIZON,
):
    """Construct an AgentLoop over fakes. `router` is unused by the loop itself
    (the Agent wraps it) so a None sentinel is passed."""
    store = store or SqliteEventStore(":memory:")
    loop = AgentLoop(
        conversation_id,
        store,
        agent,
        executor or FakeExecutor(),
        None,  # router — held but unused by the loop (the Agent owns it)
        analyzer or FakeAnalyzer(),
        policy or NeverConfirm(),
        condenser or NoOpCondenser(),
        summarizer or FakeSummarizer(),
        mode=mode,
        max_iterations=max_iterations,
        stop_hooks=stop_hooks,
        stuck_thresholds=stuck_thresholds,
        planning_tools=planning_tools,
        plan_tool=plan_tool,
        execution_mode=execution_mode,
    )
    return loop, store


def action_step(tool: str = "shell", args: dict | None = None, thought: str = "do it"):
    from disco.core import ToolCall

    return AgentStep(
        thought=thought, tool_call=ToolCall(tool_name=tool, arguments=args or {}), finished=False
    )


def finish_step(thought: str = "done"):
    return AgentStep(thought=thought, tool_call=None, finished=True)
