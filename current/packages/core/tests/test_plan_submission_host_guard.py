"""Host-owned planning prerequisites funnel the model back to the required read."""

from __future__ import annotations

import pytest
from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import ActionEvent, AgentErrorEvent, PlanEvent
from loop_fakes import ScriptedAgent, action_step


def _submit(summary: str):
    return action_step(
        "submit_plan",
        {"summary": summary, "steps": [{"title": "Build from the references"}]},
    )


@pytest.mark.asyncio
async def test_host_plan_guard_refuses_once_then_accepts_the_repaired_submission() -> None:
    executor = BuildExecutor()
    checks = 0

    def guard(_events):
        nonlocal checks
        checks += 1
        return "Read `references/Brief/PACK.md`, then resubmit." if checks == 1 else None

    executor.plan_submission_refusal = guard
    loop, store = build_plan_loop(
        ScriptedAgent([_submit("first"), _submit("grounded")]),
        conversation_id="plan-host-guard",
        executor=executor,
    )

    await loop.send_message("Build the project")
    await loop.run()
    events = await store.get_events("plan-host-guard")

    refusals = [
        event for event in events if isinstance(event, AgentErrorEvent) and "PACK.md" in event.error
    ]
    assert len(refusals) == 1
    refused_actions = [
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusals[0].action_id
    ]
    assert len(refused_actions) == 1
    assert refused_actions[0].tool_call.tool_name == "submit_plan"
    assert len([event for event in events if isinstance(event, PlanEvent)]) == 1
