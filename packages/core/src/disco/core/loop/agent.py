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

import re
import uuid
from typing import cast

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
from ..llm.request_budget import RequestBudgetEstimate
from ..llm.types import EMPTY_REASONING_ONLY_METADATA_KEY
from ..obs import log_span
from ..think import strip_think_spans
from ..view import View
from .boundaries import AgentStep, StreamHook, is_finish_tool_name

# The View feeds the model's own prior thoughts back into context with a rotating
# surface-form prefix ("Reasoning: …" / "Thought: …" — a deterministic-by-seq
# anti-overfit decoration, see view.py B3). A model (observed: MiniMax) then ECHOES
# that decoration into the NEXT thought it emits, and since each turn re-decorates,
# the prefix STACKS — the stored/displayed thought becomes
# "Reasoning: Reasoning: Reasoning: …". We strip the leaked leading decorator(s) off
# the model's response text BEFORE it is stored as the ActionEvent thought, which
# both cleans the stored event AND breaks the feedback loop (the clean thought goes
# back into context, gets exactly one fresh decoration, never compounds).
#
# Scoped to ONLY the surface forms the View applies to AGENT thoughts (Reasoning /
# Thought) — the "Observation:" / "Output:" forms decorate tool-result messages, not
# thoughts, so a thought never legitimately starts with one and we don't strip them.
_THOUGHT_DECOR_RE = re.compile(
    r"^(?:\s*(?:Reasoning|Thought)\s*:\s*)+",
    re.IGNORECASE,
)


def _clean_thought(text: str) -> str:
    """Strip leaked, stacked surface-form prefixes the View added and the model
    echoed back ("Reasoning: Reasoning: …"). Only LEADING repeated decorators are
    removed — a thought that legitimately says "Reasoning: foo" mid-sentence is
    untouched.

    Also strips inline ``<think>…</think>`` reasoning spans so inline-think
    models (MiniMax) never leak raw chain-of-thought into the user-visible
    thought/message. Idempotent; a clean thought is returned unchanged."""
    return _THOUGHT_DECOR_RE.sub("", strip_think_spans(text))


def _has_unclosed_think(text: str) -> bool:
    """A tool-less turn whose visible content opens a ``<think>`` block but never
    closes it was CUT OFF mid-reasoning — the model ran out of output budget
    before it finished thinking (and therefore before it could emit a tool call).

    This is the MiniMax wedge (fix-slides-wedge): a reasoning model that inlines
    its chain-of-thought as literal ``<think>`` tags in ``content`` (rather than a
    separate ``reasoning_content`` field) can exhaust the provider's output cap
    deep inside the block, yet NOT report ``finish_reason=="length"`` — so the
    plain length check below misses it and the turn is mis-read as a clean,
    completed no-op. An unclosed ``<think>`` is an unambiguous, model-agnostic
    "the turn never finished" signal: openai_provider already documents the same
    invariant for its F5 think-budget pass. Models that stream reasoning out of
    band (no ``<think>`` in ``content``) never match, so this is naturally scoped
    to the inline-think providers that actually hit the wedge."""
    if "<think>" not in text:
        return False
    return text.lower().count("<think>") > text.lower().count("</think>")


def _pick_tool_call_detail(tool_calls, finish_alias):  # noqa: ANN001 — duck-typed provider calls
    """P6/W-32 — pick the single action from a (possibly batched) response. Prefer the
    first NON-finish call so a batched [finish/finalizer, real action] still executes the
    real work (the finish/finalizer must then come on its own turn, where the gates run).
    The contract finalizer alias counts as finish — so it can't shadow a real action — and
    the chosen name is CANONICALIZED to "finish" when it is the finish signal, so the alias
    name never reaches the engine/event log; every downstream surface sees plain "finish".
    Returns (tool_name, arguments, requested_verification)."""
    pc = next(
        (c for c in tool_calls if not is_finish_tool_name(c.tool_name, finish_alias)),
        tool_calls[0],
    )
    requested_verification = finish_alias is not None and pc.tool_name == finish_alias
    name = "finish" if is_finish_tool_name(pc.tool_name, finish_alias) else pc.tool_name
    return name, pc.arguments, requested_verification


def _pick_tool_call(tool_calls, finish_alias):  # noqa: ANN001 — duck-typed provider calls
    """Compatibility wrapper for tests/importers that only need name + args."""
    name, args, _requested_verification = _pick_tool_call_detail(tool_calls, finish_alias)
    return name, args


