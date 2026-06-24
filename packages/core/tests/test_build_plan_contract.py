"""§20.1 Planning contract — bare Build plan-gate, driven against the REAL loop.

These assert the SPEC contract (guidelines §11.1, §15.2, §20.1). Where the product
violates the spec the test is marked `xfail(strict=True)` with the surfaced bug code,
so the suite stays green AND the bug flips to a hard failure the moment it is fixed.
See docs/build-soak-surfaced-bugs.md for the repair backlog.
"""

from __future__ import annotations

import pytest
from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import ActionEvent, AgentErrorEvent, ObservationEvent, PlanEvent
from disco.core.events import ConversationStatus, EventKind
from loop_fakes import ScriptedAgent, action_step

# Tools a PLANNING agent must NEVER be offered (writes/exec/serve) — §15.2.
_MUTATING = {"file_write", "write_file", "edit", "apply_patch", "shell", "serve", "browser"}


def _submit_plan_step(summary="p", steps=None):
    return action_step("submit_plan", {"summary": summary, "steps": steps or [{"title": "do"}]})


async def test_first_turn_planning_only_exposes_safe_tools():
    """The PLANNING agent is offered only read/explore + plan/ask tools — no
    mutation (§11.1 first-turn invariant)."""
    agent = ScriptedAgent([_submit_plan_step()])
    loop, _store = build_plan_loop(agent, conversation_id="plan-safe")
    await loop.send_message("create a landing page")
    await loop.run()  # halts at AWAITING_PLAN_APPROVAL

    offered = set(agent.seen_tools[0])
    assert "submit_plan" in offered, offered
    leaked = offered & _MUTATING
    assert not leaked, f"PLANNING offered mutating tools: {leaked}"


@pytest.mark.xfail(
    reason="BUG THINK_NOT_EXPOSED_IN_PLANNING: production planning allowlist "
    "(runtime.py:1467) omits `think`, though §20.1/§15.2 list it as an allowed "
    "first move — repair-loop target",
    strict=True,
)
async def test_first_turn_planning_exposes_think():
    """§20.1 lists `think` among the allowed first moves in PLANNING; the product
    does not offer it."""
    agent = ScriptedAgent([_submit_plan_step()])
    loop, _store = build_plan_loop(agent, conversation_id="plan-think")
    await loop.send_message("create a landing page")
    await loop.run()
    assert "think" in set(agent.seen_tools[0])


async def test_write_tool_in_planning_produces_recoverable_rejection():
    """A write tool called during PLANNING must produce a recoverable rejection
    (an AgentErrorEvent or a failed observation), NOT a successful write."""
    agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "bad"}),
            _submit_plan_step(),
        ]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="plan-write", executor=executor)
    await loop.send_message("create a page")
    await loop.run()

    events = await store.get_events("plan-write")
    writes = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    ]
    assert writes, "no file_write action recorded"
    write_id = writes[0].id
    rejected = any(isinstance(e, AgentErrorEvent) and e.action_id == write_id for e in events)
    executed_ok = any(
        isinstance(e, ObservationEvent)
        and e.action_id == write_id
        and e.tool_result.success
        for e in events
    )
    # SPEC: the write is rejected, not executed.
    assert rejected and not executed_ok
    assert "index.html" not in executor.world  # the file must NOT have been written


async def test_submit_plan_resets_planning_read_counter():
    """submit_plan resets the consecutive-planning-read counter (engine.py:852)."""
    agent = ScriptedAgent(
        [
            action_step("file_read", {"path": "a.txt"}),
            action_step("file_read", {"path": "b.txt"}),
            _submit_plan_step(),
        ]
    )
    loop, _store = build_plan_loop(agent, conversation_id="plan-reset")
    await loop.send_message("build")
    await loop.run()
    assert loop._plan_explore_reads == 0


async def test_plan_revision_increments_on_followup_planning():
    """A follow-up planning turn produces revision = previous + 1 (plans.py:147)."""
    agent = ScriptedAgent([_submit_plan_step(summary="first")])
    loop, store = build_plan_loop(agent, conversation_id="plan-rev")
    await loop.send_message("build")
    await loop.run()
    await loop.approve_plan()

    # re-plan (a follow-up planning turn after the first plan was approved)
    loop.agent = ScriptedAgent([_submit_plan_step(summary="second")])
    await loop.enter_planning("revise the heading")
    await loop.run()

    events = await store.get_events("plan-rev")
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]
    # the run halted awaiting approval of the revised plan
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert EventKind.PLAN  # import smoke
