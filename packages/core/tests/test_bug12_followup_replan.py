"""Bug 12 (§11.4, Build Soak repair #6) — a CHANGE/REVISION follow-up on an
approved/finished build must RE-ENTER PLANNING so the revision goes through a
revised plan, not a free write on the stale approved plan (NO_REPLAN_AFTER_REVISION).

Driven against the REAL loop via the PLAIN product follow-up path (`send_message`
/ a mid-run store append) — NOT `request_plan`/`enter_planning` — reproducing the
exact live-soak conditions the soak surfaced:
  * revise_after_finish  -> the FINISHED->followup intake path (run()).
  * steer_while_running   -> the mid-run RUNNING->steer path (_run_drive()).
Plus a negative Q&A guard: a pure non-mutating question must stay answerable
without a pointless forced re-plan.
"""

from __future__ import annotations

from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.llm import OperatingMode
from disco.core.loop import AgentStep
from loop_fakes import ScriptedAgent, action_step, finish_step


def _submit_plan_step(summary: str = "p") -> AgentStep:
    return action_step("submit_plan", {"summary": summary, "steps": [{"title": "do"}]})


def _write_step(content: str, path: str = "index.html") -> AgentStep:
    return action_step("file_write", {"path": path, "content": content})


def _read_step(path: str = "index.html") -> AgentStep:
    return action_step("file_read", {"path": path})


def _silent_noop() -> AgentStep:
    """A tool-less step with EMPTY thought — handle_noop_step persists NOTHING
    (turn_control.py:1182), so it does not advance 'progress' past a follow-up the
    same step injects. Lets a mid-run follow-up become the latest event for the
    next _run_drive re-entry check (deterministic steer simulation)."""
    return AgentStep(thought="", tool_call=None, finished=False)


async def _finished_first_build(cid: str):
    """plan rev1 -> approve -> a real write + finish -> FINISHED."""
    agent = ScriptedAgent([_submit_plan_step("first")])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("Create index.html with a blue hero 'Launch Day'.")
    await loop.run()  # -> AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    loop.agent = ScriptedAgent([_write_step("<h1>Launch Day</h1>"), finish_step()])
    await loop.run()  # execute + finish
    return loop, store


def _seq_of_user(events, needle: str) -> int:
    return next(
        e.seq
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and needle in (e.message.content or "")
    )


# --------------------------------------------------------------------------- #
# Test 1 — revise_after_finish: re-enter PLANNING, defer the stale-plan write.  #
# --------------------------------------------------------------------------- #
async def test_change_followup_after_finish_reenters_planning_and_defers_write():
    cid = "bug12-finish-defer"
    loop, store = await _finished_first_build(cid)
    assert (await loop.get_state()).execution_status == ConversationStatus.FINISHED
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]

    # The model, given the change, tries to WRITE first (the bug) — then, after the
    # planning gate rejects it, submits a revised plan.
    loop.agent = ScriptedAgent(
        [_write_step("<h1>Grand Opening</h1>"), _submit_plan_step("second")]
    )
    await loop.send_message(
        "Revise the hero heading to 'Grand Opening' and add a pricing section."
    )
    await loop.run()

    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "Grand Opening")

    # (a) re-entered PLANNING after the follow-up (the durable `planning` marker).
    assert any(
        isinstance(e, StatusEvent)
        and e.detail == "planning"
        and (e.seq or 0) > followup_seq
        for e in events
    ), "follow-up did not re-enter PLANNING"
    assert loop.mode == OperatingMode.PLANNING

    # (b) the stale-plan write did NOT execute.
    assert "Grand Opening" not in (executor.world.get("index.html") or "")
    assert not any(
        isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
        and (e.seq or 0) > followup_seq
        for e in events
    ), "a write executed before the revised plan was approved"

    # (c) a revised plan rev 2 was submitted; not yet approved.
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    assert (
        await loop.get_state()
    ).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