class RouterAgent:
    """[CONTRACT] An `Agent` that wraps the LLM router."""

    def __init__(
        self,
        router: DefaultLLMRouter,
        *,
        conversation_id: str | None = None,
        requirements: frozenset[Requirement] = frozenset({Requirement.TOOL_CALLING}),
        model_override: str | None = None,
        driver_context_window: int | None = None,
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
        # P6 — the active Build contract's verification finalizer (ready_for_*_verification),
        # treated identically to `finish` in batched-call selection so the model can't
        # discard a real action by batching it with the finalizer, and so the canonical
        # "finish" name flows to every downstream surface. None ⇒ no contract governs.
        # Set post-construction by the runtime (set_finish_alias) once the contract resolves.
        self._finish_alias: str | None = None
        # v1.2 model-pill hook: the driver model key chosen for THIS conversation,
        # overriding the settings assignment. None → follow settings. Set by the
        # app/agent server from the per-conversation pill selection.
        self._model_override = model_override
        # Immutable run binding resolved by the runtime before composition.  This
        # is evidence metadata only; the agent never probes or rereads config.
        # Exact type is intentional because bool is an int subclass in Python.
        self._driver_context_window = (
            driver_context_window
            if type(driver_context_window) is int and driver_context_window > 0
            else None
        )

    def set_finish_alias(self, alias: str | None) -> None:
        """P6 — bind the active Build contract's verification finalizer (or None). Set by
        the runtime once the contract resolves, so the agent treats that name as `finish`."""
        self._finish_alias = alias

    def _build_completion_request(
        self,
        view: View,
        tools: list,
        *,
        mode: OperatingMode,
        overflow_signal: OverflowSignal,
        temperature: float | None = None,
        assist: bool = False,
        attempt: int = 1,
        provider_prefs: dict | None = None,
    ) -> CompletionRequest:
        prefill = None
        if mode == OperatingMode.PLANNING and disco_env("PLAN_PREFILL") == "1":
            prefill = (
                "I've analyzed the request and current workspace state. To advance, I will now"
            )
        elif mode == OperatingMode.LONG_HORIZON and disco_env("EXEC_PREFILL") == "1":
            prefill = (
                "Given the current workspace state and the last tool result, my next action is to"
            )

        return CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.AGENT_DRIVER,
                difficulty=overflow_signal.difficulty,
                requirements=self._requirements,
                mode=mode,
            ),
            messages=view.messages,
            tools=tools,
            assistant_prefill=prefill,
            assist=assist,
            temperature=temperature if temperature is not None else self._temperature,
            request_id=f"req_{uuid.uuid4().hex}",
            attempt=attempt,
            provider_prefs=provider_prefs,
            metadata=(
                {"driver_context_window": self._driver_context_window}
                if self._driver_context_window is not None
                else None
            ),
        )

    def _build_call_context(self, overflow_signal: OverflowSignal) -> CallContext:
        return CallContext(
            conversation_id=self._cid,
            consecutive_tool_errors=overflow_signal.consecutive_tool_errors,
            last_local_confidence=overflow_signal.last_local_confidence,
            model_override=self._model_override,
        )

    def request_budget_preview(
        self,
        view: View,
        tools: list,
        *,
        mode: OperatingMode,
        overflow_signal: OverflowSignal,
        temperature: float | None = None,
        assist: bool = False,
    ) -> RequestBudgetEstimate | None:
        """Synchronous best-effort budget preview. Uses the same request-shaping
        path as real step() with attempt=1/provider_prefs=None. Returns None
        when the router or provider lacks preview support."""
        preview_fn = getattr(self._router, "request_budget_preview", None)
        if not callable(preview_fn):
            return None
        req = self._build_completion_request(
            view,
            tools,
            mode=mode,
            overflow_signal=overflow_signal,
            temperature=temperature,
            assist=assist,
            attempt=1,
            provider_prefs=None,
        )
        ctx = self._build_call_context(overflow_signal)
        try:
            return cast(RequestBudgetEstimate | None, preview_fn(req, context=ctx))
        except Exception:
            return None

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
        req = self._build_completion_request(
            view,
            tools,
            mode=mode,
            overflow_signal=overflow_signal,
            temperature=temperature,
            assist=assist,
            attempt=attempt,
            provider_prefs=provider_prefs,
        )
        ctx = self._build_call_context(overflow_signal)
        # STREAM the driver call, not `complete()`. This is load-bearing, not an
        # optimization: for large tool-call arguments (e.g. a `file_write` with a big
        # file body), some OpenAI-compatible providers (observed: DeepSeek via
        # OpenRouter/NovitaAI) return EMPTY `arguments` in non-streaming mode but
        # assemble them correctly from streamed deltas — and far faster (~18s vs ~5min
        # for the same request). Consuming the stream to its final chunk gives the
        # correctly-assembled response; surfacing the intermediate deltas to the UI
        # (watch-it-write) is the next layer on top of this.
        resp = None
        with log_span(
            "agent.step",
            role="agent_driver",
            cid=self._cid,
            request_id=req.request_id,
            driver_context_window=self._driver_context_window,
            model_repair_attempt=req.attempt,
        ) as span:
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
            if resp.routing is not None:
                span["provider"] = resp.routing.provider
                span["path"] = resp.routing.path
                span["provider_attempt"] = resp.routing.attempt

        empty_reasoning_diagnostic = getattr(resp, "response_metadata", {}).get(
            EMPTY_REASONING_ONLY_METADATA_KEY
        )
        if resp.tool_calls:
            # One-action-per-iteration: take the FIRST proposed call (§3).
            # W-32 (secondary): when the model BATCHES a `finish` ALONGSIDE a real
            # action in the same response, a bare tool_calls[0] could let the
            # finish preempt the real work. Prefer the first NON-finish call so a
            # batched [finish, real_action] still executes the work; the
            # affirmative finish must then come on its own turn (where the finish
            # gates run). All-finish / single-finish falls back to the first call.
            _name, _args, _requested_verification = _pick_tool_call_detail(
                resp.tool_calls, self._finish_alias
            )
            return AgentStep(
                thought=_clean_thought(resp.text),
                tool_call=ToolCall(tool_name=_name, arguments=_args),
                finished=False,
                requested_verification=_requested_verification,
                llm_response_id=resp.request_id,
                empty_reasoning_diagnostic=empty_reasoning_diagnostic,
            )
        # No tool call → a tool-less PROSE turn. Completion semantics are owned
        # by the AGENT (Research↔Build isolation), not derived from the per-step
        # mode:
        #   - ResearchAgent (prose_finishes=True): the prose IS the answer →
        #     finished. Research/chat completion depends on this.
        #   - BuildAgent (prose_finishes=False): talking is NOT finishing. The
        #     run ends only via the affirmative `finish` tool the loop intercepts,
        #     so mid-run talk-back can't silently end a build.
        #
        # W-31 — TRUNCATION GUARD. `finish_reason=="length"` means the provider
        # cut the message off mid-sentence (the output budget was exhausted —
        # worst right after a re-steer, when a long <think> preamble eats it).
        # A truncated prose turn is NOT a completed turn: surfacing it as
        # finished (Research) — or as a clean no-op (Build) — is the bug. Flag it
        # and force `finished=False` so the loop injects a "continue where you
        # left off" reminder and re-steps instead of ending on a fragment.
        #
        # fix-slides-wedge — STRUCTURAL truncation backstop. Some reasoning
        # providers (MiniMax inlines its chain-of-thought as `<think>` tags in
        # `content`) exhaust the output cap mid-reasoning WITHOUT reporting
        # `finish_reason=="length"`, so the check above misses it and the
        # never-finished, tool-less turn is mis-classified as a clean no-op. The
        # loop then silently re-steps a 2-minute reasoning dump that never reaches
        # a tool call — a wedge to the user (the agent slides build that drafted a
        # whole deck inside one unclosed `<think>` and produced nothing). An
        # unclosed `<think>` is an unambiguous "cut off mid-thought" signal:
        # treat it as truncated too, so the W-31 handler fires its "you were cut
        # off — be concise and take the action now with a tool call" steer
        # immediately (turn 1, not after 3 silent no-ops) and the no-op valve
        # still bounds a truncation storm into a VISIBLE PAUSED halt.
        truncated = resp.finish_reason == "length" or _has_unclosed_think(resp.text)
        return AgentStep(
            thought=_clean_thought(resp.text),
            tool_call=None,
            finished=self._prose_finishes and not truncated,
            truncated=truncated,
            llm_response_id=resp.request_id,
            empty_reasoning_diagnostic=empty_reasoning_diagnostic,
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
        driver_context_window: int | None = None,
        temperature: float = 0.0,
    ) -> None:
        super().__init__(
            router,
            conversation_id=conversation_id,
            requirements=requirements,
            model_override=model_override,
            driver_context_window=driver_context_window,
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
        driver_context_window: int | None = None,
        temperature: float = 0.4,
    ) -> None:
        super().__init__(
            router,
            conversation_id=conversation_id,
            requirements=requirements,
            model_override=model_override,
            driver_context_window=driver_context_window,
            temperature=temperature,
            prose_finishes=False,
        )
