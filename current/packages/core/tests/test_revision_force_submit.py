"""Forced-submit recovery for stuck revision re-plans (the actionless-revision fix).

Root (codex-proven, ENGINE): on a revision follow-up the model narrates the revised plan
without calling submit_plan; the actionless guard counts the prose as no-ops, submit_plan
(when it IS called) was mis-grouped as bookkeeping so it never reset the streak, and resume
reconstructed the same unsubmitted state → pause "actionless" → STUCK loop. Fix:
  (1) submit_plan RESETS consecutive_noops (it's the planning→execution gate, real progress)
      WITHOUT touching _BOOKKEEPING_TOOLS (other readers depend on it).
  (2) after K prose-only revision-planning nudges the engine emits a durable
      StatusEvent(detail="force_submit_plan") marker; signals.revision_force_submit reads it
      so driver.tools_for_step narrows the offered tools to submit_plan ONLY.
  (3) the marker is an event → survives resume (no soft-nudge loop after a pause).
"""

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    ToolCall,
)
from disco.core.llm import OperatingMode
from disco.core.loop import signals
from loop_fakes import build_loop


def _agent_msg(text="describing the revised plan..."):
    return MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
    )


def _action(tool):
    return ActionEvent(thought="t", tool_call=ToolCall(tool_name=tool, arguments={}))


# --- Fix 1: submit_plan resets the actionless streak; plan_step/etc. do not ---


def test_consecutive_noops_submit_plan_bounds_the_streak():
    # prose, prose, submit_plan, prose -> only the ONE prose AFTER submit_plan counts;
    # the submission bounds the backward walk (it is real planning->execution progress).
    events = [_agent_msg(), _agent_msg(), _action("submit_plan"), _agent_msg()]
    assert signals.consecutive_noops(events) == 1


def test_consecutive_noops_plan_step_still_neutral_not_a_reset():
    # plan_step/update_plan_progress stay bookkeeping no-ops: a model shuffling plan
    # state still racks up the streak (spam guard intact) — only submit_plan resets.
    events = [_agent_msg(), _action("plan_step"), _agent_msg(), _action("update_plan_progress")]
    # walk back: update_plan_progress (continue), prose (count=1), plan_step (continue),
    # prose (count=2) -> 2. The bookkeeping tools did NOT bound the streak.
    assert signals.consecutive_noops(events) == 2


def test_consecutive_noops_real_action_still_breaks():
    events = [_agent_msg(), _action("file_write"), _agent_msg(), _agent_msg()]
    assert signals.consecutive_noops(events) == 2  # only the two trailing proses


# --- Fix 2/3: revision_force_submit marker (revision-only, resume-durable) ---


def _revision_planning_prefix():
    # plan approved (seq 2) then re-entered planning (seq 6) -> in_planning_for_revision.
    return [
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=2),
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=6),
    ]


def test_revision_force_submit_true_after_marker():
    events = [
        *_revision_planning_prefix(),
        _agent_msg(),
        StatusEvent(status=ConversationStatus.RUNNING, detail="force_submit_plan", seq=9),
        _agent_msg(),
    ]
    assert signals.in_planning_for_revision(events) is True
    assert signals.revision_force_submit(events) is True


def test_revision_force_submit_released_by_submission():
    # a submit_plan AFTER the marker satisfies/releases the forced state.
    events = [
        *_revision_planning_prefix(),
        StatusEvent(status=ConversationStatus.RUNNING, detail="force_submit_plan", seq=9),
        _action("submit_plan"),
    ]
    assert signals.revision_force_submit(events) is False


def test_revision_force_submit_false_without_marker():
    events = [*_revision_planning_prefix(), _agent_msg(), _agent_msg()]
    assert signals.revision_force_submit(events) is False


def test_revision_force_submit_false_when_not_revision():
    # initial planning (no prior plan_approved) — never forced, even with a stray marker.
    events = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=2),
        StatusEvent(status=ConversationStatus.RUNNING, detail="force_submit_plan", seq=4),
    ]
    assert signals.in_planning_for_revision(events) is True  # first plan pending counts
    # ...but once the plan is approved + NOT re-entered, a marker is inert:
    approved = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=2),
        StatusEvent(status=ConversationStatus.RUNNING, detail="force_submit_plan", seq=3),
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=4),
    ]
    assert signals.revision_force_submit(approved) is False


def test_revision_force_submit_survives_resume():
    # the marker precedes a PAUSED+resume; because it is an EVENT in the replayed log,
    # a resumed segment still reads forced (no soft-nudge loop after the pause).
    events = [
        *_revision_planning_prefix(),
        StatusEvent(status=ConversationStatus.RUNNING, detail="force_submit_plan", seq=9),
        StatusEvent(status=ConversationStatus.PAUSED, detail="actionless", seq=10),
        StatusEvent(status=ConversationStatus.RUNNING, detail="resumed", seq=11),
        _agent_msg(),
    ]
    assert signals.revision_force_submit(events) is True


# --- the driver narrows tools to submit_plan only when forced ---