# --------------------------------------------------------------------------- #
# Test 2 — after approving the revision, the write succeeds; ordered chain.     #
# --------------------------------------------------------------------------- #
async def test_revised_plan_approval_then_write_succeeds_with_ordered_chain():
    cid = "bug12-finish-chain"
    loop, store = await _finished_first_build(cid)
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]

    loop.agent = ScriptedAgent(
        [_write_step("<h1>Grand Opening</h1>"), _submit_plan_step("second")]
    )
    await loop.send_message("Revise the hero heading to 'Grand Opening'.")
    await loop.run()  # -> AWAITING_PLAN_APPROVAL (rev 2)
    assert (
        await loop.get_state()
    ).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    await loop.approve_plan()  # approve the revision
    loop.agent = ScriptedAgent([_write_step("<h1>Grand Opening</h1>"), finish_step()])
    await loop.run()  # NOW the write executes

    assert "Grand Opening" in (executor.world.get("index.html") or "")

    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "Grand Opening")
    rev2_seq = next(
        e.seq for e in events if isinstance(e, PlanEvent) and e.revision == 2
    )
    awaiting_seq = next(
        e.seq
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.AWAITING_PLAN_APPROVAL
        and (e.seq or 0) > followup_seq
    )
    approved_seq = next(
        e.seq
        for e in events
        if isinstance(e, StatusEvent)
        and e.detail == "plan_approved"
        and (e.seq or 0) > followup_seq
    )
    write_seq = next(
        e.seq
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
        and (e.seq or 0) > approved_seq
    )
    # The exact revision oracle's expected chain.
    assert followup_seq < rev2_seq < awaiting_seq < approved_seq < write_seq


def _steer_injector(store, cid: str):
    async def _inject() -> None:
        # A real product steer lands as a plain USER message mid-run (WS append),
        # NOT request_plan. Appended directly to the store (bypasses the lock the
        # drive loop holds) — exactly the live RUNNING->steer condition.
        await store.append(
            cid,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(
                    role="user",
                    content="Also add a Contact page and update the nav links.",
                ),
            ),
        )

    return _inject


# --------------------------------------------------------------------------- #
# Test 3 — steer_while_running, THE IN-FLIGHT RACE: a change steer that lands   #
# DURING an in-flight drive_step (the step itself returns a WRITE) must NOT get #
# one write through on the OLD plan — the apply-time gate defers it; only an    #
# approved revised plan (rev 2) lets the write land. The top-of-loop check      #
# alone cannot catch this (the steer arrives after it ran).                     #
# --------------------------------------------------------------------------- #
async def test_change_followup_mid_step_write_is_deferred_until_revised_plan():
    cid = "bug12-steer-race"
    agent = ScriptedAgent([_submit_plan_step("first")])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("Create a two-page static site with Home and About.")
    await loop.run()
    await loop.approve_plan()
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]

    # `before={0: inject}` fires at the START of the step that RETURNS the write —
    # the steer lands WHILE the write step is in flight, AFTER the top-of-loop
    # re-plan check already ran. The apply-time gate must still defer the write.
    loop.agent = ScriptedAgent(
        [_write_step("<h1>Contact</h1>", path="contact.html"),
         _submit_plan_step("second")],
        before={0: _steer_injector(store, cid)},
    )
    await loop.run()  # -> AWAITING_PLAN_APPROVAL (rev 2); the mid-step write deferred

    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "Contact page")

    # re-entered PLANNING off the in-flight steer (the apply-time gate path).
    assert any(
        isinstance(e, StatusEvent)
        and e.detail == "planning"
        and (e.seq or 0) > followup_seq
        for e in events
    ), "in-flight steer did not re-enter PLANNING"
    # the mid-step write on the OLD plan did NOT land (deferred, no successful obs).
    assert "contact.html" not in executor.world
    assert not any(
        isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
        and (e.seq or 0) > followup_seq
        for e in events
    ), "a write executed on the OLD plan from an in-flight steer"
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    assert (
        await loop.get_state()
    ).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    # Only an APPROVED revised plan lets the write through — the full chain holds
    # even though the steer arrived mid-step.
    await loop.approve_plan()
    loop.agent = ScriptedAgent(
        [_write_step("<h1>Contact</h1>", path="contact.html"), finish_step()]
    )
    await loop.run()
    assert "contact.html" in executor.world

    events = await store.get_events(cid)
    rev2_seq = next(
        e.seq for e in events if isinstance(e, PlanEvent) and e.revision == 2
    )
    approved_seq = next(
        e.seq
        for e in events
        if isinstance(e, StatusEvent)
        and e.detail == "plan_approved"
        and (e.seq or 0) > followup_seq
    )
    write_seq = next(
        e.seq
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
        and (e.seq or 0) > approved_seq
    )
    assert followup_seq < rev2_seq < approved_seq < write_seq


