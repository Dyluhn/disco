"""Unit 22 — planning-gate refusal is repetition-aware via the real gate.

Old-red: streak=1 emitted only the bare suffix (no count, no consequence, no
next move) — the first refusal had no repetition signal at all. Streak 2
already carried an escalation (``Planning-mode refusal 2: ... ONLY valid next
action``), so the first and second complete messages were NOT byte-identical,
but the first still violated constraint 4's "at minimum" (every fire carries a
live count, the narrowing consequence, and one next move).

New-green: every refusal, including the first, carries a live count, the
narrowing consequence, and exactly ONE next move
(``call `submit_plan` with the current plan`` — not ``submit_plan`` OR a read).
The second consecutive refusal changes bytes and count.

This test drives the REAL async production caller
`planning_gates._refuse_planning_tool`, captures the actual emitted
`AgentErrorEvent`, and proves the byte change end-to-end.  It does not
manufacture a synthetic error and does not call only
`_planning_tool_refusal_message`.
"""

from __future__ import annotations

import pytest
from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core.loop.driver_retry import _PLANNING_TOOL_REFUSAL_NEEDLE
from disco.core import AgentErrorEvent
from loop_fakes import ScriptedAgent, action_step


def _submit_plan_step(summary: str = "p"):
    return action_step("submit_plan", {"summary": summary, "steps": [{"title": "do"}]})


async def _collect_planning_refusals(*, shells_before_plan: int) -> list[AgentErrorEvent]:
    """Run a PLANNING-mode loop with N disallowed shells then a plan, return refusals."""
    steps = [action_step("shell", {"cmd": "pwd"}) for _ in range(shells_before_plan)]
    steps.append(_submit_plan_step("plan after refusals"))
    agent = ScriptedAgent(steps)
    executor = BuildExecutor()
    # Mirror the escalation test's readonly shape so force_submit_read_calls_remaining is deterministic
    executor.readonly_tool_names = lambda: frozenset(  # type: ignore[attr-defined]
        {"file_read", "file_list", "search", "extract", "think"}
    )
    loop, store = build_plan_loop(
        agent,
        conversation_id=f"ut22-repetition-{shells_before_plan}",
        executor=executor,
    )
    await loop.send_message("create a page")
    await loop.run()
    events = await store.get_events(f"ut22-repetition-{shells_before_plan}")
    refusals = [
        e for e in events if isinstance(e, AgentErrorEvent) and _PLANNING_TOOL_REFUSAL_NEEDLE in e.error
    ]
    return refusals


@pytest.mark.asyncio
async def test_unit22_first_planning_refusal_via_real_gate_has_count_consequence_next_move():
    """First refusal via the real gate already carries count + consequence + next move."""
    refusals = await _collect_planning_refusals(shells_before_plan=1)
    assert len(refusals) == 1
    first = refusals[0].error
    # Live count (old-red had no count at streak 1)
    assert "refusal 1" in first.lower()
    # Consequence: how many reads remain before narrowing
    assert "read calls remaining" in first.lower()
    assert "allowed tools narrow" in first.lower()
    # One next move — exactly ONE action: submit_plan with the current plan, not an OR
    assert "Your next move:" in first
    assert "call `submit_plan` with the current plan" in first
    assert "or use a read tool" not in first.lower(), (
        "Unit 22 correction: the first-refusal next move must name exactly ONE action "
        "(submit_plan), not `submit_plan` OR a read"
    )
    # Still contains the base suffix / needle (F63)
    from disco.core.loop.engine_contracts import _PLANNING_TOOL_REFUSAL_SUFFIX

    assert _PLANNING_TOOL_REFUSAL_SUFFIX in first
    assert _PLANNING_TOOL_REFUSAL_NEEDLE in first


@pytest.mark.asyncio
async def test_unit22_consecutive_second_planning_refusal_via_real_gate_changes_bytes_and_count():
    """A consecutive second refusal changes bytes and count (not byte-identical to the first)."""
    first_batch = await _collect_planning_refusals(shells_before_plan=1)
    two_batch = await _collect_planning_refusals(shells_before_plan=2)
    assert len(first_batch) == 1
    assert len(two_batch) == 2
    first = first_batch[0].error
    second = two_batch[1].error
    # Bytes change
    assert first != second, "streak 1 and streak 2 must not render byte-identically"
    # Count increments — note: planning_tool_refusal_streak counts consecutive
    # planning refusals (any tool), not retries of one tool name.
    assert "refusal 1" in first.lower()
    assert "refusal 2" in second.lower()
    # Second also carries consequence / next move (escalation adds ONLY-valid clause)
    assert "read calls remaining" in second.lower()
    assert "ONLY valid next action" in second or "Your next move" in second
    # Length grows (second is longer / contains escalated guidance)
    assert len(second) != len(first)
