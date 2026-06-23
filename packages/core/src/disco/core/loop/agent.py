"""RouterAgent — the concrete `Agent` (agent-loop-contract.md §3).

The brain: given the model-facing View and the available tools, it builds a
CompletionRequest(role=AGENT_DRIVER), calls the LLMRouter, and maps the
response's FIRST ProposedToolCall to a ToolCall — the only place the model is
consulted for action selection. It returns exactly one AgentStep (one-action-
per-iteration). It does NOT execute tools.

Typed against `DefaultLLMRouter` (not the bare `LLMRouter` protocol) because it
uses that router's `CallContext` extension to thread the conversation id, the
(now-advisory) runtime signals, and the per-conversation `model_override` — the
backend hook for the main-screen model pill (router contract §4, v1.2).

v1.2 note: under the deterministic router these `overflow_signal` fields
(difficulty, tool-error streak, confidence) no longer steer model selection; they
are carried as advisory metadata only. The seam is unchanged — the agent still
asks for the AGENT_DRIVER role and gets back the assigned model — so the loop did
NOT need to change. The one addition is `model_override`, which lets the operator
pin the driver model for THIS conversation from the UI.

v1 finish convention [INTERIOR]: if the model returns no tool call, the agent is
treated as finished (it stopped acting). A dedicated `finish` tool is the likely
refinement once the Tool/Sandbox contract lands; the loop already supports a
thought-only no-op step independently of this Agent's choice.

v1 risk [INTERIOR]: the response carries no structured self-risk, so
`self_assessed_risk` stays UNKNOWN — the independent SecurityAnalyzer is the real
signal (BoD §17.2).
"""

from __future__ import annotations

import uuid

from ..env import disco_env
from ..events import ToolCall
from ..llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    ModelRole,
    OperatingMode,
    OverflowSignal,
    Requirement,
)
from ..obs import log_span
from ..view import View
from .boundaries import AgentStep, StreamHook