# --------------------------------------------------------------------------- #
# Test 3b — steer that arrives BETWEEN steps (the top-of-loop _run_drive check).#
# A silent no-op step injects the steer (it persists nothing, so the steer is   #
# the latest event for the NEXT iteration's top-of-loop check), re-entering     #
# PLANNING; the subsequent write is then rejected by the planning gate.         #
# --------------------------------------------------------------------------- #
async def test_change_followup_between_steps_reenters_planning():
    cid = "bug12-steer-between"
    agent = ScriptedAgent([_submit_plan_step("first")])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("Create a two-page static site with Home and About.")
    await loop.run()
    await loop.approve_plan()
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]

    loop.agent = ScriptedAgent(
        [_silent_noop(), _write_step("<h1>Contact</h1>", path="contact.html"),
         _submit_plan_step("second")],
        before={0: _steer_injector(store, cid)},
    )
    await loop.run()

    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "Contact page")
    assert any(
        isinstance(e, StatusEvent)
        and e.detail == "planning"
        and (e.seq or 0) > followup_seq
        for e in events
    ), "between-steps steer did not re-enter PLANNING"
    assert "contact.html" not in executor.world
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    assert (
        await loop.get_state()
    ).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


# --------------------------------------------------------------------------- #
# Test 3c — the READ-then-WRITE in-flight window (codex residual): the steer    #
# lands while the in-flight tool is an ALLOWED READ. The read may proceed, BUT  #
# planning must be re-entered ON DETECTION so the model's NEXT write is rejected #
# on the old plan — otherwise the read's ActionEvent buries the steer marker and #
# the write slips through. Only an approved rev 2 lets the write land.          #
# --------------------------------------------------------------------------- #
async def test_change_followup_during_in_flight_read_still_gates_next_write():
    cid = "bug12-steer-read"
    agent = ScriptedAgent([_submit_plan_step("first")])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("Create a two-page static site with Home and About.")
    await loop.run()
    await loop.approve_plan()
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]

    # before[0] fires at the START of the in-flight READ step (the steer lands
    # while the read is in flight). step1 is the WRITE the model then attempts.
    loop.agent = ScriptedAgent(
        [_read_step("index.html"),
         _write_step("<h1>Contact</h1>", path="contact.html"),
         _submit_plan_step("second")],
        before={0: _steer_injector(store, cid)},
    )
    await loop.run()  # -> AWAITING_PLAN_APPROVAL (rev 2); the next write rejected

    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "Contact page")

    # planning re-entered ON DETECTION even though the in-flight tool was a READ.
    assert any(
        isinstance(e, StatusEvent)
        and e.detail == "planning"
        and (e.seq or 0) > followup_seq
        for e in events
    ), "in-flight read did not re-enter PLANNING on steer detection"
    # the read MAY have proceeded (harmless) — but the WRITE did NOT land.
    assert "contact.html" not in executor.world
    assert not any(
        isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
        and (e.seq or 0) > followup_seq
        for e in events
    ), "a write slipped through after a read-in-flight steer (read-then-write window)"
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    assert (
        await loop.get_state()
    ).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    # Only an APPROVED revised plan lets the write through.
    await loop.approve_plan()
    loop.agent = ScriptedAgent(
        [_write_step("<h1>Contact</h1>", path="contact.html"), finish_step()]
    )
    await loop.run()
    assert "contact.html" in executor.world


# --------------------------------------------------------------------------- #
# Test 4 (negative) — a pure Q&A follow-up is answered WITHOUT a forced re-plan.#
# --------------------------------------------------------------------------- #
async def test_question_followup_after_finish_does_not_force_replan():
    cid = "bug12-qa"
    loop, store = await _finished_first_build(cid)

    # A pure, non-mutating question. The model answers (notify_user) then finishes.
    loop.agent = ScriptedAgent(
        [action_step("notify_user", {"message": "The hero uses the Inter typeface."}),
         finish_step()]
    )
    await loop.send_message("What font did you use?")
    await loop.run()

    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "What font")

    # NO forced re-plan: no planning marker, no AWAITING gate, no new PlanEvent.
    assert not any(
        isinstance(e, StatusEvent)
        and e.detail == "planning"
        and (e.seq or 0) > followup_seq
        for e in events
    ), "a pure Q&A follow-up was wrongly forced into a re-plan"
    assert loop.mode != OperatingMode.PLANNING
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1]

    # The question WAS answerable — the agent's answer is in the log, and the run
    # reached a terminal-for-now state (no dead-end / hang).
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "Inter" in (e.message.content or "")
        and (e.seq or 0) > followup_seq
        for e in events
    )
    final = (await loop.get_state()).execution_status
    assert final != ConversationStatus.AWAITING_PLAN_APPROVAL
    assert final not in (ConversationStatus.RUNNING,)


