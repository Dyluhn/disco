"""Internal Loop turn-control collaborator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..effects import ActionProfile, EffectCapability
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ToolResult,
)
from ..llm import OperatingMode
from . import signals
from .boundaries import AgentStep
from .control import Disp
from .observe import _DELEGATE_ACTION_PROFILE, _FANOUT_INPUT_MAX_CHARS
from .turn_control_support import (
    _handle_serve,
)

if TYPE_CHECKING:
    from .loop_facade_compat import _AgentLoopCompatibility as AgentLoop

_LOG = logging.getLogger("disco.loop")


class MetaToolCommonMixin:
    _loop: AgentLoop

    async def handle_notify_user(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        msg = str(step.tool_call.arguments.get("message") or "").strip() or step.thought
        if msg.strip():
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=msg),
                )
            )
        else:
            self._loop._invisible_steps += 1
        # Non-blocking, but NOT exempt from the actionless valve —
        # a bare `continue` here let prose spam bypass every cap
        # (Phase-B re-run, 2026-06-10).
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_remember(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        # Durable memory: emit a PINNED KnowledgeEvent so the fact
        # survives condensation and is re-injected into context every
        # step. Non-blocking — like notify_user, the agent keeps
        # working right after. A blank fact is ignored (no-op).
        #
        # FRESH-SESSION BACKSTOP (Phase-B re-run #6, 2026-06-11):
        # remember is withheld from the offered set until the
        # session's first real action, but a weak model can
        # hallucinate calls to unoffered tools — re-run #6's model
        # remember-spammed duplicate CSV facts right after its
        # first-move finish was refused. Unlike notify_user (which
        # degrades into the bounded prose channel), an executed
        # remember POLLUTES pinned knowledge and reads as success,
        # so the model keeps picking it. Refuse with actionable
        # feedback: pinned facts must come from THIS session's work.
        if (
            self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
        ):
            return await self._loop._valve.refuse_fresh_session(
                step,
                "remember refused: no real work has happened "
                "yet in this session. Facts worth pinning come "
                "from real observations — execute the next plan "
                "step with real tool calls first, then remember "
                "what you learned.",
            )
        fact = str(step.tool_call.arguments.get("fact") or "").strip()
        if fact:
            import hashlib

            scope = str(step.tool_call.arguments.get("scope") or "").strip()
            normalized = fact
            fact_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            seen = {
                (e.scope, hashlib.sha256(e.snippet.strip().encode("utf-8")).hexdigest())
                for e in events
                if isinstance(e, KnowledgeEvent)
            }
            if (scope, fact_hash) in seen:
                action = ActionEvent(
                    thought=step.thought,
                    tool_call=step.tool_call,
                    self_assessed_risk=step.self_assessed_risk,
                    llm_response_id=step.llm_response_id,
                )
                res = ToolResult(
                    call_id=step.tool_call.call_id,
                    tool_name="remember",
                    success=True,
                    content="Already recorded — not stored again.",
                    action_profile=ActionProfile(
                        capabilities=frozenset({EffectCapability.RUN_CONTROL})
                    ),
                )
                await self._loop._emit(action)
                await self._loop._emit(ObservationEvent(tool_result=res, action_id=action.id))
            else:
                await self._loop._emit(
                    KnowledgeEvent(source=EventSource.AGENT, scope=scope, snippet=fact)
                )
                # C5 — write-through mirror: persist the fact to
                # `<workspace>/.pmx/MEMORY.md` so the standing
                # memory survives a hard filesystem reset (a
                # box wipe / fresh backend). The in-View
                # KnowledgeEvent channel remains the
                # authoritative in-session source — `.pmx/`
                # is a write-through mirror, not a divergent
                # second store. Best-effort: a sandbox write
                # failure is logged but never blocks the
                # in-View emission (the agent still has the
                # fact in-context for THIS run).
                try:
                    await self._loop._write_pmx_memory_fact(scope, fact)
                except Exception:  # noqa: BLE001 — mirror is best-effort
                    import logging as _logging

                    _logging.getLogger(__name__).warning(
                        "pmx MEMORY write-through failed (in-View fact survives)",
                        exc_info=True,
                    )
        else:
            # Blank fact persists NOTHING — count it or it's an
            # unbounded silent token burn.
            self._loop._invisible_steps += 1
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_serve(self, step: AgentStep, events: list[Event]) -> Disp:
        return await _handle_serve(self._loop, step, events)

    async def handle_delegate_explore(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller (engine loop) dispatches by tool_name
        # C20 — `delegate_explore`: a bounded, read-only Explore/Plan
        # helper the loop dispatches+joins. The driver calls it; the
        # loop:
        #   1. enforces the per-run-segment cap (_FANOUT_MAX_PER_RUN;
        #      past the cap → refuse with a system-reminder, no
        #      dispatch, no observation);
        #   2. emits the ActionEvent (so the audit trail sees the
        #      proposed call);
        #   3. dispatches the subagent (default: a thin deterministic
        #      stub — the test seam; production wires a real LLM
        #      round-trip with read-only tools only — see
        #      `_run_fanout` for the override hook);
        #   4. folds the subagent's response back as a paired
        #      ObservationEvent (success=True on a clean dispatch,
        #      success=False with error="cap_exceeded" on refusal)
        #      so the driver sees the result on its next turn.
        # The helper is READ-ONLY: a subagent cannot mutate
        # workspace state, cannot run shell, cannot write files
        # (enforced upstream by the tools the helper is offered —
        # file_read/file_list/search/extract; the engine also
        # cannot recurse through `delegate_explore` because the
        # cap applies to nested calls too). NON-BLOCKING: the
        # driver keeps working right after — the same shape as
        # notify_user/remember/serve (the actionless valve still
        # applies if a fan-out produces no real work).
        if self._loop._fanout_count >= self._loop._fanout_max:
            # Cap exceeded — refuse with feedback. The cap is
            # per-run-segment, so a fresh `run()` resets it.
            # We do NOT raise / halt / STUCK (this is a soft
            # "no more fan-outs this segment" gate, not a
            # stuck-detector); we emit a system-reminder +
            # ActionEvent + AgentErrorEvent (paired by
            # call_id) so the driver sees the refusal on its
            # next turn and falls back to direct tools. The
            # refusal is invisible to the actionless valve
            # (an error-paired action doesn't extend the
            # noop streak).
            action = ActionEvent(
                thought=step.thought,
                tool_call=step.tool_call,
                self_assessed_risk=step.self_assessed_risk,
                llm_response_id=step.llm_response_id,
            )
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"delegate_explore refused: the per-run-segment "
                        f"cap ({self._loop._fanout_max}) has been reached "
                        f"(used {self._loop._fanout_count}/{self._loop._fanout_max} "
                        f"this segment). Fall back to direct read-only "
                        f"tools (file_read, file_list, search, extract) "
                        f"for the rest of this run segment; a fresh run "
                        f"segment resets the budget.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=(action.tool_call.call_id if action.tool_call else None),
                )
            )
            return Disp.CONTINUE
        # Under the cap → record the proposed action, dispatch
        # the helper, and fold the result back. The dispatch
        # is `await`ed so the ActionEvent and ObservationEvent
        # land in the same turn (the driver sees both on its
        # next step).
        self._loop._fanout_count += 1
        action = ActionEvent(
            thought=step.thought,
            tool_call=step.tool_call,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        await self._loop._emit(action)
        # Length-bound the helper's input BEFORE dispatching —
        # the bound is a property of the fan-out (regardless
        # of what `_run_fanout` does — a test seam, a real
        # LLM round-trip, a future override). Without this
        # bound a driver could grow the helper's prompt
        # unboundedly within a single segment; the result
        # is folded back into the View, which the condenser
        # would later have to manage.
        _fanout_args = dict(action.tool_call.arguments or {})
        _trunc_marker = "…[truncated]"
        for _k in ("question", "context"):
            _v = str(_fanout_args.get(_k) or "")
            if len(_v) > _FANOUT_INPUT_MAX_CHARS:
                _fanout_args[_k] = (
                    _v[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
                )
        result = await self._loop._run_fanout(
            _fanout_args,
            events,
            call_id=(action.tool_call.call_id if action.tool_call else ""),
        )
        # Runtime overrides return the same ToolResult shape as the default
        # helper. Bind the host-owned profile here as well so every actually
        # dispatched fan-out is restart-stable regardless of the override.
        result = result.model_copy(update={"action_profile": _DELEGATE_ACTION_PROFILE})
        await self._loop._emit(ObservationEvent(tool_result=result, action_id=action.id))
        # Fan-out is non-blocking — the driver keeps working
        # right after. The actionless valve still applies if
        # the helper returned empty (a degenerate fan-out is
        # still a no-op step, like remember/serve).
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_workflow_control(self, step: AgentStep, events: list[Event]) -> Disp:
        assert step.tool_call is not None  # caller dispatches by workflow-control name
        workflow_run = getattr(self._loop, "_workflow_run", None)
        if workflow_run is None:
            return Disp.FALLTHROUGH

        name = step.tool_call.tool_name
        args = step.tool_call.arguments or {}
        meta = {
            "workflow_control": name,
            "workflow_run_id": getattr(workflow_run, "run_id", ""),
        }
        if name == "skip":
            reason = str(args.get("reason") or "").strip() or "workflow skipped"
            await self._loop._valve.land_terminal_with_explanation(
                reason=reason,
                guidance=(
                    "The workflow control tool `skip` was called. The run is "
                    "ending honestly without producing the contracted output."
                ),
                status=ConversationStatus.FINISHED,
                detail="workflow_skipped",
                meta=meta,
            )
            return Disp.HALT

        if name == "needs_input":
            reason = str(args.get("reason") or "").strip() or "workflow needs input"
            required_action = str(args.get("required_action") or "").strip()
            if not required_action:
                required_action = "Provide the missing workflow input or approval."
            guidance = f"{reason}\nRequired action: {required_action}"
            await self._loop._valve.land_blocked(
                reason=reason,
                guidance=guidance,
                legacy_status=ConversationStatus.PAUSED,
                legacy_detail="workflow_needs_input",
                required_explanation=required_action,
            )
            return Disp.HALT

        return Disp.FALLTHROUGH

    async def handle_noop_step(self, step: AgentStep, events: list[Event]) -> Disp:
        if step.thought.strip():
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=step.thought),
                )
            )
        else:
            # Nothing persisted — invisible to every event-derived
            # detector, so the instance counter has to carry it.
            self._loop._invisible_steps += 1
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def handle_truncated_step(self, step: AgentStep, events: list[Event]) -> Disp:
        """W-31 — the model's message was cut off mid-sentence
        (`finish_reason=="length"`) with no tool call. Persist the partial text
        as the assistant's turn (so the model sees its own fragment), then inject
        an ENVIRONMENT reminder telling it to CONTINUE from where it stopped —
        not restart. The reminder is a non-USER message, so it does NOT reset the
        stuck detector (a truncation storm still trips gate_stuck). Falls through
        the same no-progress valve as a no-op so a run can never spin forever on
        repeated truncations."""
        if step.thought.strip():
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.AGENT,
                    message=LLMMessage(role="assistant", content=step.thought),
                )
            )
        else:
            # Nothing persisted — invisible to every event-derived detector, so
            # the instance counter has to carry it (same as handle_noop_step).
            self._loop._invisible_steps += 1
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Your previous message was cut off mid-sentence — it hit "
                        "the output length limit. Continue it from exactly where "
                        "it stopped; do NOT restart or repeat what you already "
                        "wrote. Be more concise this time, and if you were about "
                        "to take an action, take it now with a tool call.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        if await self._loop._post_noop_valve() is Disp.HALT:
            return Disp.HALT
        return Disp.CONTINUE

    async def gate_ask_fresh_session(self, step: AgentStep, events: list[Event]) -> Disp:
        if (
            step.tool_call is not None
            and step.tool_call.tool_name
            in ("ask_user", "questions_v2", "clarify", "propose_plan_update")
            and self._loop.mode != OperatingMode.PLANNING
            and signals.actions_since_last_resume(events) == 0
            # In autonomous mode, ask_user/questions_v2/clarify are owned by the headless-
            # stall guard below (a clean "no user — decide yourself" nudge);
            # don't pre-empt them here with the interactive "do work, then ask"
            # message, which tells the model it can ask when it can't. (g.5)
            # still governs propose_plan_update on a fresh session.
            and not (
                self._loop._autonomous
                and step.tool_call.tool_name in ("ask_user", "questions_v2", "clarify")
            )
        ):
            return await self._loop._valve.refuse_fresh_session(
                step,
                f"{step.tool_call.tool_name} refused: no real "
                "work has happened yet in this session. Attempt "
                "the next plan step with real tool calls first — "
                "if it fails or something is genuinely unclear, "
                "you can then ask or propose a plan change with "
                "the evidence in hand.",
            )
        return Disp.FALLTHROUGH

    async def gate_autonomous_ask_stall(self, step: AgentStep, events: list[Event]) -> Disp:
        if (
            self._loop._autonomous
            and step.tool_call is not None
            and step.tool_call.tool_name in ("ask_user", "questions_v2", "clarify")
        ):
            asked = (
                str(step.tool_call.arguments.get("question") or "").strip() or step.thought.strip()
            )
            stall_action = ActionEvent(
                thought=step.thought,
                tool_call=step.tool_call,
                self_assessed_risk=step.self_assessed_risk,
                llm_response_id=step.llm_response_id,
            )
            await self._loop._emit(stall_action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "Autonomous mode is ON — there is no user available "
                        f"to answer. `{step.tool_call.tool_name}` is "
                        "unavailable in this mode. Make the best decision you "
                        "can from the information you already have, log the "
                        "assumptions in the submit_plan.context preamble, and "
                        "continue working toward the goal."
                        + (f"\nYour question was: {asked}" if asked else "")
                        + "\n</system-reminder>"
                    ),
                    action_id=stall_action.id,
                    tool_call_id=(
                        stall_action.tool_call.call_id if stall_action.tool_call else None
                    ),
                )
            )
            return Disp.CONTINUE
        return Disp.FALLTHROUGH
