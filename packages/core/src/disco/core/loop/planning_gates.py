"""Internal _LoopFacet collaborator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..security import RiskAssessment
from .engine_contracts import (
    _FORCE_SUBMIT_DIRECTIVE,
    _PLAN_NUDGE,
    _PLANNING_TOOL_REFUSAL_ESCALATE_AT,
    _REVISION_FORCE_SUBMIT_K,
    _WORKFLOW_ROUTER_PLAN_NUDGE,
    ActionEvent,
    AgentErrorEvent,
    AgentStep,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    Iterable,
    LLMMessage,
    MessageEvent,
    OperatingMode,
    StatusEvent,
    _out_of_phase_submit_plan_refusal,
    _planning_tool_refusal_message,
    cast,
    harvest_revision_plan_after_refusal,
    planning_tool_refusal_streak,
    signals,
)

if TYPE_CHECKING:
    from .ports import (
        ContextGroundingPort,
        ConversationModePort,
        GateCounterPort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        TurnControlPort,
    )

    class _LoopFacet(

        ContextGroundingPort,

        ConversationModePort,

        GateCounterPort,

        LoopEventPort,

        PlanLifecyclePort,

        ToolExecutionPort,

        TurnControlPort,

        Protocol,

    ):
        """The loop capability this module uses: context grounding, conversation mode, gate
        counters, the event log, the plan lifecycle, tool execution, turn control.
        """

from .plan_submission import _handle_submitted_plan


async def _preserve_planning_prose(
    loop: _LoopFacet, step: AgentStep, events: list[Event]
) -> list[Event]:
    """Publish a planner acknowledgement before adding host guidance."""
    if step.thought.strip() and not loop._quiet:
        await loop._emit(
            MessageEvent(
                source=EventSource.AGENT,
                message=LLMMessage(role="assistant", content=step.thought),
            )
        )
        return await loop._events()
    return events


async def _maybe_harvest_prose_plan(
    loop: _LoopFacet, events: list[Event]
) -> Disp | None:
    if not (
        loop._revision_force_submit_enabled
        and signals.prose_plan_force_submit(events)
        and not signals.prose_plan_harvested(events)
        and not signals.plan_submitted_since_current_planning(events)
    ):
        return None
    plan = loop._harvest_prose_plan(events)
    if plan is None:
        return None
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    "REL-RC-N HARVEST: the planner stayed in PLANNING after the "
                    "force-submit ladder and still did not call `submit_plan`. "
                    f"Synthesizing fresh PlanEvent revision {plan.revision} from "
                    f"the latest assistant prose plan with {len(plan.steps)} "
                    "step(s), then routing it through the normal plan approval "
                    "gate.\n</system-reminder>"
                ),
            ),
        )
    )
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="prose_plan_harvested",
        )
    )
    await loop._emit(plan)
    loop._plan_explore_reads = 0
    loop._plan_nudges = 0
    return await loop._route_plan_approval_gate(plan)


async def _maybe_force_submit_prose(
    loop: _LoopFacet, events: list[Event]
) -> Disp | None:
    if not (
        loop._revision_force_submit_enabled
        and signals.plan_nudges_since_current_planning(events)
        >= _REVISION_FORCE_SUBMIT_K
        and not signals.prose_plan_force_submit(events)
        and not signals.plan_submitted_since_current_planning(events)
    ):
        return None
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="force_submit_plan",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=_FORCE_SUBMIT_DIRECTIVE),
        )
    )
    loop._plan_nudges = 0
    return Disp.CONTINUE


async def _nudge_planner(loop: _LoopFacet) -> Disp:
    loop._plan_nudges += 1
    nudge = (
        _WORKFLOW_ROUTER_PLAN_NUDGE
        if loop._workflow_router_phase_active()
        else _PLAN_NUDGE
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=nudge),
        )
    )
    if await loop._post_noop_valve() is Disp.HALT:
        return Disp.HALT
    return Disp.CONTINUE


async def _handle_planning_prose(
    loop: _LoopFacet, step: AgentStep, events: list[Event]
) -> Disp:
    events = await _preserve_planning_prose(loop, step, events)
    harvested = await _maybe_harvest_prose_plan(loop, events)
    if harvested is not None:
        return harvested
    forced = await _maybe_force_submit_prose(loop, events)
    if forced is not None:
        return forced
    return await _nudge_planner(loop)


async def _refuse_planning_tool(
    loop: _LoopFacet, step: AgentStep, events: list[Event]
) -> Disp:
    tool_call = step.tool_call
    assert tool_call is not None
    refusal_streak = planning_tool_refusal_streak(events) + 1
    action = ActionEvent(
        thought=step.thought,
        tool_call=tool_call,
        self_assessed_risk=step.self_assessed_risk,
        llm_response_id=step.llm_response_id,
    )
    await loop._emit(action)
    await loop._emit(
        AgentErrorEvent(
            error=_planning_tool_refusal_message(
                tool_call.tool_name,
                streak=refusal_streak,
                read_calls_remaining=loop._driver.force_submit_read_calls_remaining(),
            ),
            action_id=action.id,
            tool_call_id=tool_call.call_id,
        )
    )
    revision_refusal = signals.in_planning_for_revision(events)
    verifier_repair = signals.verifier_repair_planning_active(events)
    if not verifier_repair and (
        revision_refusal or refusal_streak >= _PLANNING_TOOL_REFUSAL_ESCALATE_AT
    ):
        harvested = await harvest_revision_plan_after_refusal(loop)
        if harvested is not None:
            return harvested
    return Disp.CONTINUE


async def _handle_planning_tool(
    loop: _LoopFacet, step: AgentStep, events: list[Event]
) -> Disp:
    tool_call = step.tool_call
    assert tool_call is not None
    allowed = loop._driver.planning_allowed_tool_names()
    if tool_call.tool_name not in allowed:
        return await _refuse_planning_tool(loop, step, events)
    noncounting = {"ask_user", "questions_v2", "clarify", "think"}
    if tool_call.tool_name not in noncounting:
        await loop._planner.note_planning_read_and_maybe_force()
    return Disp.FALLTHROUGH


class PlanningGateController:
    def __init__(self, loop: _LoopFacet) -> None:
        self._loop = loop

    async def _gate_planning_mode(self, step: AgentStep, events: list[Event]) -> Disp:
        if self._loop._reconcile_mode_from_events(events) != OperatingMode.PLANNING:
            return Disp.FALLTHROUGH
        tool_call = step.tool_call
        if (
            tool_call is not None
            and self._loop._workflow_run is not None
            and tool_call.tool_name in ("skip", "needs_input")
        ):
            return Disp.FALLTHROUGH
        if tool_call is not None and tool_call.tool_name == self._loop._plan_tool:
            return await _handle_submitted_plan(self._loop, tool_call, events)
        if tool_call is None:
            return await _handle_planning_prose(self._loop, step, events)
        return await _handle_planning_tool(self._loop, step, events)

    async def _gate_out_of_phase_submit_plan(self, step: AgentStep, events: list[Event]) -> Disp:
        """Refuse the planning-only signal at the execution dispatch boundary.

        ``submit_plan`` remains callable in the executor's broad security scope
        because the same executor backs both phases, but Driver deliberately
        withholds it from execution requests.  A provider may still return a
        remembered/unoffered name.  Letting that reach the executor produces an
        inert acknowledgement or an argument-schema error, neither of which
        tells the model the important fact: its corrected plan was approved and
        it should execute.  Persist a paired, phase-aware refusal instead.
        """

        tool_call = step.tool_call
        plan_name = getattr(self._loop._plan_tool, "name", self._loop._plan_tool)
        if tool_call is None or tool_call.tool_name != plan_name:
            return Disp.FALLTHROUGH
        if self._loop._reconcile_mode_from_events(events) == OperatingMode.PLANNING:
            return Disp.FALLTHROUGH
        approved = signals.latest_approved_plan(events)
        action = ActionEvent(
            thought=step.thought,
            tool_call=tool_call,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        stored = cast("ActionEvent", await self._loop._emit(action))
        await self._loop._emit(
            AgentErrorEvent(
                error=_out_of_phase_submit_plan_refusal(
                    approved.revision if approved is not None else None
                ),
                action_id=stored.id,
                tool_call_id=tool_call.call_id,
            )
        )
        return Disp.CONTINUE

    async def _gate_stuck_escape_tool_quarantine(
        self, step: AgentStep, events: list[Event]
    ) -> Disp:
        """Persist and bound a model call withheld by the active recovery episode.

        Two refusal classes exist, both durable, paired, and restart-stable:

        - a read-BYPASS alias (general shell/code execution, delegated
          exploration) selected while the episode has zero trusted progress —
          those tools are absent from the schema but a model may hallucinate
          the name;
        - a ``file_read`` whose every requested line is PROVABLY redundant
          (already held in visible context at the current content digest).
          ``file_read`` itself stays offered: the k6g canary showed tool-level
          quarantine convicts legitimate different-resource reads (F1).

        The halt bound counts refusals of the SAME withheld target (the same
        bypass tool, or the same redundant resource) within the current
        episode; refusals of different targets are independent recoverable
        errors, never one repeated behavior.
        """
        tool_call = step.tool_call
        if tool_call is None:
            return Disp.FALLTHROUGH
        blocked_tools = self._loop._driver.stuck_escape_blocked_tools_for_step(events)
        bound_path: str | None = None
        refusal_floor: int | None = None
        if tool_call.tool_name in blocked_tools:
            refusal_detail = (
                f"`{tool_call.tool_name}` is temporarily withheld during the current "
                "read-loop recovery because general execution or delegated exploration "
                "can bypass the typed read quarantine. It returns once real progress "
                "lands (a trusted changed-state receipt), a plan revision is approved, "
                "or the user replies. Use a structured mutation, verifier, or finish "
                "tool instead."
            )
        else:
            refusal = self._loop._driver.stuck_escape_redundant_read_refusal(events, tool_call)
            if refusal is None:
                return Disp.FALLTHROUGH
            bound_path = str(refusal["path"])
            raw_floor = refusal.get("refusal_floor_seq")
            refusal_floor = raw_floor if type(raw_floor) is int else None
            held_spans = cast("list[tuple[int, int]]", refusal["held_spans"])
            held = ", ".join(f"{start}-{end}" for start, end in held_spans)
            refusal_detail = (
                f"`file_read` of `{bound_path}` (lines "
                f"{refusal['requested_start_line']}-{refusal['requested_end_line']}) is "
                "withheld: every requested line is already in your context, "
                f"byte-identical (you hold lines {held} of {refusal['total_lines']}; "
                f"content digest {refusal['digest_prefix']} unchanged). Re-reading "
                "unchanged bytes is what caused the current loop. This file becomes "
                "readable again when its content changes, a plan revision is approved, "
                "or the user replies. Available now: edit it directly with the exact "
                "content you already hold, read a different file or an unseen line "
                "range, run a structured verifier, or call `finish`."
            )
        prior_refusals = signals.stuck_escape_refusal_count(
            events, tool_call.tool_name, action_path=bound_path, after_seq=refusal_floor
        )
        action = ActionEvent(
            thought=step.thought,
            tool_call=tool_call,
            self_assessed_risk=step.self_assessed_risk,
            llm_response_id=step.llm_response_id,
        )
        await self._loop._emit(action)
        await self._loop._emit(
            AgentErrorEvent(
                error=(f"{signals.STUCK_ESCAPE_REFUSAL_ERROR_PREFIX}{tool_call.tool_name}"),
                detail=refusal_detail,
                action_id=action.id,
                tool_call_id=tool_call.call_id,
            )
        )
        if prior_refusals >= 1:
            await self._loop._land_blocked(
                reason="stuck_escape_tool_quarantine",
                guidance=(
                    "The model selected the same withheld recovery target twice during "
                    "the current escape episode. The action was not executed."
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail="stuck_escape_tool_quarantine",
            )
            return Disp.HALT
        return Disp.CONTINUE

    async def _gate_hard_deny(self, action: ActionEvent) -> Disp:
        # (h.5) HARD DENY (Cluster 3) — catastrophic commands are refused
        # outright, BEFORE the confirm gate. No approval, policy, or LLM
        # can run them. The agent sees the refusal as an error and adapts.
        deny_reason = signals.hard_deny_reason(action)
        if deny_reason is not None:
            await self._loop._emit(action)  # record the proposed action for audit
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"REFUSED: that command is hard-denied ({deny_reason}). It "
                        "will never be executed regardless of approval. Choose a "
                        "different, safe approach.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id if action.tool_call else None,
                )
            )
            return Disp.CONTINUE
        return Disp.FALLTHROUGH

    async def _gate_risk_confirm(self, action: ActionEvent) -> tuple[Disp, ActionEvent]:
        # (i) RISK GATE — assess, then maybe require confirmation (§5).
        # GATE ORDER (appkit-lane live catch 2026-07-10): an action whose tool is
        # NOT in the executor's callable set can never execute, so it must never
        # park on a human confirmation — in an autonomous run nobody can answer
        # and the run dies at the inactivity cap (a barred `file_edit` in strict
        # AppKit hit BlastRadiusConfirm instead of the executor's unknown_tool
        # refusal). Fall through so execute() emits the ONE canonical refusal.
        _callable = getattr(self._loop.executor, "callable_tool_names", None)
        if callable(_callable):
            try:
                if action.tool_call.tool_name not in set(cast(Iterable[str], _callable())):
                    return Disp.FALLTHROUGH, action
            except Exception:  # noqa: BLE001 — introspection failure → normal gating
                pass
        # Audit (security §7): when the analyzer exposes the detailed
        # assessment, stamp it into the action's meta so the security
        # posture (final risk, rationale, contributing analyzers, the
        # self-assessment) is reconstructable from the log. Analyzers that
        # implement only assess() are unaffected.
        detailed = getattr(self._loop.analyzer, "assess_detailed", None)
        if callable(detailed):
            # Duck-typed: analyzers implementing the richer protocol return a
            # RiskAssessment; the getattr(..., None) probe widens it to object.
            assessment = cast(RiskAssessment, detailed(action))
            risk = assessment.risk
            audited_meta = {
                **action.meta,
                "risk_assessment": assessment.model_dump(mode="json"),
            }
            action = action.model_copy(update={"meta": audited_meta})
        else:
            risk = self._loop.analyzer.assess(action)

        # DC-03: obtain WHERE this concrete tool call executes. The call-aware hook
        # lets sandbox-backed tools declare host scope before any HTTP/key work.
        _scope_fn = getattr(self._loop.executor, "tool_scope_for_call", None)
        _tool_scope = "unknown"
        if callable(_scope_fn):
            try:
                _tool_scope = _scope_fn(action.tool_call.tool_name, action.tool_call.arguments)
            except Exception:
                _tool_scope = "unknown"
        else:
            _static_scope_fn = getattr(self._loop.executor, "tool_scope", None)
            if callable(_static_scope_fn):
                try:
                    _tool_scope = _static_scope_fn(action.tool_call.tool_name)
                except Exception:
                    _tool_scope = "unknown"

        # Use should_confirm_action if the policy supports it; fall back to
        # should_confirm(risk) for policies that predate DC-03.
        _sca = getattr(self._loop.policy, "should_confirm_action", None)
        if callable(_sca):
            _gates = _sca(risk, scope=_tool_scope, tool_name=action.tool_call.tool_name)
        else:
            _gates = self._loop.policy.should_confirm(risk)

        # Journal: stamp auto_approved when scope-based exemption overrides
        # what the base risk gate would have decided.
        if not _gates and self._loop.policy.should_confirm(risk):
            action = action.model_copy(
                update={"meta": {**action.meta, "auto_approved": "sandboxed"}}
            )

        if _gates:
            await self._loop._emit(action)  # record the PROPOSED action
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.WAITING_FOR_CONFIRMATION,
                    detail=action.id,
                )
            )
            return Disp.HALT, action
        return Disp.FALLTHROUGH, action