# --------------------------------------------------------------------------- #
# Test 5 — DURABLE NO_REPLAN fix: deterministic re-plan at steer INGEST.        #
# The two guards above are POLLING-based (store-append path) and race with an   #
# in-flight turn. A steer via the PRODUCT steer() path re-enters PLANNING the    #
# MOMENT it arrives — BEFORE the loop drives a single step — so it cannot race.  #
# --------------------------------------------------------------------------- #
async def test_steer_ingest_immediately_reenters_planning_before_any_drive():
    cid = "noreplan-ingest"
    loop, store = await _finished_first_build(cid)
    assert loop.mode != OperatingMode.PLANNING

    # The scope-adding revision steer via the PRODUCT path. NO run() yet.
    await loop.steer("Also add a Contact page and update the nav links.")

    # PLANNING entered AT INGEST — deterministic, before any drive step.
    assert loop.mode == OperatingMode.PLANNING
    events = await store.get_events(cid)
    followup_seq = _seq_of_user(events, "Contact page")
    assert any(
        isinstance(e, StatusEvent)
        and e.detail == "planning"
        and (e.seq or 0) > followup_seq
        for e in events
    ), "ingest steer did not re-enter PLANNING immediately"


async def test_qa_steer_ingest_does_not_reenter_planning():
    """A pure Q&A steer at ingest must NOT force a re-plan (is_revision_intent exempts)."""
    cid = "noreplan-ingest-qa"
    loop, _store = await _finished_first_build(cid)
    await loop.steer("What font did you use?")
    assert loop.mode != OperatingMode.PLANNING


async def test_steer_ingest_then_stale_write_is_refused_end_to_end():
    """After an ingest steer flips PLANNING, a model that still tries the stale-plan
    write is refused; only an approved rev 2 lets it land (full chain via ingest path)."""
    cid = "noreplan-ingest-e2e"
    loop, store = await _finished_first_build(cid)
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]

    await loop.steer("Also add a Contact page and update the nav links.")
    assert loop.mode == OperatingMode.PLANNING
    # The model tries a write first (stale plan) then submits the revised plan.
    loop.agent = ScriptedAgent(
        [_write_step("<h1>Contact</h1>", path="contact.html"), _submit_plan_step("second")]
    )
    await loop.run()  # -> AWAITING_PLAN_APPROVAL; the stale write deferred
    assert "contact.html" not in executor.world
    events = await store.get_events(cid)
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    await loop.approve_plan()
    loop.agent = ScriptedAgent(
        [_write_step("<h1>Contact</h1>", path="contact.html"), finish_step()]
    )
    await loop.run()
    assert "contact.html" in executor.world


# --------------------------------------------------------------------------- #
# Test 6 — THE LIVE RACE fix1 missed (codex RCA2): the kernel appends a durable  #
# `revision_steer_pending` marker; the loop must re-enter PLANNING off it EVEN   #
# WHEN in-flight agent activity has MASKED latest_unprocessed_user_text (the     #
# predicate the old polling guards relied on). This is the exact live           #
# steer_while_running failure (steer seq N, masked, write would land on seq N+k).#
# --------------------------------------------------------------------------- #
async def test_live_steer_marker_reenters_planning_when_unprocessed_text_masked():
    from disco.core import ConversationStatus
    from disco.core.loop import signals

    cid = "noreplan-marker-race"
    agent = ScriptedAgent([_submit_plan_step("first")])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("Create a two-page static site with Home and About.")
    await loop.run()
    await loop.approve_plan()
    assert loop.mode != OperatingMode.PLANNING

    # LIVE kernel steer sequence: user(steer) msg → an in-flight AGENT message (masks
    # has_unprocessed_user_message → latest_unprocessed_user_text becomes None) → the
    # durable revision_steer_pending marker the DiscoKernel ingress appends.
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="Also add a Contact page and update nav."),
            meta={"steer": True},
        ),
    )
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content="Working on it."),
        ),
    )
    await store.append(
        cid, StatusEvent(status=ConversationStatus.RUNNING, detail="revision_steer_pending")
    )

    events = await store.get_events(cid)
    # Precondition that PROVES we exercise the race: the text predicate is masked.
    assert signals.latest_unprocessed_user_text(events) is None
    assert signals.pending_revision_steer(events) is True

    # The marker path must re-enter PLANNING despite the masked text predicate.
    reentered = await loop._maybe_reenter_planning_for_followup(events)
    assert reentered is True
    assert loop.mode == OperatingMode.PLANNING

    # …and consuming it is idempotent: pending is now False (a `planning` was emitted).
    events2 = await store.get_events(cid)
    assert signals.pending_revision_steer(events2) is False