class RouterAgent:
    """[CONTRACT] An `Agent` that wraps the LLM router."""

    def __init__(
        self,
        router: DefaultLLMRouter,
        *,
        conversation_id: str | None = None,
        requirements: frozenset[Requirement] = frozenset({Requirement.TOOL_CALLING}),
        model_override: str | None = None,
        temperature: float = 0.4,
        prose_finishes: bool = True,
    ) -> None:
        self._router = router
        self._cid = conversation_id
        self._requirements = requirements
        # Cluster 9 anti-fewshot jitter for the acting driver (0.0 would be a
        # deterministic self-imitation chain on a long run).
        self._temperature = temperature
        # Research↔Build isolation: completion semantics are owned by the AGENT,
        # not a mode flag. `prose_finishes=True` means a tool-less prose turn IS
        # the answer (Research/chat). `False` means talking isn't finishing —
        # the run ends only via the `finish` tool (Build). See ResearchAgent /
        # BuildAgent below; this base default (True) matches the original v1
        # convention so direct RouterAgent(router) callers are unchanged.
        self._prose_finishes = prose_finishes
        # v1.2 model-pill hook: the driver model key chosen for THIS conversation,
        # overriding the settings assignment. None → follow settings. Set by the
        # app/agent server from the per-conversation pill selection.
        self._model_override = model_override

    async def step(
        self,
        view: View,
        tools: list,
        *,
        mode: OperatingMode,
        overflow_signal: OverflowSignal,
        on_stream: StreamHook | None = None,
        temperature: float | None = None,
        assist: bool = False,
        attempt: int = 1,
        provider_prefs: dict | None = None,
    ) -> AgentStep:
        # B9: Assistant prefill. In PLANNING mode, force
        # the model to start its thought with an honest acknowledgment of
        # the task, reducing the "lazy prose" failure. Gated by flag.
        prefill = None
        if mode == OperatingMode.PLANNING and disco_env("PLAN_PREFILL") == "1":
            prefill = (
                "I've analyzed the request and current workspace state. "
                "To advance, I will now"
            )
        # C13: Mirror the B9 prefill pattern for the EXECUTION phase
        # (LONG_HORIZON mode in the engine: `execution_mode: OperatingMode =
        # OperatingMode.LONG_HORIZON`). Biases the model toward a concrete
        # next-action tool call rather than another "let me think..."
        # prose turn. Gated by an independent flag so it can be A/B'd
        # against the planning flag; default OFF keeps the gated-experiment
        # contract — flag OFF is byte-identical to today (prefill stays None).
        elif mode == OperatingMode.LONG_HORIZON and disco_env("EXEC_PREFILL") == "1":
            prefill = (
                "Given the current workspace state and the last tool result, "
                "my next action is to"
            )

        req = CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.AGENT_DRIVER,
                difficulty=overflow_signal.difficulty,
                requirements=self._requirements,
                mode=mode,
            ),
            messages=view.messages,
            tools=tools,
            assistant_prefill=prefill,
            assist=assist,  # weak-model assist gate → F1 reads req.assist for recovery
            # Cluster 9: a small non-zero temperature for the driver (anti-fewshot).
            # A long uniform run at temp 0.0 is a near-deterministic self-imitation
            # chain — maximally prone to repeating a prior failing pattern. The
            # summarizer + other structured roles stay at 0.0 (they set it
            # explicitly); only the acting driver gets the jitter. A per-step
            # `temperature` override (the loop's stuck-escape bump) wins when given.
            temperature=temperature if temperature is not None else self._temperature,
            request_id=f"req_{uuid.uuid4().hex}",
            # F5 — repair-attempt counter. The engine increments this on each
            # retry of a failed call; the provider reads it to disable thinking
            # on attempt ≥ 2. Default 1 = first try; assist-OFF callers ignore.
            attempt=attempt,
            # P2 — provider routing escalation (threaded from the driver on
            # LLMProviderUnavailable retries; None on first/normal calls).
            provider_prefs=provider_prefs,
        )
        ctx = CallContext(
            conversation_id=self._cid,
            consecutive_tool_errors=overflow_signal.consecutive_tool_errors,
            last_local_confidence=overflow_signal.last_local_confidence,
            model_override=self._model_override,
        )
        # STREAM the driver call, not `complete()`. This is load-bearing, not an
        # optimization: for large tool-call arguments (e.g. a `file_write` with a big
        # file body), some OpenAI-compatible providers (observed: DeepSeek via
        # OpenRouter/NovitaAI) return EMPTY `arguments` in non-streaming mode but
        # assemble them correctly from streamed deltas — and far faster (~18s vs ~5min
        # for the same request). Consuming the stream to its final chunk gives the
        # correctly-assembled response; surfacing the intermediate deltas to the UI
        # (watch-it-write) is the next layer on top of this.
        resp = None
        with log_span("agent.step", role="agent_driver", cid=self._cid) as span:
            async for chunk in self._router.stream_complete(req, context=ctx):
                # Forward tool-call arg deltas (the file body) to the watch-it-write
                # hook. The agent stays ignorant of tool semantics — it just relays the
                # raw fragments; the loop accumulates + extracts the field to display.
                if on_stream is not None and chunk.tool_args_delta:
                    await on_stream(chunk)
                if chunk.done and chunk.final is not None:
                    resp = chunk.final
            if resp is None:
                # The stream produced no terminal chunk — fall back to the non-stream
                # call so a provider that doesn't stream still works (never wedge).
                resp = await self._router.complete(req, context=ctx)
            # measured fields land on the span's `end` record (trace-assertable)
            span["model"] = resp.model_used
            span["in_tokens"] = resp.usage.input_tokens
            span["out_tokens"] = resp.usage.output_tokens
            span["cached_tokens"] = resp.usage.cached_tokens
            span["finish"] = str(resp.finish_reason)

        if resp.tool_calls:
            # One-action-per-iteration: take the FIRST proposed call (§3).
            # W-32 (secondary): when the model BATCHES a `finish` ALONGSIDE a real
            # action in the same response, a bare tool_calls[0] could let the
            # finish preempt the real work. Prefer the first NON-finish call so a
            # batched [finish, real_action] still executes the work; the
            # affirmative finish must then come on its own turn (where the finish
            # gates run). All-finish / single-finish falls back to the first call.
            pc = next(
                (c for c in resp.tool_calls if c.tool_name != "finish"),
                resp.tool_calls[0],
            )
            return AgentStep(
                thought=resp.text,
                tool_call=ToolCall(tool_name=pc.tool_name, arguments=pc.arguments),
                finished=False,
                llm_response_id=resp.request_id,
            )
        # No tool call → a tool-less PROSE turn. Completion semantics are owned
        # by the AGENT (Research↔Build isolation), not derived from the per-step
        # mode:
        #   - ResearchAgent (prose_finishes=True): the prose IS the answer →
        #     finished. Research/chat completion depends on this.
        #   - BuildAgent (prose_finishes=False): talking is NOT finishing. The
        #     run ends only via the affirmative `finish` tool the loop intercepts,
        #     so mid-run talk-back can't silently end a build.
        return AgentStep(
            thought=resp.text,
            tool_call=None,
            finished=self._prose_finishes,
            llm_response_id=resp.request_id,
        )


class ResearchAgent(RouterAgent):
    """The Research / plain-chat brain. A tool-less prose turn IS the answer →
    the run finishes (single-pass-ish grounded chat). Deterministic by default
    (temperature 0.0) — research answers shouldn't jitter. This is a distinct
    class from BuildAgent so a Build-surface change can never silently alter
    Research's completion behavior (the leak that caused the GAP B bug)."""

    def __init__(
        self,
        router: DefaultLLMRouter,
        *,
        conversation_id: str | None = None,
        requirements: frozenset[Requirement] = frozenset({Requirement.TOOL_CALLING}),
        model_override: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        super().__init__(
            router,
            conversation_id=conversation_id,
            requirements=requirements,
            model_override=model_override,
            temperature=temperature,
            prose_finishes=True,
        )


class BuildAgent(RouterAgent):
    """The Build (agentic) brain. Talking is NOT finishing — the long-horizon
    run ends only via the affirmative `finish` tool, so the mid-run talk-back
    the prompt encourages can't silently end a build. A small non-zero
    temperature (anti-fewshot) keeps a long uniform run from collapsing into a
    deterministic self-imitation chain."""

    def __init__(
        self,
        router: DefaultLLMRouter,
        *,
        conversation_id: str | None = None,
        requirements: frozenset[Requirement] = frozenset({Requirement.TOOL_CALLING}),
        model_override: str | None = None,
        temperature: float = 0.4,
    ) -> None:
        super().__init__(
            router,
            conversation_id=conversation_id,
            requirements=requirements,
            model_override=model_override,
            temperature=temperature,
            prose_finishes=False,
        )
