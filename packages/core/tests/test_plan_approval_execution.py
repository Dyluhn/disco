"""§20.4 Plan-approval execution contract — driven against the REAL loop
(guidelines §11.2, §20.4).

The idempotency / mode-flip / no-task-duplication contracts hold and pass. The
"runtime kick after approval produces an action or a terminal failure" contract
is now SATISFIED (Build Soak repair #2): finish.py gate_execution_nudge caps at
3 nudges then terminalizes the run STUCK with detail `approve_plan_no_execution`
(a terminal explicit failure per §11.2) instead of a false FINISHED. See
docs/build-soak-surfaced-bugs.md.
"""

from __future__ import annotations

from _buildsoak_fakes import build_plan_loop
from disco.core import ActionEvent, ConversationStatus, EventSource, MessageEvent, StatusEvent
from disco.core.llm import OperatingMode
from loop_fakes import ScriptedAgent, action_step, finish_step


def _submit_plan_step():
    return action_step("submit_plan", {"summary": "p", "steps": [{"title": "do"}]})


async def _planned(cid):
    agent = ScriptedAgent([_submit_plan_step()])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("build the thing")
    await loop.run()
    return loop, store


async def test_approve_plan_flips_mode_to_execution():
    loop, _store = await _planned("appr-flip")
    assert loop.mode == OperatingMode.PLANNING
    await loop.approve_plan()
    assert loop.mode == OperatingMode.LONG_HORIZON


async def test_approve_plan_is_idempotent():
    """A second approve_plan on a non-pending state is a no-op — exactly one
    plan_approved status is emitted."""
    loop, store = await _planned("appr-idem")
    await loop.approve_plan()
    await loop.approve_plan()  # idempotent
    approvals = [
        e
        for e in await store.get_events("appr-idem")
        if isinstance(e, StatusEvent) and e.detail == "plan_approved"
    ]
    assert len(approvals) == 1


async def test_execution_does_not_duplicate_user_task():
    """Approval does not re-append the user's original task message."""
    loop, store = await _planned("appr-nodup")
    await loop.approve_plan()
    user_msgs = [
        e
        for e in await store.get_events("appr-nodup")
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and "build the thing" in (e.message.content or "")
    ]
    assert len(user_msgs) == 1


async def test_kick_after_approval_produces_action_or_terminal_failure():
    """After approval, running the loop must produce a real execution action OR a
    terminal explicit failure — never a silent FINISHED with no work. A plan
    approved but never executed terminalizes STUCK:approve_plan_no_execution
    (the §11.2 terminal-failure case; Build Soak repair #2)."""
    loop, store = await _planned("appr-exec")
    await loop.approve_plan()
    # An agent that immediately tries to finish without doing any work.
    loop.agent = ScriptedAgent([finish_step()])
    await loop.run()

    events = await store.get_events("appr-exec")
    has_action = any(isinstance(e, ActionEvent) for e in events)
    terminal_failure = any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.STUCK
        and e.detail == "approve_plan_no_execution"
        for e in events
    )
    # The terminal must NOT be a false FINISHED.
    finished = any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
        for e in events
    )
    assert has_action or terminal_failure
    assert not finished, "approved-but-no-execution must not land FINISHED"


async def test_approval_then_no_action_is_bounded_stuck_not_finished():
    """REGRESSION (Build Soak repair #2, negative): submit_plan → approve →
    repeated no-action finish turns drive the W-5 execution-nudge gate to its cap
    (3) → the run terminalizes STUCK:approve_plan_no_execution, bounded (well
    before max_iterations) and NEVER a false FINISHED. Driven against the REAL
    loop the way the RCA specifies (the finish-path nudge gate, not the actionless
    valve)."""
    from disco.core.loop.finish import _EXECUTION_NUDGE_CAP

    loop, store = await _planned("appr-noexec-stuck")
    await loop.approve_plan()
    # Repeats finish forever — every turn is a no-tool finish, no productive work.
    loop.agent = ScriptedAgent([finish_step()])
    await loop.run()

    state = await store.get_state("appr-noexec-stuck")
    events = await store.get_events("appr-noexec-stuck")
    # Terminalized STUCK with the named detail — NOT FINISHED.
    assert state.execution_status == ConversationStatus.STUCK
    terminal = next(
        e
        for e in reversed(events)
        if isinstance(e, StatusEvent) and e.status == ConversationStatus.STUCK
    )
    assert terminal.detail == "approve_plan_no_execution"
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
        for e in events
    )
    # Bounded: the gate fired its full cap (no infinite loop) and the run landed
    # in a handful of turns (the script's finish repeats but the loop halted).
    assert loop._execution_nudges >= _EXECUTION_NUDGE_CAP
    # No productive action ever happened (the precondition for the failure).
    assert not any(isinstance(e, ActionEvent) for e in events)


async def test_approval_then_real_action_still_finishes():
    """REGRESSION (Build Soak repair #2, positive): submit_plan → approve → a real
    shell action (observation) → finish → FINISHED. A legit run with ≥1 productive
    action after approval still finishes — the STUCK terminal is reserved for the
    zero-action-after-approval case and must NOT regress a normal completion."""
    loop, store = await _planned("appr-exec-ok")
    await loop.approve_plan()
    # Do real work (a shell action → observation), then declare done.
    loop.agent = ScriptedAgent([action_step("shell", {"command": "echo hi"}), finish_step()])
    await loop.run()

    state = await store.get_state("appr-exec-ok")
    events = await store.get_events("appr-exec-ok")
    # A productive action happened...
    assert any(isinstance(e, ActionEvent) for e in events)
    # ...so the run FINISHES — not STUCK/approve_plan_no_execution.
    assert state.execution_status == ConversationStatus.FINISHED
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.STUCK
        and e.detail == "approve_plan_no_execution"
        for e in events
    )
