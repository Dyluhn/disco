"""§20.4 Plan-approval execution contract — driven against the REAL loop
(guidelines §11.2, §20.4).

The idempotency / mode-flip / no-task-duplication contracts hold and pass. The
"runtime kick after approval produces an action or a terminal failure" contract is
violated by the current nudge-release behavior (finish.py gate_execution_nudge caps
at 3 nudges then FINISHES with a warning — no action, no terminal FAILURE) and is
xfail(strict=True). See docs/build-soak-surfaced-bugs.md.
"""

from __future__ import annotations

import pytest
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


@pytest.mark.xfail(
    reason="BUG APPROVE_PLAN_NO_EXECUTION: after approval, an agent that finishes "
    "without working is nudged 3× then RELEASED to FINISHED with a warning "
    "(finish.py gate_execution_nudge ~L1008) — no action and no terminal FAILURE, "
    "violating §11.2/§20.4 'at least one action or terminal explicit failure' — "
    "repair-loop target",
    strict=True,
)
async def test_kick_after_approval_produces_action_or_terminal_failure():
    """After approval, running the loop must produce a real execution action OR a
    terminal explicit failure (ERROR) — never a silent FINISHED with no work."""
    loop, store = await _planned("appr-exec")
    await loop.approve_plan()
    # An agent that immediately tries to finish without doing any work.
    loop.agent = ScriptedAgent([finish_step()])
    await loop.run()

    events = await store.get_events("appr-exec")
    has_action = any(isinstance(e, ActionEvent) for e in events)
    terminal_error = any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR for e in events
    )
    assert has_action or terminal_error
