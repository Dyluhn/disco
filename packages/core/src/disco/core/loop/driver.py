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
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from ..llm import (
    LLMContextWindowExceeded,
    LLMError,
    LLMProviderUnavailable,
    LLMTransientError,
    OperatingMode,
)
from ..view import View
from . import signals, view_render
from .boundaries import AgentStep
from .control import Disp
from .fc_kit import _nearest_tool_name
from .messages import _describe_llm_error
from .stream_extract import extract_partial_string_field
from .tool_specs import (
    _ask_user_tool_singleton,
    _clarify_tool_singleton,
    _notify_user_tool_singleton,
    _propose_plan_update_tool_singleton,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..llm import StreamChunk
    from .boundaries import StreamHook
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

_sleep = asyncio.sleep
_DRIVER_RETRY_BACKOFFS_S: tuple = (10.0, 30.0, 90.0)


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


class Driver:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    def build_stream_hook(self) -> StreamHook | None:
        """Per-step watch-it-write hook (or None if no sink is wired). Decodes the
        driver's streamed tool-call arg fragments into growing file-content frames
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
                return  # only stream tools that carry a file body
            if not st["path"]:
                # Require the WHOLE path (closing quote present) so a frame never
                # shows a half-typed filename like "styles" for "styles.css".
                p = extract_partial_string_field(st["args"], "path", require_complete=True)
                if p:
                    st["path"] = p
            content = extract_partial_string_field(st["args"], "content")
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
                }
            )

        return _hook

    async def _pause_driver_unavailable(self) -> tuple[None, Disp]:
        """Emit the standard PAUSED/driver-unavailable event pair and HALT.
        Called from both the LLMProviderUnavailable and LLMTransientError
        exhaustion paths to keep drive_step within its LOC budget."""
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "model driver unavailable — conversation"
                        " paused, resume when the model is back"
                    ),
                ),
            )
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.PAUSED,
                detail="driver-unavailable",
            )
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

    def tools_for_step(self, *, suppress_meta_tools: bool = False) -> list:
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
            _finish_tool_singleton,
            _remember_tool_singleton,
            _serve_tool_singleton,
        )

        tools = self._loop.executor.available_tools()
        if self._loop.mode == OperatingMode.PLANNING:
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
            # Append the VIRTUAL ask_user + clarify even while planning: an under-specified
            # task most needs clarification BEFORE a plan is committed (the user
            # named a detail only they know). ask_user is read-only-safe — the loop
            # intercepts it (never executes it against the sandbox) and halts at the
            # Ask-gate, same as in execution. clarify is the MULTI-QUESTION variant
            # for when several specifics are missing. Without this the planner is forced
            # to guess and bury the unknown in the plan instead of just asking.
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
                _delegate_explore_tool_singleton(),
            ]
        return list(tools) + virtuals

    def known_tool_names_for_requery(self) -> set[str]:
        _cn = getattr(self._loop.executor, "callable_tool_names", None)
        if callable(_cn):
            # Duck-typed: executors exposing callable_tool_names return an
            # iterable of tool-name strings (frozenset[str] on the real backend).
            known_tool_names = set(cast("Iterable[str]", _cn()))
        else:
            known_tool_names = {t.name for t in self._loop.executor.available_tools()}
        virtual_names = {
            "ask_user",
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
        try:
            attempts = 0
            requery_count = 0
            provider_retry_count = 0  # P2: tracks LLMProviderUnavailable occurrences
            transient_messages: list[LLMMessage] = []
            while True:
                try:
                    # Apply transient messages (requery-outside-log, Rung 6)
                    # to the View if we're in a retry loop.
                    current_view = view
                    if transient_messages:
                        current_view = view.model_copy(update={
                            "messages": view.messages + transient_messages
                        })

                    step = await self._loop.agent.step(
                        current_view,
                        self.tools_for_step(suppress_meta_tools=fresh_session),
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
                                suppress_meta_tools=fresh_session
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
                    # DEFECT-6: Provider 4xx "rejected request" must not be
                    # terminal; enter requery path with a hint.
                    if requery_count < 2:
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
            await self._loop._emit(ErrorEvent(code="model_error", detail=_describe_llm_error(e)))
            return None, Disp.HALT
        return step, Disp.FALLTHROUGH