def test_tools_for_step_force_submit_narrows_to_plan_tool():
    from disco.core.llm import ToolSpec
    from loop_fakes import FakeExecutor

    # submit_plan reaches the model from executor.available_tools() (the engine intercepts
    # the call); give the fake a submit_plan tool + a read tool so the narrowing is exercised.
    executor = FakeExecutor(
        tools=[
            ToolSpec(name="submit_plan", description="propose a plan", parameters_schema={}),
            ToolSpec(name="file_read", description="read a file", parameters_schema={}),
        ]
    )
    loop, _ = build_loop(
        agent=None,
        conversation_id="conv",
        executor=executor,
        mode=OperatingMode.PLANNING,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    # The driver identifies read tools via executor.readonly_tool_names() (the capability
    # backstop). Give the fake one so the read-inclusion under force_submit is exercised.
    executor.readonly_tool_names = lambda: frozenset({"file_read"})  # type: ignore[attr-defined]

    driver = loop._driver
    plan_name = getattr(loop._plan_tool, "name", loop._plan_tool)  # "submit_plan"

    # Under force_submit, the model keeps submit_plan + READ tools (within the read grace):
    # a model re-planning a revision often wants to file_read the current files to ground its
    # diff BEFORE submitting (~30% of the time — narrowing to submit-only stranded it →
    # actionless → killed). _plan_explore_reads starts at 0 → reads allowed.
    loop._plan_explore_reads = 0
    forced = driver.tools_for_step(force_submit_only=True)
    assert {getattr(t, "name", None) for t in forced} == {plan_name, "file_read"}

    # …but once the read grace is exhausted (reads > cap + grace) it COLLAPSES to
    # submit_plan only, so a read loop can't run to the iteration hard cap.
    from disco.core.loop.driver import _FORCE_SUBMIT_READ_GRACE
    from disco.core.loop.messages import _PLAN_EXPLORE_READ_CAP

    loop._plan_explore_reads = _PLAN_EXPLORE_READ_CAP + _FORCE_SUBMIT_READ_GRACE
    forced_after = driver.tools_for_step(force_submit_only=True)
    assert {getattr(t, "name", None) for t in forced_after} == {plan_name}

    # default (not forced) offers more than just the plan tool while planning.
    normal = driver.tools_for_step()
    assert {getattr(t, "name", None) for t in normal} != {plan_name}


# --- [REL-RC-F] current_revision_instruction: session-tied source for the synth-step fallback ---


def _user_msg(text, seq):
    return MessageEvent(
        source=EventSource.USER, message=LLMMessage(role="user", content=text), seq=seq
    )


def _env_msg(text, seq):  # the force_submit_plan directive is ENVIRONMENT-injected (role=user)
    return MessageEvent(
        source=EventSource.ENVIRONMENT, message=LLMMessage(role="user", content=text), seq=seq
    )


def test_current_revision_instruction_returns_user_followup():
    events = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=2),
        _user_msg("change every CTA button to Get Started", seq=5),
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=6),
    ]
    assert signals.current_revision_instruction(events) == "change every CTA button to Get Started"


def test_current_revision_instruction_excludes_env_force_submit_directive():
    # The ENVIRONMENT force-submit directive must NOT be mistaken for the user's instruction.
    events = [
        _user_msg("add a footer with Copyright 2026", seq=5),
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=6),
        _env_msg("<system-reminder>call submit_plan NOW…</system-reminder>", seq=8),
    ]
    assert signals.current_revision_instruction(events) == "add a footer with Copyright 2026"


def test_current_revision_instruction_ignores_user_msg_after_planning():
    # Session-tied: only a user msg AT/BEFORE the current planning marker triggered this replan.
    events = [
        _user_msg("the revision instruction", seq=5),
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=6),
        _user_msg("a later unrelated message", seq=9),
    ]
    assert signals.current_revision_instruction(events) == "the revision instruction"


def test_current_revision_instruction_none_when_no_user():
    events = [StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=6)]
    assert signals.current_revision_instruction(events) is None


# --- [REL-RC-F] the synth plan MUST be a fresh PlanEvent (new id), not model_copy -----------------


def test_synth_planevent_needs_fresh_id_not_model_copy():
    """Codex code-gate pin: model_copy PRESERVES the id → the store dedups it (idempotent on
    (conversation_id, id)) and _latest_plan keeps returning the EMPTY plan → stranded execution.
    A freshly-constructed PlanEvent gets a distinct id and persists as a new event."""
    from disco.core.events import PlanEvent, PlanStep

    empty = PlanEvent(summary="s", steps=[])
    synth = PlanEvent(summary="s", steps=[PlanStep(title="do the edit")], revision=2)
    assert synth.id != empty.id  # fresh construction → distinct id (the fix)
    # the trap the fix avoids: model_copy reuses the id
    assert empty.model_copy(update={"steps": [PlanStep(title="do the edit")]}).id == empty.id


@pytest.mark.asyncio
async def test_fresh_synth_plan_wins_latest_plan_in_store():
    """End-to-end pin: after an empty plan, a fresh synth PlanEvent persists and _latest_plan
    returns IT (with the step), so approval audit/context seeding see the real 1-step plan."""
    from disco.core import SqliteEventStore
    from disco.core.events import PlanEvent, PlanStep
    from disco.core.view import _latest_plan

    store = SqliteEventStore(":memory:")
    await store.append("c", PlanEvent(summary="s", steps=[]))
    await store.append(
        "c", PlanEvent(summary="s", steps=[PlanStep(title="do the edit")], revision=2)
    )
    latest = _latest_plan(await store.get_events("c"))
    assert latest is not None and [s.title for s in latest.steps] == ["do the edit"]
