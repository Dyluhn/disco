"""The model-driving step: tool-set assembly + one bounded `agent.step()`.

Extracted from engine.py as a stateful collaborator: `Driver` holds a back-ref
to its `AgentLoop` and runs the (e) drive step — mode-scoped tool visibility, the
watch-it-write stream hook, the weak-model invalid-tool requery ladder (Rung 7),
transient-retry backoff, and the context-window hard-reset path. Bodies are
byte-identical to the former AgentLoop methods with `self.` rewritten to
`self._loop.` (sibling calls stay in-collaborator).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal, cast

from ..context.compaction import CompactionPolicy, context_compact_if_needed
from ..effects import CoverageSpan, EffectCapability, MutationReceipt, ObservationReceipt
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    ToolCall,
)
from ..inspect import inspect_enabled, record_progress_shadow, record_tool_scope
from ..llm import (
    BudgetExceeded,
    LLMAuthError,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
    OperatingMode,
)
from ..llm.request_budget import RequestBudgetEstimate
from ..llm.types import EMPTY_REASONING_ONLY_METADATA_KEY
from ..obs import log_event
from ..view import View, repair_tool_call_adjacency
from . import signals, view_render
from .boundaries import AgentStep
from .context_live import context_pack_enabled, protected_context_compaction_seqs
from .control import Disp
from .fc_kit import _nearest_tool_name
from .messages import _PLAN_EXPLORE_READ_CAP, _describe_llm_error
from .progress import reduce_progress
from .resource_context import merge_spans
from .stream_extract import extract_partial_string_field
from .stuck import successful_mutation_with_receipt
from .tool_specs import (
    _ask_user_tool_singleton,
    _clarify_tool_singleton,
    _notify_user_tool_singleton,
    _propose_plan_update_tool_singleton,
    _questions_v2_tool_singleton,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..llm import StreamChunk
    from .boundaries import StreamHook
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

_sleep = asyncio.sleep
_DRIVER_RETRY_BACKOFFS_S: tuple = (10.0, 30.0, 90.0)

# FORCED-SUBMIT read grace: how many ADDITIONAL grounding reads a model may make AFTER
# force_submit fired (at _PLAN_EXPLORE_READ_CAP) before the offered tools collapse to
# submit_plan only. ~30% of revision re-plans want to file_read the current files to ground
# the diff BEFORE submitting; narrowing to submit-only stranded them (file_read rejected →
# actionless → killed). A few grounding reads then submit-only bounds a runaway (the
# actionless valve already catches tool-LESS prose turns; this bounds tool-CALL read loops).
_FORCE_SUBMIT_READ_GRACE = 3


def _escalated_provider_prefs(n: int) -> dict:
    """P2 — escalation ladder for provider routing retries.

    n=1 (first occurrence): enable fallbacks (allow OpenRouter to try the next
    upstream). n≥2: additionally hard-exclude Chutes (the observed free-pool
    offender) so OpenRouter doesn't route there again. Cap: the caller enforces
    ≤2 total provider retries (reusing the existing requery budget)."""
    if n >= 2:
        return {"allow_fallbacks": True, "ignore": ["Chutes"]}
    return {"allow_fallbacks": True}


# A hard temperature jitter for the single stuck-escape retry step. When the loop
# detects a repeating action→error/obs rut it gives the model ONE retry at this
# temperature (vs. the driver's small anti-fewshot default) to break the
# self-imitation chain, BEFORE declaring STUCK. Pairs with a rotating reminder
# pool (C7) so consecutive escape attempts differ in bytes as well as in
# sampling temperature — otherwise a model that has internalized the previous
# reminder would echo it back verbatim and the escape wouldn't break the
# self-imitation chain (the test on test_loop_stuck.py locks this in).
_STUCK_ESCAPE_TEMP = 0.9
_PLANNING_TOOL_REFUSAL_NEEDLE = "is not available in PLANNING mode"
_PLANNING_TOOL_REFUSAL_ESCALATE_AT = 2
_PLANNING_TOOL_REFUSAL_NARROW_AT = 3
_PLANNING_TOOL_REFUSAL_READ_TOOLS = frozenset({"file_read"})
_EMPTY_REASONING_REPAIR_REMINDER = (
    "Your previous response produced no visible text and no tool call — call exactly "
    "one tool now, or say in plain text what you need."
)
_PROSE_NOOP_REPAIR_REMINDER = (
    "You described the next action instead of performing it — call the tool for it in THIS turn."
)
# During an authenticated redundant-read escape, general execution and the
# read-only fan-out helper are equivalent content-access capabilities: r15 used
# ``shell`` + ``wc``/``sed``, while ``delegate_explore`` is explicitly wired to
# a helper with ``file_read``. Bind the capability at the tool surface rather
# than parsing arbitrary command languages or prompts.
_STUCK_ESCAPE_EQUIVALENT_READ_BYPASS_TOOLS = frozenset(
    {"shell", "shell_exec", "code_exec", "delegate_explore"}
)


def _view_has_current_objective(view: View) -> bool:
    return any("<current-objective>" in (message.content or "") for message in view.messages)


def _is_tool_result_adjacency_protocol_error(err: LLMError) -> bool:
    """Provider-side protocol rejection for broken tool_call/tool-result order."""
    text = " ".join(
        str(part)
        for part in (
            err,
            getattr(err, "provider", ""),
            getattr(err, "model", ""),
            getattr(err, "code", ""),
            getattr(err, "error_code", ""),
        )
        if part
    ).lower()
    if "tool call result does not follow" in text:
        return True
    return "2013" in text and "invalid" in text and "param" in text


def _is_planning_tool_refusal(event: Event) -> bool:
    return isinstance(event, AgentErrorEvent) and _PLANNING_TOOL_REFUSAL_NEEDLE in event.error


def planning_tool_refusal_streak(events: list[Event]) -> int:
    """Consecutive planning-gate tool refusals at the event-log tail.

    ActionEvents are pairing noise between refusal observations. Successful
    observations, a submitted plan, or a user message reset the streak.
    """
    streak = 0
    for event in reversed(events):
        if _is_planning_tool_refusal(event):
            streak += 1
            continue
        if isinstance(event, ActionEvent):
            continue
        if isinstance(event, ObservationEvent | PlanEvent):
            break
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            break
        if isinstance(event, AgentErrorEvent):
            break
    return streak


class Driver:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def _record_model_repair(
        self, repair_kind: str, *, attempt: int, tool_name: str | None = None
    ) -> None:
        """Put every hidden model/provider repair into the per-conversation trace.

        Requeries intentionally stay out of the durable chat/event history, but that
        previously made a run which guessed bad tool syntax or repeatedly triggered
        provider 400s look clean to the soak. DISCO_INSPECT traces are bounded and
        redacted at the HTTP boundary, so record only structural diagnostics here.
        """
        fields: dict[str, object] = {
            "cid": self._loop.conversation_id,
            "repair_kind": repair_kind,
            "attempt": attempt,
        }
        if tool_name:
            fields["tool_name"] = tool_name
        log_event("agent.repair", **fields)

    async def _wait_retry_backoff(self, delay_s: float) -> bool:
        """Wait for a retry delay, returning False when a control op interrupts it.

        The drive loop holds its step lock while it calls the provider.  A plain
        ``sleep`` therefore also held that lock for the full 10/30/90-second
        ladder, preventing cancel and steer from reaching their next checkpoint.
        Race the delay against the loop-owned control event and clean up both
        tasks on every exit.  This interrupts only an idle backoff; an in-flight
        provider response or tool side effect remains atomic.
        """
        interrupt = self._loop._retry_interrupt
        if interrupt.is_set():
            return False

        delay_task = asyncio.create_task(_sleep(delay_s))
        interrupt_task = asyncio.create_task(interrupt.wait())
        tasks = (delay_task, interrupt_task)
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if delay_task in done:
                await delay_task
            interrupted = interrupt_task in done or interrupt.is_set()
            return not interrupted
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _persist_empty_reasoning_diagnostic(self, diagnostic: dict) -> None:
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=_EMPTY_REASONING_REPAIR_REMINDER),
                meta={
                    "diagnostic": EMPTY_REASONING_ONLY_METADATA_KEY,
                    **diagnostic,
                },
            )
        )

    async def _persist_prose_noop_diagnostic(self, step: AgentStep) -> None:
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=_PROSE_NOOP_REPAIR_REMINDER),
                meta={
                    "diagnostic": signals.PROSE_NOOP_REPAIR_DIAGNOSTIC,
                    "content_len": len(step.thought),
                    "tool_call_count": 0,
                    "llm_response_id": step.llm_response_id,
                },
            )
        )

    def build_stream_hook(self) -> StreamHook | None:
        """Per-step watch-it-write hook (or None if no sink is wired). Decodes the
        driver's streamed tool-call arg fragments into growing file/edit frames
        and publishes them live via `self.stream_sink`. The frontend appends each
        `delta` to a per-path buffer and reconciles against the final, authoritative
        ActionEvent when it lands (which supersedes the streamed text)."""
        sink = self._loop.stream_sink
        if sink is None:
            return None
        # Per-step, per-tool-index accumulator (fresh each step → no stale state).
        state: dict[int, dict] = {}

        async def _hook(chunk: StreamChunk) -> None:
            st = state.setdefault(
                chunk.tool_index, {"args": "", "sent": 0, "path": None, "tool": ""}
            )
            st["args"] += chunk.tool_args_delta
            if chunk.tool_name:
                st["tool"] = chunk.tool_name
            if st["tool"] not in self._loop._STREAMING_WRITE_TOOLS:
                return  # only stream tools that carry a file body/edit replacement
            if not st["path"]:
                # Require the WHOLE path (closing quote present) so a frame never
                # shows a half-typed filename like "styles" for "styles.css".
                p = extract_partial_string_field(st["args"], "path", require_complete=True)
                if p:
                    st["path"] = p
            if not st["path"]:
                return
            field = "new" if st["tool"] == "file_edit" else "content"
            content = extract_partial_string_field(st["args"], field)
            if content is None:
                return
            new = content[st["sent"] :]
            if len(new) < self._loop._STREAM_FLUSH_CHARS and "\n" not in new:
                return  # coalesce — wait for more
            st["sent"] = len(content)
            sink(
                {
                    "type": "file_stream",
                    "tool": st["tool"],
                    "path": st["path"] or "",
                    "index": chunk.tool_index,
                    "delta": new,
                    "field": field,
                    "agent_view_id": self._loop._current_agent_view_id(),
                }
            )

        return _hook

    async def _pause_driver_unavailable(self, cause: LLMError | None = None) -> tuple[None, Disp]:
        """Explain a driver outage and HALT at the user-question gate.
        Called from both the LLMProviderUnavailable and LLMTransientError
        exhaustion paths to keep drive_step within its LOC budget.

        `cause` (when supplied) is labeled into the landing events' non-semantic
        `meta` channel so the UI can say WHY the driver stayed unavailable (e.g.
        a provider 429 usage limit) instead of a generic pause. The reason,
        guidance, statuses, and model-visible text are byte-identical either
        way — meta never enters the model view or any loop/oracle signal."""
        extra_meta: dict[str, str | int] | None = None
        if cause is not None:
            # str(cause) is the provider adapter's SAFE summary (never the raw
            # body) — e.g. "provider opencode-go returned HTTP 429 type=…".
            extra_meta = {
                "driver_error": str(cause),
                "driver_error_kind": type(cause).__name__,
            }
            if cause.provider:
                extra_meta["driver_error_provider"] = cause.provider
            if isinstance(cause, LLMTransientError) and cause.http_status is not None:
                extra_meta["driver_error_http_status"] = cause.http_status
        await self._loop._land_blocked(
            reason="driver-unavailable",
            guidance=(
                "The model driver stayed unavailable after the bounded provider "
                "retry path was exhausted."
            ),
            legacy_status=ConversationStatus.PAUSED,
            legacy_detail="driver-unavailable",
            extra_meta=extra_meta,
        )
        return None, Disp.HALT

    def readonly_tool_names(self) -> frozenset[str] | None:
        """The executor's set of read-only tool names, or None if this executor
        can't report it (older/fake executors). None → the capability backstop
        is skipped and only the name allowlist governs (legacy behavior); a real
        DefaultToolExecutor always reports, so the backstop is live in prod."""
        fn = getattr(self._loop.executor, "readonly_tool_names", None)
        if fn is None:
            return None
        try:
            return frozenset(fn())
        except Exception:  # noqa: BLE001 — never let tool-listing crash the loop
            return None

    def _available_tools(self, available_tools: list | None) -> list:
        """Use one captured tool snapshot when the caller provides it."""
        if available_tools is not None:
            return available_tools
        return self._loop.executor.available_tools()

    def planning_allowed_tool_names(self, available_tools: list | None = None) -> frozenset[str]:
        """The names a tool call may legitimately carry while in PLANNING mode —
        the SAME read-only-capability ∩ name-allowlist intersection that
        `tools_for_step()` uses to decide tool VISIBILITY, so the execute-time
        phase gate (`engine._gate_planning_mode`) and the advertise-time filter
        can never drift. A model can only be offered, and only run, this set
        before its plan is approved.

        Composed of:
          - executor tools that pass the planning filter: read-only per the
            capability backstop (when the executor reports it) AND within the
            operator's `_planning_tools` name allowlist (when configured) — the
            exact `_planner_ok` predicate from `tools_for_step()`;
          - the plan tool itself (loop-intercepted, never executed) — always
            allowed even if the executor doesn't advertise it or `_planning_tools`
            omits it;
          - the virtual ask_user / questions_v2 / clarify escape hatches — ALWAYS permitted
            (even in autonomous mode, where they are withheld from *advertisement*
            so the planner doesn't stall on a human): a hallucinated ask/intake
            call must reach its downstream handler / the autonomous-stall guard,
            not be rejected by this gate.
        """
        readonly = self.readonly_tool_names()  # frozenset | None (None=unknown)
        allow = self._loop._planning_tools

        def _planner_ok(name: str | None) -> bool:
            # Identical predicate to the closure in tools_for_step()'s PLANNING
            # branch — keep them in lockstep.
            if name is None:
                return False
            if readonly is not None and name not in readonly:
                return False  # capability backstop — never a mutating tool
            if allow:
                return name in allow  # allowlist restricts further
            return True

        names: set[str] = set()
        tools = self._available_tools(available_tools)
        for t in tools:
            name = getattr(t, "name", None)
            if isinstance(name, str) and _planner_ok(name):
                names.add(name)
        names.add(self._loop._plan_tool)  # submit_plan — always intercepted
        names.update({"ask_user", "questions_v2", "clarify"})  # virtual escape hatches
        return frozenset(names)

    def force_submit_read_calls_remaining(self) -> int:
        return max(
            0,
            (_PLAN_EXPLORE_READ_CAP + _FORCE_SUBMIT_READ_GRACE) - self._loop._plan_explore_reads,
        )

    def tools_for_step(
        self,
        *,
        suppress_meta_tools: bool = False,
        force_submit_only: bool = False,
        force_read_tools: frozenset[str] | None = None,
        blocked_tools: frozenset[str] = frozenset(),
        mode: OperatingMode | None = None,
        available_tools: list | None = None,
    ) -> list:
        """Mode-scoped tool visibility. With no planning_tools configured this is a
        pass-through (Research / default). While PLANNING the agent sees ONLY the
        planning tool(s); while executing it sees everything else.

        suppress_meta_tools (Phase-B re-runs #4/#5, 2026-06-10): until the
        first real action of a session (post-resume or conversation start),
        ALL virtuals except finish — notify_user, remember, serve, ask_user,
        propose_plan_update — are WITHHELD from the offered set. FOUR
        distinct post-resume degenerations in a row (prose spam,
        serve-before-work, remember spam, ask_user-as-narration) each
        escaped through whatever meta channel remained; shaping the action
        space beats refusing after the fact (the model can't pick what
        isn't offered). The first turn of a session must be a real tool
        call. Asking/re-planning become available after one real attempt —
        an evidence-backed question beats a preemptive one. finish stays
        (verify-on-finish gates it).

        In execution mode the loop also appends a VIRTUAL `ask_user` tool — a
        clean escape hatch the model can call when it (in its own reasoning)
        decides it needs human input. The loop intercepts the call (the tool
        is never executed by the executor); see the ASK-USER GATE in the run
        loop. This is the Claude Code pattern: the tool is *available*, the
        model *discovers and chooses* it, the harness does not nudge it."""
        # Engine-local tool-spec singletons; imported lazily to avoid a
        # module-load circular import (engine imports this module).
        from .engine import (
            _delegate_explore_tool_singleton,
            _finish_alias_tool_spec,
            _finish_tool_singleton,
            _remember_tool_singleton,
            _serve_tool_singleton,
            _workflow_finish_tool_spec,
        )

        effective_mode = mode or self._loop.mode
        tools = [
            tool
            for tool in self._available_tools(available_tools)
            if getattr(tool, "name", None) not in blocked_tools
        ]
        if effective_mode == OperatingMode.PLANNING:
            # FORCED-SUBMIT RECOVERY (prose planning). When the engine has escalated a
            # stuck prose-planning segment, narrow the offered
            # tools to submit_plan + READ tools (file_read/file_list) — NOT submit-only.
            # ROOT (proven live, ~30% reproduction): a model re-planning a revision often
            # wants to file_read the current files to ground the diff BEFORE submitting
            # ("let me re-read the existing files, then submit"). Narrowing to submit_plan
            # ONLY left that model no legal move (its file_read was rejected → no progress →
            # actionless valve → killed). Keep reads so it can ground, then submit. A few
            # grounding reads are bounded by the read grace below; the prose-narration case
            # force_submit also targets is a TOOL-LESS turn, still caught by the valve.
            if force_submit_only and self._loop._plan_tool is not None:
                plan_name = getattr(self._loop._plan_tool, "name", self._loop._plan_tool)
                # Read grace: allow grounding reads until the read counter exceeds the cap
                # by _FORCE_SUBMIT_READ_GRACE, THEN collapse to submit-only so a read loop
                # can't run to the iteration hard cap.
                reads_allowed = self.force_submit_read_calls_remaining() > 0
                readonly_names = self.readonly_tool_names()
                force_readonly: frozenset[str] = (
                    readonly_names if readonly_names is not None else frozenset()
                )
                planning_tools = self._loop._planning_tools
                if planning_tools:
                    # Keep allowlist semantics; never widen beyond read-only tools.
                    force_readonly = force_readonly & planning_tools
                if force_read_tools is not None:
                    force_readonly = force_readonly & force_read_tools

                def _force_keep(name: str | None) -> bool:
                    if name == plan_name:
                        return True
                    if name is None:
                        return False
                    return reads_allowed and name in force_readonly

                narrowed = [t for t in tools if _force_keep(getattr(t, "name", None))]
                if narrowed:
                    return narrowed
                # Plan tool object not in the available set (shouldn't happen in the
                # build flow) — fall through to normal planning tools rather than
                # strand the model with zero tools.
            # The PLANNING agent is READ-ONLY (Claude-Code plan-mode parity): it
            # gathers context and proposes a plan; writes/exec are off the table
            # until approval. TWO independent, fail-safe guards:
            #   (1) capability backstop (ALWAYS, when the executor can report it):
            #       drop any tool not marked read_only — so even a misconfigured
            #       name allowlist that names a write tool can't leak it.
            #   (2) name allowlist (when configured): restrict further to the
            #       operator's curated set.
            readonly = self.readonly_tool_names()  # frozenset | None (None=unknown)
            allow = self._loop._planning_tools

            def _planner_ok(name: str | None) -> bool:
                if readonly is not None and name not in readonly:
                    return False  # capability backstop — never a mutating tool
                if allow:
                    return name in allow  # allowlist restricts further
                return True

            planner_tools = [t for t in tools if _planner_ok(getattr(t, "name", None))]
            # Append the VIRTUAL ask_user + questions_v2 + clarify even while
            # planning: an under-specified task most needs clarification BEFORE a
            # plan is committed (the user named a detail only they know).
            # ask_user is read-only-safe — the loop
            # intercepts it (never executes it against the sandbox) and halts at the
            # Ask-gate, same as in execution. questions_v2 is the structured §K
            # batch-intake variant; clarify stays as a legacy alias. Without this
            # the planner is forced to guess and bury the unknown in the plan.
            #
            # C20 — `delegate_explore` is intentionally ABSENT from the planning
            # tool set. It is an EXECUTION-only tool: the call DISPATCHES a
            # subagent (an action that yields an observation, not a pure
            # read). The planner gathers context with the read-only tools it
            # already has (file_read, file_list, search, extract) and proposes
            # a plan; the fan-out helper is available to the driver in
            # execution mode only. Belt-and-suspenders: the tool def is also
            # `read_only=False`, so even a misconfigured readonly backstop
            # would exclude it from the planning branch.
            if self._loop._autonomous:
                # Autonomous/headless: WITHHOLD the ask gates — there is no human
                # to answer, so the autonomous planner proceeds with the
                # documented "assume + proceed" default instead of stalling on an
                # ask. (Mirrors the execution-branch withholding at the
                # `if not self._autonomous` gate below; required by
                # test_autonomous_withholds_ask_gates_in_planning.)
                return planner_tools
            return planner_tools + [
                _ask_user_tool_singleton(),
                _questions_v2_tool_singleton(),
                _clarify_tool_singleton(),
            ]
        if self._loop._planning_tools:
            # `_planning_tools` is the PLANNING allowlist, not a declaration that
            # every member is planning-only. Production includes dual-use reads
            # and `think` there; removing the whole set after approval silently
            # stripped the exact exploration tools both execution prompts direct.
            # Withhold only the intercepted plan-submission signal. Planning-only
            # ask/intake virtuals are assembled separately below and therefore
            # cannot leak through this executor-tool filter.
            plan_name = getattr(self._loop._plan_tool, "name", self._loop._plan_tool)
            tools = [t for t in tools if getattr(t, "name", None) != plan_name]
        # Append the virtual ask_user + clarify + propose_plan_update tools in execution
        # mode. All are documented so the model decides WHEN to use them;
        # neither is injected by reminder. propose_plan_update is the model's
        # auto-recovery affordance: when its current plan is wrong, it proposes
        # a revision and the user accepts/refines via the plan-approval gate.
        workflow_run = getattr(self._loop, "_workflow_run", None)
        if workflow_run is not None:
            virtuals = [_workflow_finish_tool_spec(workflow_run)]
        else:
            virtuals = [_finish_tool_singleton()]
        # P6 — when a Build contract governs the run, ALSO advertise its verification
        # finalizer (ready_for_*_verification) as a per-kind alias of `finish` (the name
        # the prompt pack instructs the model to call). Advertised next to plain `finish`
        # (kept for compatibility) and, like `finish`, survives meta-tool suppression
        # since both route to the same host-truth finish gate.
        if self._loop._finish_alias:
            if workflow_run is not None:
                virtuals.append(
                    _workflow_finish_tool_spec(
                        workflow_run,
                        name=self._loop._finish_alias,
                    )
                )
            else:
                virtuals.append(_finish_alias_tool_spec(self._loop._finish_alias))
        if not suppress_meta_tools:
            # Autonomous mode withholds the ask gates (no human to answer); the model
            # is told to assume + proceed. The rest stay — propose_plan_update is the
            # model's self-correction affordance (auto-approved when autonomous).
            if not self._loop._autonomous:
                virtuals += [_ask_user_tool_singleton(), _clarify_tool_singleton()]
            virtuals += [
                _propose_plan_update_tool_singleton(),
                _notify_user_tool_singleton(),
                _remember_tool_singleton(),
                _serve_tool_singleton(),
                # C20 — read-only Explore/Plan helper dispatch+join. Available
                # in execution mode ONLY (the PLANNING branch of
                # _tools_for_step intentionally does NOT append it — see the
                # PLANNING branch for the rationale; `delegate_explore` is an
                # EXECUTION-only tool because the call dispatches a subagent
                # and is therefore an ACTION, not a pure read). Cap is
                # enforced at the call site, not in the schema (a model that
                # calls it past the cap is refused with a system-reminder,
                # the same shape the (c.3) bookkeeping-stuck nudge uses).
                #
                # FALSE-AFFORDANCE GUARD (live-caught 2026-07-03): the default
                # `_run_fanout` is a STUB that acks "helper dispatched" and
                # returns nothing — a model that leans on it (MiniMax did, in
                # strict AppKit mode) concludes the platform is broken and
                # aborts the build. Advertise the tool ONLY when the runtime
                # actually overrode the seam (instance-attribute override is
                # the documented hook — see AgentLoop._run_fanout).
                *(
                    [_delegate_explore_tool_singleton()]
                    if "_run_fanout" in vars(self._loop)
                    else []
                ),
            ]
        return [
            tool
            for tool in list(tools) + virtuals
            if getattr(tool, "name", None) not in blocked_tools
        ]

    def _executor_callable_tool_names(self) -> set[str]:
        _cn = getattr(self._loop.executor, "callable_tool_names", None)
        if callable(_cn):
            # Duck-typed: executors exposing callable_tool_names return an
            # iterable of tool-name strings (frozenset[str] on the real backend).
            return set(cast("Iterable[str]", _cn()))
        return {t.name for t in self._loop.executor.available_tools()}

    def _virtual_tool_names(self) -> set[str]:
        virtual_names = {
            "ask_user",
            "questions_v2",
            "clarify",
            "propose_plan_update",
            "plan_step",
            "notify_user",
            "finish",
            "remember",
            "serve",
            self._loop._plan_tool,
            # C20 — `delegate_explore`: read-only
            # Explore/Plan helper. Listed in the
            # known-names set so the Rung 7 requery
            # doesn't bounce a valid fan-out call.
            # The cap is enforced at the call site,
            # NOT via schema suppression.
            "delegate_explore",
        }
        # P6 — the contract finalizer alias is a recognized finish signal, so a call to
        # it must not be bounced as unknown by the Rung-7 invalid-tool requery.
        if self._loop._finish_alias:
            virtual_names.add(self._loop._finish_alias)
        # Include mode-scoped virtuals (planning tools) so
        # we don't requery for valid exploration turns.
        return virtual_names | set(self._loop._planning_tools)

    def known_tool_names_for_requery(self) -> set[str]:
        _kn = getattr(self._loop.executor, "known_tool_names_for_requery", None)
        known_tool_names = (
            set(cast("Iterable[str]", _kn()))
            if callable(_kn)
            else self._executor_callable_tool_names()
        )
        return known_tool_names | self._virtual_tool_names()

    def allowed_tool_names_for_mode(
        self,
        mode: OperatingMode,
        *,
        available_tools: list,
        blocked_tools: frozenset[str] = frozenset(),
    ) -> set[str]:
        """Names the loop would accept for the current model request.

        Planning uses the exact execute-time gate predicate.  Other modes use
        the executor's callable (not merely advertised) names plus the loop's
        virtual/intercepted names.  This intentionally does not use the broader
        requery-recognition set: a strict executor may recognize registered but
        scope-denied names so they receive one canonical denial, while inspect
        evidence must still report that those names are not callable.
        """
        if mode == OperatingMode.PLANNING:
            return set(self.planning_allowed_tool_names(available_tools)) - blocked_tools
        allowed = self._executor_callable_tool_names() | self._virtual_tool_names()
        plan_name = getattr(self._loop._plan_tool, "name", self._loop._plan_tool)
        # These signals are valid only before approval. Keep them in the broader
        # requery-recognition set so a hallucination gets its canonical typed
        # refusal, but do not report them as allowed execution capabilities.
        allowed.difference_update({plan_name, "questions_v2", "plan_step"})
        allowed.difference_update(blocked_tools)
        return allowed

    def unknown_tool_requery_hint(self, tool_name: str, offered_names: set[str]) -> str:
        _hint = f"ERROR: Unknown tool '{tool_name}'. Available: {sorted(list(offered_names))}"
        if self._loop._assist:
            _suggestion = _nearest_tool_name(tool_name, offered_names)
            if _suggestion is not None:
                _hint = f"{_hint} did you mean '{_suggestion}'?"
        return _hint

    def _prepare_drive_context(
        self, view: View, events: list[Event]
    ) -> tuple[
        OperatingMode,
        float | None,
        bool,
        bool,
        frozenset[str] | None,
        frozenset[str],
    ]:
        """Compute the request-context invariants for a single drive step.

        Returns ``(mode, escape_temp, fresh_session, force_submit_only,
        force_read_tools, escape_blocked_tools)``. Kept inline with
        drive_step's original logic so retry counters, event ordering, and
        model requests are unchanged outside the typed escape turn.
        """
        escape_seq = signals.stuck_escape_seq(events)
        recovered_since_escape = escape_seq is not None and self._trusted_receipt_since(
            events, escape_seq
        )
        # H533: a read-only bridge (notably an unchanged file_list) is not a
        # recovery from the read loop that created this quarantine. Keep the
        # bypass aliases withheld until the environment confirms a
        # receipt-backed mutation; failed mutations and inspection-only actions
        # may not launder the escape state and silently re-enable them.
        in_escape = escape_seq is not None and not recovered_since_escape
        escape_temp = _STUCK_ESCAPE_TEMP if in_escape else None
        escape_blocked_tools = self.stuck_escape_blocked_tools_for_step(events)

        # K4 shadow: reduce the same durable event log the legacy recovery gates
        # consume, but publish bounded inspect telemetry only. It cannot alter
        # tool visibility, temperature, retries, status, or any persisted event.
        if inspect_enabled():
            try:
                progress = reduce_progress(events)
                record_progress_shadow(
                    self._loop.conversation_id,
                    latest_event_seq=progress.latest_event_seq,
                    last_progress_seq=progress.last_progress_seq,
                    evidence_fingerprint=progress.evidence_fingerprint,
                    progress_kinds=sorted(kind.value for kind in progress.progress_kinds),
                    current_resource_count=len(progress.current_resources),
                    observation_count=len(progress.observations),
                    mutation_count=len(progress.mutations),
                    verification_count=len(progress.verifications),
                    executed_invocations=progress.executed_invocations,
                    zero_progress_invocations=progress.zero_progress_invocations,
                    unattributed_invocations=progress.unattributed_invocations,
                    invalid_event_pairs=progress.invalid_event_pairs,
                    invalid_receipt_groups=progress.invalid_receipt_groups,
                    stale_receipts=progress.stale_receipts,
                    recovery_lease_phase=(
                        progress.recovery_lease.phase.value
                        if progress.recovery_lease is not None
                        else None
                    ),
                    recovery_candidate_capability=(
                        progress.recovery_candidate.blocked_capability.value
                        if progress.recovery_candidate is not None
                        else None
                    ),
                    recovery_candidate_streak=(
                        progress.recovery_candidate.zero_progress_streak
                        if progress.recovery_candidate is not None
                        else 0
                    ),
                    recovery_comparable=progress.recovery_comparable,
                    invalid_log_order=progress.invalid_log_order,
                    legacy_escape_active=in_escape,
                )
            except Exception:  # noqa: BLE001 — shadow telemetry never changes execution
                _LOG.exception("progress reducer shadow failed for %s", self._loop.conversation_id)

        cached_mode = self._loop.mode
        mode = self._loop._reconcile_mode_from_events(events)
        if (
            cached_mode == OperatingMode.PLANNING
            and mode != OperatingMode.PLANNING
            and _view_has_current_objective(view)
        ):
            _LOG.error(
                "Mode desync corrected for %s at composition: cached=%s effective=%s",
                self._loop.conversation_id,
                cached_mode.value,
                mode.value,
            )

        fresh_session = (
            mode != OperatingMode.PLANNING and signals.actions_since_last_resume(events) == 0
        )

        planning_refusal_force = (
            mode == OperatingMode.PLANNING
            and planning_tool_refusal_streak(events) >= _PLANNING_TOOL_REFUSAL_NARROW_AT
        )
        force_submit_only = mode == OperatingMode.PLANNING and (
            signals.prose_plan_force_submit(events) or planning_refusal_force
        )
        force_read_tools = _PLANNING_TOOL_REFUSAL_READ_TOOLS if planning_refusal_force else None
        return (
            mode,
            escape_temp,
            fresh_session,
            force_submit_only,
            force_read_tools,
            escape_blocked_tools,
        )

    def stuck_escape_blocked_tools_for_step(self, events: list[Event]) -> frozenset[str]:
        """Schema-level tool boundary for the current read-loop recovery episode.

        Only the read-BYPASS aliases (general shell/code execution and delegated
        exploration) are schema-withheld, and only while the episode has seen
        ZERO trusted changed-state receipts: in that no-progress window they
        could reread the same bytes under another name without yielding a typed
        read receipt (H583).  ``file_read`` itself is NEVER schema-withheld —
        the k6g 128k canary proved tool-level quarantine convicts legitimate
        different-resource reads (finding F1).  Redundant reads are refused
        per-call by :meth:`stuck_escape_redundant_read_refusal` using canonical
        resource identity, requested range, and content digests instead of the
        tool name.  A user turn, recovery boundary, or typed blocking obligation
        ends the episode and restores the ordinary surface.
        """
        escape_seq = signals.stuck_escape_seq(events)
        if escape_seq is None:
            return frozenset()
        if "file_read" not in signals.stuck_escape_blocked_tools(events):
            return frozenset()
        if self._trusted_receipt_since(events, escape_seq):
            return frozenset()
        return _STUCK_ESCAPE_EQUIVALENT_READ_BYPASS_TOOLS

    @staticmethod
    def _trusted_receipt_since(events: list[Event], boundary_seq: int) -> bool:
        """Whether any paired trusted changed-state receipt landed after *boundary_seq*."""

        actions = {
            event.id: event
            for event in events
            if isinstance(event, ActionEvent) and event.tool_call is not None
        }
        for event in events:
            if (
                not isinstance(event, ObservationEvent)
                or event.source != EventSource.ENVIRONMENT
                or event.seq is None
                or event.seq <= boundary_seq
            ):
                continue
            action = actions.get(event.action_id or "")
            if (
                action is None
                or action.source != EventSource.AGENT
                or action.seq is None
                or not boundary_seq < action.seq < event.seq
                or action.tool_call is None
                or event.tool_result.call_id != action.tool_call.call_id
            ):
                continue
            if successful_mutation_with_receipt(event, action):
                return True
        return False

    def stuck_escape_redundant_read_refusal(
        self, events: list[Event], tool_call: ToolCall
    ) -> dict[str, object] | None:
        """Refusal facts when a read is PROVABLY redundant in the active episode.

        The decision uses the host's own typed evidence — canonical resource
        identity, content digest, and exact line-span coverage from
        ``file_read``'s ObservationReceipts — never the tool name:

        - no active recovery episode → never refused;
        - a different resource, an unknown resource, or a resource with no
          receipt-proven coverage → allowed;
        - a resource whose digest changed (trusted MutationReceipt) since its
          coverage was recorded → allowed (mutation-then-reread is progress);
        - any trusted changed-state receipt WITHOUT exact resource attribution
          (an opaque broad mutator) after the coverage → allowed, fail-open:
          possibly-changed bytes are never "already known";
        - coverage delivered by an observation that condensation has since
          FORGOTTEN is not knowledge the model still holds → allowed;
        - a requested range containing any line outside the receipt-proven
          coverage of the CURRENT digest → allowed (pagination stays possible);
        - only a request whose every returned line is provably already in the
          model's visible context, byte-identical, is refused.
        """

        escape_seq = signals.stuck_escape_seq(events)
        if escape_seq is None or tool_call.tool_name != "file_read":
            return None
        if "file_read" not in signals.stuck_escape_blocked_tools(events):
            return None
        path = signals.normalized_workspace_read_path(tool_call.arguments.get("path"))
        if path is None:
            return None

        forgotten: list[tuple[int, int]] = [
            (event.forgotten_start_seq, event.forgotten_end_seq)
            for event in events
            if isinstance(event, CondensationEvent)
        ]

        def _visible(seq: int | None) -> bool:
            if seq is None:
                return False
            return all(not (start <= seq <= end) for start, end in forgotten)

        actions = {
            event.id: event
            for event in events
            if isinstance(event, ActionEvent) and event.tool_call is not None
        }
        # Latest authoritative digest per this resource + visible coverage per digest.
        latest_digest: str | None = None
        latest_digest_seq = -1
        last_path_mutation_seq = -1
        covered_by_digest: dict[str, set[tuple[int, int]]] = {}
        total_by_digest: dict[str, int | None] = {}
        coverage_seq_by_digest: dict[str, int] = {}
        opaque_mutation_seq = -1
        for event in events:
            if (
                not isinstance(event, ObservationEvent)
                or event.source != EventSource.ENVIRONMENT
                or event.seq is None
                or not event.tool_result.success
            ):
                continue
            action = actions.get(event.action_id or "")
            paired = (
                action is not None
                and action.source == EventSource.AGENT
                and action.tool_call is not None
                and event.tool_result.call_id == action.tool_call.call_id
            )
            if not paired:
                continue
            receipts = event.tool_result.effect_receipts
            named_mutation = False
            for receipt in receipts:
                if isinstance(receipt, MutationReceipt):
                    named_mutation = True
                    # Verifier V1/V5: a DELETION receipt (after=None) advances
                    # the revision too — coverage of a deleted revision proves
                    # nothing about a future read. Mutations win same-event
                    # ordering ties (>=) so an ObservationReceipt listed before
                    # the MutationReceipt in one observation cannot shadow the
                    # new digest.
                    if (
                        receipt.resource.namespace == "workspace.file"
                        and receipt.resource.identifier == path
                        and event.seq >= latest_digest_seq
                    ):
                        latest_digest = receipt.after.digest if receipt.after is not None else None
                        latest_digest_seq = event.seq
                        last_path_mutation_seq = event.seq
                elif (
                    isinstance(receipt, ObservationReceipt)
                    and receipt.capability is EffectCapability.WORKSPACE_CONTENT_READ
                    and receipt.revision.resource.namespace == "workspace.file"
                    and receipt.revision.resource.identifier == path
                ):
                    digest = receipt.revision.digest
                    if event.seq > latest_digest_seq:
                        latest_digest = digest
                        latest_digest_seq = event.seq
                    if not _visible(event.seq):
                        continue
                    spans = covered_by_digest.setdefault(digest, set())
                    if receipt.complete and receipt.coverage.total is not None:
                        spans.add((0, receipt.coverage.total))
                    for span in receipt.coverage.spans:
                        spans.add((span.start, span.end))
                    if receipt.coverage.total is not None:
                        total_by_digest[digest] = receipt.coverage.total
                    coverage_seq_by_digest[digest] = max(
                        coverage_seq_by_digest.get(digest, -1), event.seq
                    )
            if (
                not named_mutation
                and successful_mutation_with_receipt(event, action)
                and event.seq > opaque_mutation_seq
            ):
                opaque_mutation_seq = event.seq

        if latest_digest is None:
            return None
        covered = covered_by_digest.get(latest_digest)
        total = total_by_digest.get(latest_digest)
        if not covered or total is None:
            return None
        if opaque_mutation_seq > coverage_seq_by_digest.get(latest_digest, -1):
            # An unattributed broad mutator ran after this coverage was recorded;
            # the bytes may have changed, so the read is not provably redundant.
            return None

        raw_offset = tool_call.arguments.get("offset")
        raw_limit = tool_call.arguments.get("limit")
        offset = raw_offset if type(raw_offset) is int and raw_offset >= 1 else 1
        limit = raw_limit if type(raw_limit) is int and raw_limit >= 1 else None
        req_start = offset - 1
        req_end = total if limit is None else min(total, req_start + limit)
        if req_start < total:
            merged = sorted(covered)
            cursor = req_start
            for start, end in merged:
                if start > cursor:
                    break
                cursor = max(cursor, end)
                if cursor >= req_end:
                    break
            if cursor < req_end:
                return None
        held = merge_spans(tuple(CoverageSpan(start=s, end=e) for s, e in sorted(covered)))
        return {
            "path": path,
            "requested_start_line": min(offset, total + 1),
            # Verifier N1: a past-EOF probe must not render an inverted range.
            "requested_end_line": max(req_end, min(offset, total + 1)),
            "total_lines": total,
            "held_spans": [(span.start + 1, span.end) for span in held],
            "digest_prefix": latest_digest[:12],
            # Verifier V2: the halt bound counts refusals only after the latest
            # trusted progress on THIS resource (the invariant's fourth
            # episode-advancing clause) — a refusal before a mutation of the
            # loop resource and one after it are two independent recoveries.
            "refusal_floor_seq": max(escape_seq, last_path_mutation_seq, opaque_mutation_seq),
        }

    async def _repair_degenerate_step(
        self,
        step: AgentStep,
        *,
        mode: OperatingMode,
        events: list[Event],
        transient_messages: list[LLMMessage],
        empty_reasoning_repair_count: int,
        prose_noop_repair_count: int,
    ) -> tuple[bool, list[LLMMessage], int, int]:
        """Handle empty-reasoning and prose-noop degenerate steps.

        Emits diagnostics and appends transient user reminders exactly as the
        inline code did, preserving counter limits and event order. Returns
        ``(should_continue, transient_messages, empty_reasoning_repair_count,
        prose_noop_repair_count)``.
        """
        if step.empty_reasoning_diagnostic is not None:
            await self._persist_empty_reasoning_diagnostic(step.empty_reasoning_diagnostic)
            if empty_reasoning_repair_count < 1:
                self._record_model_repair(
                    "empty_reasoning", attempt=empty_reasoning_repair_count + 1
                )
                return (
                    True,
                    transient_messages
                    + [LLMMessage(role="user", content=_EMPTY_REASONING_REPAIR_REMINDER)],
                    empty_reasoning_repair_count + 1,
                    prose_noop_repair_count,
                )

        if (
            mode != OperatingMode.PLANNING
            and step.tool_call is None
            and not step.finished
            and not step.truncated
            and step.thought.strip()
            and prose_noop_repair_count < 1
            and not signals.prose_noop_repair_seen_current_execution_segment(events)
        ):
            await self._persist_prose_noop_diagnostic(step)
            self._record_model_repair("prose_without_action", attempt=prose_noop_repair_count + 1)
            return (
                True,
                transient_messages + [LLMMessage(role="user", content=_PROSE_NOOP_REPAIR_REMINDER)],
                empty_reasoning_repair_count,
                prose_noop_repair_count + 1,
            )

        return False, transient_messages, empty_reasoning_repair_count, prose_noop_repair_count

    async def _try_request_budget_preview(
        self,
        view: View,
        events: list[Event],
        mode,
        escape_temp,
        initial_offered_tools: list,
    ) -> Disp | None:
        agent = self._loop.agent
        preview_fn = getattr(agent, "request_budget_preview", None)
        if not callable(preview_fn):
            self._log_budget_event(outcome="unavailable")
            return None

        try:
            overflow = view_render.overflow_signal(events)
            preview = cast(
                RequestBudgetEstimate | None,
                preview_fn(
                    view,
                    initial_offered_tools,
                    mode=mode,
                    overflow_signal=overflow,
                    temperature=escape_temp,
                    assist=self._loop._assist,
                ),
            )
        except Exception:
            self._log_budget_event(outcome="unavailable")
            return None
        if preview is None:
            self._log_budget_event(outcome="unavailable")
            return None

        p = preview  # narrowed to non-None

        # --- should_condense (fail-open: unavailable → normal provider call) ---
        try:
            req = self._loop.condenser.should_condense(view, token_count=p.pressure_tokens)
        except Exception:
            self._log_budget_event(outcome="unavailable", estimate=p)
            return None

        if req is None:
            self._log_budget_event(outcome="within_budget", estimate=p)
            return None

        pressure_kind: Literal["soft", "hard"] = "soft" if req.soft else "hard"

        # --- context pack (fail-open: try condenser fallback) ---
        try:
            if context_pack_enabled():
                policy = CompactionPolicy.default()
                snips = context_compact_if_needed(
                    events,
                    policy,
                    protected_seqs=protected_context_compaction_seqs(events),
                    pressure_chars=p.canonical_payload_bytes,
                )
            else:
                snips = None
        except Exception:
            snips = None
        if snips:
            for snip in snips:
                await self._loop._assert_current_agent_view()
                await self._loop._emit(snip)
            self._log_budget_event(
                outcome="compacted",
                estimate=p,
                method="context_pack",
                kind=pressure_kind,
            )
            return Disp.CONTINUE

        # --- condenser (fail-open: no_progress → normal provider call) ---
        await self._loop._assert_current_agent_view()
        try:
            view_of_events = View.of(events)
            tombstone = await self._loop.condenser.condense(
                events, view_of_events, summarizer=self._loop.summarizer
            )
        except Exception:
            self._log_budget_event(outcome="no_progress", estimate=p, kind=pressure_kind)
            return None

        if tombstone is not None:
            await self._loop._assert_current_agent_view()
            await self._loop._emit(tombstone)
            self._log_budget_event(
                outcome="compacted",
                estimate=p,
                method="condenser",
                kind=pressure_kind,
            )
            return Disp.CONTINUE

        self._log_budget_event(outcome="no_progress", estimate=p, kind=pressure_kind)
        return None

    def _log_budget_event(
        self,
        *,
        outcome: Literal["unavailable", "within_budget", "compacted", "no_progress"],
        estimate: RequestBudgetEstimate | None = None,
        kind: Literal["soft", "hard"] | None = None,
        method: Literal["context_pack", "condenser"] | None = None,
    ) -> None:
        try:
            if not inspect_enabled():
                return
            fields: dict[str, object] = {
                "cid": self._loop.conversation_id,
                "outcome": outcome,
            }
            if estimate is not None:
                fields["driver_context_window"] = estimate.driver_context_window
                fields["max_output_tokens"] = estimate.max_output_tokens
                fields["canonical_payload_bytes"] = estimate.canonical_payload_bytes
                fields["messages_json_bytes"] = estimate.messages_json_bytes
                fields["tools_json_bytes"] = estimate.tools_json_bytes
                fields["estimated_input_tokens"] = estimate.estimated_input_tokens
                fields["pressure_tokens"] = estimate.pressure_tokens
                fields["message_count"] = estimate.message_count
                fields["tool_count"] = estimate.tool_count
            if kind is not None:
                fields["kind"] = kind
            if method is not None:
                fields["method"] = method
            log_event("request_budget.preview", **fields)
        except Exception:
            pass

    async def drive_step(self, view: View, events: list[Event]) -> tuple[AgentStep | None, Disp]:
        # (e) ask the agent for ONE action (principle 1). The visible tool
        # set is mode-scoped: while PLANNING the agent sees ONLY the plan
        # tool (so it can't act before approval); while executing it sees
        # everything except the plan tool.
        (
            mode,
            escape_temp,
            fresh_session,
            force_submit_only,
            force_read_tools,
            escape_blocked_tools,
        ) = self._prepare_drive_context(view, events)

        # Compute offered tools exactly ONCE after _prepare_drive_context.
        # The preview and the first actual agent.step must share the same
        # list object so equality is guaranteed (modulo the request id).
        initial_available_tools = self._loop.executor.available_tools()
        initial_offered_tools = self.tools_for_step(
            suppress_meta_tools=fresh_session,
            force_submit_only=force_submit_only,
            force_read_tools=force_read_tools,
            blocked_tools=escape_blocked_tools,
            mode=mode,
            available_tools=initial_available_tools,
        )

        # --- request budget preview (at most once per step, before the retry loop) ---
        preview_disp = await self._try_request_budget_preview(
            view,
            events,
            mode,
            escape_temp,
            initial_offered_tools,
        )
        if preview_disp is not None:
            return None, preview_disp

        try:
            attempts = 0
            first_agent_invocation = True
            requery_count = 0
            provider_retry_count = 0
            protocol_repair_count = 0
            empty_reasoning_repair_count = 0
            prose_noop_repair_count = 0
            transient_messages: list[LLMMessage] = []
            repaired_view: View | None = None
            while True:
                current_view = repaired_view or view
                try:
                    if transient_messages:
                        current_view = current_view.model_copy(
                            update={"messages": current_view.messages + transient_messages}
                        )

                    if first_agent_invocation:
                        available_tools = initial_available_tools
                        offered_tools = initial_offered_tools
                    else:
                        available_tools = self._loop.executor.available_tools()
                        offered_tools = self.tools_for_step(
                            suppress_meta_tools=fresh_session,
                            force_submit_only=force_submit_only,
                            force_read_tools=force_read_tools,
                            blocked_tools=escape_blocked_tools,
                            mode=mode,
                            available_tools=available_tools,
                        )
                    if inspect_enabled():
                        record_tool_scope(
                            self._loop.conversation_id,
                            mode=mode.value,
                            offered_tools={
                                name
                                for tool in offered_tools
                                if isinstance((name := getattr(tool, "name", None)), str)
                            },
                            allowed_tools=self.allowed_tool_names_for_mode(
                                mode,
                                available_tools=available_tools,
                                blocked_tools=escape_blocked_tools,
                            ),
                            attempt=attempts + 1,
                        )
                    await self._loop._assert_current_agent_view()
                    first_agent_invocation = False
                    step = await self._loop.agent.step(
                        current_view,
                        offered_tools,
                        mode=mode,
                        overflow_signal=view_render.overflow_signal(events),
                        on_stream=self.build_stream_hook(),
                        temperature=escape_temp,
                        assist=self._loop._assist,
                        # F5: thread the repair-attempt counter (1 = first
                        # try; incremented on transient-retry / requery).
                        # The OpenAI provider reads `req.attempt >= 2` to
                        # force `enable_thinking=False`. assist-OFF is
                        # untouched (the provider's gate ignores the field
                        # when req.assist is False).
                        attempt=attempts + 1,
                        # P2: escalated provider prefs on routing retries.
                        # None on the first/normal call; populated after the
                        # first LLMProviderUnavailable so the NEXT step
                        # carries a steering hint to OpenRouter.
                        provider_prefs=(
                            _escalated_provider_prefs(provider_retry_count)
                            if provider_retry_count > 0
                            else None
                        ),
                    )
                    await self._loop._assert_current_agent_view()

                    (
                        should_continue,
                        transient_messages,
                        empty_reasoning_repair_count,
                        prose_noop_repair_count,
                    ) = await self._repair_degenerate_step(
                        step,
                        mode=mode,
                        events=events,
                        transient_messages=transient_messages,
                        empty_reasoning_repair_count=empty_reasoning_repair_count,
                        prose_noop_repair_count=prose_noop_repair_count,
                    )
                    if should_continue:
                        continue

                    # Rung 7: Invalid-tool reroute (weak-model FC kit).
                    # Valid JSON but unknown tool name -> if we haven't
                    # hit the requery bound, inject a hint and retry
                    # without persisting the failure to the store.
                    # The requery applies ONLY to names absent from the
                    # FULL tool registry (truly unknown), never to
                    # known-but-currently-withheld tools.
                    next_requery_count = _prepare_unknown_tool_requery(
                        self,
                        step,
                        requery_count,
                        transient_messages,
                        fresh_session=fresh_session,
                        force_submit_only=force_submit_only,
                        force_read_tools=force_read_tools,
                        blocked_tools=escape_blocked_tools,
                        mode=mode,
                    )
                    if next_requery_count is not None:
                        requery_count = next_requery_count
                        continue

                    break  # Step is valid or requeries exhausted
                except LLMContextWindowExceeded:
                    raise  # handled by view-materialization hard-reset (§8)
                except LLMProviderUnavailable as e:
                    # P2 — provider-routing rejection (Chutes / no instances /
                    # no cookie auth). The model did nothing wrong; the upstream
                    # is unavailable or excluded. Re-issue with escalated
                    # provider_prefs (OpenRouter steering hint) instead of
                    # blaming the model. Capped at the existing requery budget
                    # (≤2); after the cap, fall through to PAUSE (a provider
                    # outage is recoverable on resume, not a fatal model error).
                    # IMPORTANT: this arm MUST precede `except LLMTransientError`
                    # because LLMProviderUnavailable IS a LLMTransientError subclass
                    # — Python matches in order; wrong order → wrong arm fires.
                    if provider_retry_count < 2:
                        provider_retry_count += 1
                        _LOG.warning(
                            f"Provider unavailable: {e}. Escalating provider_prefs "
                            f"(retry {provider_retry_count}/2)..."
                        )
                        # No transient hint appended — the model did nothing wrong.
                        continue
                    # Cap exhausted → PAUSE (a provider outage is recoverable).
                    return await self._pause_driver_unavailable(cause=e)
                except LLMTransientError as e:
                    if attempts < len(_DRIVER_RETRY_BACKOFFS_S):
                        self._record_model_repair("driver_transient_backoff", attempt=attempts + 1)
                        if not await self._wait_retry_backoff(_DRIVER_RETRY_BACKOFFS_S[attempts]):
                            self._record_model_repair(
                                "driver_transient_interrupted", attempt=attempts + 1
                            )
                            return None, Disp.CONTINUE
                        attempts += 1
                        continue
                    else:
                        self._record_model_repair(
                            "driver_transient_exhausted", attempt=attempts + 1
                        )
                        return await self._pause_driver_unavailable(cause=e)
                except LLMError as e:
                    if _is_tool_result_adjacency_protocol_error(e):
                        if protocol_repair_count < 1:
                            protocol_repair_count += 1
                            self._record_model_repair(
                                "tool_history_protocol", attempt=protocol_repair_count
                            )
                            repaired_messages = repair_tool_call_adjacency(current_view.messages)
                            repaired_view = current_view.model_copy(
                                update={"messages": repaired_messages}
                            )
                            transient_messages = []
                            _LOG.error(
                                "Provider rejected tool-call history ordering (%s); "
                                "retrying once with repaired history (%d -> %d messages)",
                                e,
                                len(current_view.messages),
                                len(repaired_messages),
                            )
                            continue
                        raise
                    # DEFECT-6: Provider 4xx "rejected request" must not be
                    # terminal; enter requery path with a hint.
                    # R2: but a TERMINAL provider error (bad/exhausted key, hit
                    # key/budget cap) can't be fixed by asking the model to
                    # rephrase — skip the requery and surface it immediately,
                    # so a capped OpenRouter key fails fast + honestly instead of
                    # wasting two more doomed round-trips.
                    if not isinstance(e, (LLMAuthError, BudgetExceeded)) and requery_count < 2:
                        requery_count += 1
                        self._record_model_repair(
                            "provider_rejected_request", attempt=requery_count
                        )
                        _LOG.warning(f"Provider rejected request: {e}, requerying...")
                        transient_messages.append(
                            LLMMessage(
                                role="user",
                                content=(
                                    f"The provider rejected the previous request: {e}. "
                                    "Please adjust your response (check tool names, "
                                    "JSON structure, or parameters) and try again."
                                ),
                            )
                        )
                        continue
                    raise
        except LLMContextWindowExceeded:
            if await self._loop._hard_reset(await self._loop._events()):
                return None, Disp.CONTINUE
            await self._loop._emit(
                ErrorEvent(code="context_window", detail="hard reset made no progress")
            )
            return None, Disp.HALT
        except LLMError as e:
            # REACTIVE error surfacing: the driver model's call failed (the
            # provider rejected the input, refused, auth/transient exhausted,
            # or the assignment was bad). Do NOT swallow it or flatten it into
            # a generic failure — surface the provider's real content to the
            # UI via ErrorEvent.detail (the agent server streams every event
            # to the client). This is conversation-fatal: the brain itself
            # failed, so there is no observation to feed back. The typed
            # classification (the exception class) is preserved in the detail.
            # R2: distinguish a TERMINAL auth/budget failure (bad/exhausted key,
            # hit key-limit/cost-cap) from a generic model error, so the UI can
            # show honest "check your key / credits / spend limit" guidance instead
            # of implying a transient glitch worth retrying.
            code = "auth_error" if isinstance(e, (LLMAuthError, BudgetExceeded)) else "model_error"
            await self._loop._emit(ErrorEvent(code=code, detail=_describe_llm_error(e)))
            return None, Disp.HALT
        return step, Disp.FALLTHROUGH


def _prepare_unknown_tool_requery(
    driver: Driver,
    step: AgentStep,
    requery_count: int,
    transient_messages: list[LLMMessage],
    *,
    fresh_session: bool,
    force_submit_only: bool,
    force_read_tools: frozenset[str] | None,
    blocked_tools: frozenset[str],
    mode: OperatingMode,
) -> int | None:
    """Append one balanced unknown-tool repair turn, if rerouting is safe."""
    tool_call = step.tool_call
    if tool_call is None or tool_call.tool_name in driver.known_tool_names_for_requery():
        return None
    gates_by_name = getattr(driver._loop.policy, "gates_by_name", None)
    if callable(gates_by_name) and gates_by_name(tool_call.tool_name):
        return None
    if requery_count >= 2:
        return None

    requery_count += 1
    driver._record_model_repair(
        "unknown_tool",
        attempt=requery_count,
        tool_name=tool_call.tool_name,
    )
    _LOG.info("Unknown tool %s, requerying...", tool_call.tool_name)
    offered_names = {
        tool.name
        for tool in driver.tools_for_step(
            suppress_meta_tools=fresh_session,
            force_submit_only=force_submit_only,
            force_read_tools=force_read_tools,
            blocked_tools=blocked_tools,
            mode=mode,
        )
    }
    transient_messages.append(
        LLMMessage(
            role="assistant",
            content=step.thought,
            tool_calls=[
                {
                    "id": tool_call.call_id,
                    "name": tool_call.tool_name,
                    "arguments": tool_call.arguments,
                }
            ],
        )
    )
    transient_messages.append(
        LLMMessage(
            role="user",
            content=driver.unknown_tool_requery_hint(tool_call.tool_name, offered_names),
        )
    )
    return requery_count
