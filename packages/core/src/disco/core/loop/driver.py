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
from typing import TYPE_CHECKING, cast

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from ..llm import (
    BudgetExceeded,
    LLMAuthError,
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
    OperatingMode,
)
from ..view import View, repair_tool_call_adjacency
from . import signals, view_render
from .boundaries import AgentStep
from .control import Disp
from .fc_kit import _nearest_tool_name
from .messages import _PLAN_EXPLORE_READ_CAP, _describe_llm_error
from .stream_extract import extract_partial_string_field
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
    return (
        isinstance(event, AgentErrorEvent)
        and _PLANNING_TOOL_REFUSAL_NEEDLE in event.error
    )


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
                }
            )

        return _hook

    async def _pause_driver_unavailable(self) -> tuple[None, Disp]:
        """Explain a driver outage and HALT at the user-question gate.
        Called from both the LLMProviderUnavailable and LLMTransientError
        exhaustion paths to keep drive_step within its LOC budget."""
        await self._loop._land_blocked(
            reason="driver-unavailable",
            guidance=(
                "The model driver stayed unavailable after the bounded provider "
                "retry path was exhausted."
            ),
            legacy_status=ConversationStatus.PAUSED,
            legacy_detail="driver-unavailable",
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

    def planning_allowed_tool_names(self) -> frozenset[str]:
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
        for t in self._loop.executor.available_tools():
            name = getattr(t, "name", None)
            if isinstance(name, str) and _planner_ok(name):
                names.add(name)
        names.add(self._loop._plan_tool)  # submit_plan — always intercepted
        names.update({"ask_user", "questions_v2", "clarify"})  # virtual escape hatches
        return frozenset(names)

    def force_submit_read_calls_remaining(self) -> int:
        return max(
            0,
            (_PLAN_EXPLORE_READ_CAP + _FORCE_SUBMIT_READ_GRACE)
            - self._loop._plan_explore_reads,
        )

    def tools_for_step(
        self,
        *,
        suppress_meta_tools: bool = False,
        force_submit_only: bool = False,
        force_read_tools: frozenset[str] | None = None,
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
        )

        tools = self._loop.executor.available_tools()
        if self._loop.mode == OperatingMode.PLANNING:
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
                plan_name = getattr(
                    self._loop._plan_tool, "name", self._loop._plan_tool
                )
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

                narrowed = [
                    t for t in tools if _force_keep(getattr(t, "name", None))
                ]
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
            # Append the VIRTUAL ask_user + questions_v2 + clarify even while planning: an under-specified
            # task most needs clarification BEFORE a plan is committed (the user
            # named a detail only they know). ask_user is read-only-safe — the loop
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
            tools = [
                t for t in tools if getattr(t, "name", None) not in self._loop._planning_tools
            ]
        # Append the virtual ask_user + clarify + propose_plan_update tools in execution
        # mode. All are documented so the model decides WHEN to use them;
        # neither is injected by reminder. propose_plan_update is the model's
        # auto-recovery affordance: when its current plan is wrong, it proposes
        # a revision and the user accepts/refines via the plan-approval gate.
        virtuals = [_finish_tool_singleton()]
        # P6 — when a Build contract governs the run, ALSO advertise its verification
        # finalizer (ready_for_*_verification) as a per-kind alias of `finish` (the name
        # the prompt pack instructs the model to call). Advertised next to plain `finish`
        # (kept for compatibility) and, like `finish`, survives meta-tool suppression
        # since both route to the same host-truth finish gate.
        if self._loop._finish_alias:
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
        return list(tools) + virtuals

    def known_tool_names_for_requery(self) -> set[str]:
        _kn = getattr(self._loop.executor, "known_tool_names_for_requery", None)
        if callable(_kn):
            known_tool_names = set(cast("Iterable[str]", _kn()))
        else:
            _cn = getattr(self._loop.executor, "callable_tool_names", None)
            if callable(_cn):
                # Duck-typed: executors exposing callable_tool_names return an
                # iterable of tool-name strings (frozenset[str] on the real backend).
                known_tool_names = set(cast("Iterable[str]", _cn()))
            else:
                known_tool_names = {t.name for t in self._loop.executor.available_tools()}
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
        all_known_names = (
            known_tool_names
            | virtual_names
            | set(self._loop._planning_tools)
        )
        return all_known_names

    def unknown_tool_requery_hint(self, tool_name: str, offered_names: set[str]) -> str:
        _hint = (
            f"ERROR: Unknown tool '{tool_name}'. "
            f"Available: {sorted(list(offered_names))}"
        )
        if self._loop._assist:
            _suggestion = _nearest_tool_name(
                tool_name, offered_names
            )
            if _suggestion is not None:
                _hint = f"{_hint} did you mean '{_suggestion}'?"
        return _hint

    async def drive_step(self, view: View, events: list[Event]) -> tuple[AgentStep | None, Disp]:
        # (e) ask the agent for ONE action (principle 1). The visible tool
        # set is mode-scoped: while PLANNING the agent sees ONLY the plan
        # tool (so it can't act before approval); while executing it sees
        # everything except the plan tool.
        # Escape temperature: jitter HARD to break a self-imitation chain —
        # the single step right after a stuck reframe (StuckDetector's escape).
        # escape_seq/acted_since_escape are pure functions of `events`
        # (the stuck gate recomputes its own copy); recompute here for the
        # temperature decision (folds into `_drive_step` on extraction).
        escape_seq = signals.stuck_escape_seq(events)
        acted_since_escape = escape_seq is not None and any(
            isinstance(e, ActionEvent) and e.seq is not None and e.seq > escape_seq
            for e in events
        )
        in_escape = escape_seq is not None and not acted_since_escape
        escape_temp = _STUCK_ESCAPE_TEMP if in_escape else None
        # Withhold the meta/handoff virtuals until this session's first
        # real action (see _tools_for_step docstring — Phase-B re-run #4).
        fresh_session = (
            self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
        )
        # Forced-submit recovery narrows the offered tools to submit_plan plus a bounded
        # read set. Prose-plan loops key off a replayed event marker; repeated planning
        # refusals key off the tail refusal streak in the same event log.
        planning_refusal_force = (
            self._loop.mode == OperatingMode.PLANNING
            and planning_tool_refusal_streak(events) >= _PLANNING_TOOL_REFUSAL_NARROW_AT
        )
        force_submit_only = self._loop.mode == OperatingMode.PLANNING and (
            signals.prose_plan_force_submit(events) or planning_refusal_force
        )
        force_read_tools = (
            _PLANNING_TOOL_REFUSAL_READ_TOOLS if planning_refusal_force else None
        )
        try:
            attempts = 0
            requery_count = 0
            provider_retry_count = 0  # P2: tracks LLMProviderUnavailable occurrences
            protocol_repair_count = 0
            transient_messages: list[LLMMessage] = []
            repaired_view: View | None = None
            while True:
                current_view = repaired_view or view
                try:
                    # Apply transient messages (requery-outside-log, Rung 6)
                    # to the View if we're in a retry loop.
                    if transient_messages:
                        current_view = current_view.model_copy(
                            update={
                                "messages": current_view.messages + transient_messages
                            }
                        )

                    step = await self._loop.agent.step(
                        current_view,
                        self.tools_for_step(
                            suppress_meta_tools=fresh_session,
                            force_submit_only=force_submit_only,
                            force_read_tools=force_read_tools,
                        ),
                        mode=self._loop.mode,
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

                    # Rung 7: Invalid-tool reroute (weak-model FC kit).
                    # Valid JSON but unknown tool name -> if we haven't
                    # hit the requery bound, inject a hint and retry
                    # without persisting the failure to the store.
                    # The requery applies ONLY to names absent from the
                    # FULL tool registry (truly unknown), never to
                    # known-but-currently-withheld tools.
                    all_known_names = self.known_tool_names_for_requery()

                    if step.tool_call and step.tool_call.tool_name not in all_known_names:
                        # A tool whose NAME alone trips the confirm policy's
                        # security gate (publish/deploy/release — it leaves the
                        # blast radius) must NOT be bounced back to the model by
                        # the unknown-tool requery: an unregistered publish-class
                        # name is a real publish intent that has to reach the
                        # human confirm gate, not a hallucination to retry.
                        # Without this the requery swallowed `deploy_site` before
                        # BlastRadiusConfirm's publish guard could pause for
                        # confirmation — then the plan read "done" with nothing
                        # executed and the execution-finish gate spun forever
                        # (the confirm/reject livelock root cause).
                        _gbn = getattr(self._loop.policy, "gates_by_name", None)
                        _name_gated = callable(_gbn) and _gbn(
                            step.tool_call.tool_name
                        )
                        if not _name_gated and requery_count < 2:
                            requery_count += 1
                            _LOG.info(
                                f"Unknown tool {step.tool_call.tool_name}, requerying..."
                            )
                            offered_tools = self.tools_for_step(
                                suppress_meta_tools=fresh_session,
                                force_submit_only=force_submit_only,
                                force_read_tools=force_read_tools,
                            )
                            offered_names = {t.name for t in offered_tools}
                            # Mirror the assistant's turn so the next call's
                            # messages list stays balanced for pairing.
                            transient_messages.append(
                                LLMMessage(
                                    role="assistant",
                                    content=step.thought,
                                    tool_calls=[
                                        {
                                            "id": step.tool_call.call_id,
                                            "name": step.tool_call.tool_name,
                                            "arguments": step.tool_call.arguments,
                                        }
                                    ],
                                )
                            )
                            # F2 / T9 (assist-gated): when self._assist is on, append
                            # a "did you mean <name>?" suggestion computed by
                            # Levenshtein distance over the offered tool names.
                            # The existing Rung-7 hint and requery bound (cap=2)
                            # are reused — no new reroute path, no new cap.
                            # Assist OFF (capable-model default) leaves the hint
                            # byte-identical to today.
                            _hint = self.unknown_tool_requery_hint(
                                step.tool_call.tool_name, offered_names
                            )
                            transient_messages.append(
                                LLMMessage(
                                    role="user",
                                    content=_hint,
                                )
                            )
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
                    return await self._pause_driver_unavailable()
                except LLMTransientError:
                    if attempts < len(_DRIVER_RETRY_BACKOFFS_S):
                        await _sleep(_DRIVER_RETRY_BACKOFFS_S[attempts])
                        attempts += 1
                        continue
                    else:
                        return await self._pause_driver_unavailable()
                except LLMError as e:
                    if _is_tool_result_adjacency_protocol_error(e):
                        if protocol_repair_count < 1:
                            protocol_repair_count += 1
                            repaired_messages = repair_tool_call_adjacency(
                                current_view.messages
                            )
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
                        _LOG.warning(f"Provider rejected request: {e}, requerying...")
                        transient_messages.append(LLMMessage(
                            role="user",
                            content=(
                                f"The provider rejected the previous request: {e}. "
                                "Please adjust your response (check tool names, "
                                "JSON structure, or parameters) and try again."
                            )
                        ))
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
