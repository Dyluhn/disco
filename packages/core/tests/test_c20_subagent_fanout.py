"""C20 — bounded subagent fan-out (read-only Explore/Plan helper dispatch+join).

# Contract

A build/agent loop can fan out a single, read-only Explore/Plan helper task
(``delegate_explore``) and fold the result back as an observation on the
driver's next turn. The fan-out is BOUNDED:

  * a per-run-segment COUNT cap (``_FANOUT_MAX_PER_RUN``) — exceeding it is
    refused with a visible system-reminder; the loop never recurses past the
    cap (no unbounded fan-out, no always-on cost);
  * the helper is READ-ONLY — the cap is the only path the fan-out can
    expand along; the helper cannot mutate workspace state, cannot run shell,
    cannot write files (enforced upstream — the helper's tools are
    read-only; the engine's cap applies to nested calls too);
  * the cap is per-run-segment — a fresh ``run()`` resets the counter, so
    a resume/steer gets a fresh budget (mirrors the verify / DoD / plan-
    revision caps).

The dispatch is the loop's intercept path: the engine emits the
``ActionEvent`` (audit trail), dispatches the helper via the
``_run_fanout`` test seam, and folds the response back as a paired
``ObservationEvent`` so the driver sees the result on its next turn.

# Acceptance (this file)

  1. A driver call to ``delegate_explore`` under the cap → a paired
     ``ActionEvent`` + ``ObservationEvent`` are emitted in the same turn
     (one action, one observation, no extra events), with the helper's
     response folded back via the ObservationEvent's ``content`` /
     ``structured`` (and the result is correlated to the proposed
     ``call_id`` for KV-cache stability).
  2. Exceeding the cap → a refusal ``AgentErrorEvent`` (paired by
     ``call_id``) is emitted; NO successful ``ObservationEvent`` is
     produced (the fan-out was refused, not silently degraded); the
     cap is the load-bearing piece (a model that hammers the helper
     never gets a single observation back).
  3. The cap is per-run-segment: a fresh ``run()`` resets the counter
     so a new segment gets a fresh budget (a resume/steer is a clean
     slate, mirroring the verify / DoD / plan-revision caps).

Tests use only fakes (no real model, no real container) — same pattern as
``test_c18_plan_step_done_condition.py`` and ``test_c6_recitation_cadence.py``.
"""

from __future__ import annotations

import pytest

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.loop.engine import (
    _FANOUT_INPUT_MAX_CHARS,
    _FANOUT_MAX_PER_RUN,
)
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

CID = "conv-c20"


def _make_tool_result(*, call_id, success, content, structured=None, error=None):
    """Build a ToolResult for the test seam. The engine awaits `_run_fanout`,
    so the seam is an async-awaitable coroutine — but the production stub
    returns the ToolResult synchronously. We wrap the construction here so
    the seam stays readable, then `await` it in the engine. The ToolResult
    itself is identical either way (sync construction)."""
    from disco.core import ToolResult

    return ToolResult(
        call_id=call_id,
        tool_name="delegate_explore",
        success=success,
        content=content,
        structured=structured,
        error=error,
    )


# ---------------------------------------------------------------------------
# Helper: a custom _run_fanout override (deterministic, controllable)
# ---------------------------------------------------------------------------


class _StubFanout:
    """A controllable _run_fanout override: the test injects a list of
    (args, content, structured) responses and the override returns them in
    order (loops on the last). Each entry is a structured report the test
    can assert on. Mirrors the ScriptedAgent pattern — the same idea,
    applied to the fan-out seam."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[tuple[dict, list]] = []
        self.idx = 0

    async def __call__(self, args, events, *, call_id=""):
        # Async to match the engine's `await self._run_fanout(...)` — the
        # production stub is also async (a real LLM round-trip), so this is
        # the right shape for the seam. The ToolResult construction itself
        # is sync; we just wrap it in a coroutine so the await works.
        self.calls.append((dict(args), list(events)))
        i = min(self.idx, len(self._scripted) - 1)
        self.idx += 1
        spec = self._scripted[i]
        return _make_tool_result(
            call_id=call_id, success=spec.get("success", True),
            content=spec["content"],
            structured=spec.get("structured"),
            error=spec.get("error"),
        )


# ---------------------------------------------------------------------------
# Test 1 — under the cap: dispatch + join, paired ActionEvent + ObservationEvent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c20_fanout_under_cap_dispatches_and_folds_back_as_observation():
    """A driver call to ``delegate_explore`` under the per-run-segment cap
    produces a paired ``ActionEvent`` + ``ObservationEvent`` in the same
    turn, with the helper's response folded back. The driver sees the
    result on its next turn via the ObservationEvent's content /
    structured, and the two events are correlated by ``action_id`` +
    ``call_id`` (KV-cache stability)."""
    agent = ScriptedAgent(
        [
            # delegate_explore under the cap → should land one action + one observation
            action_step(
                "delegate_explore",
                {
                    "question": "Where should the new helper land in src/foo.py?",
                    "context": "I want a focused second look before I commit the edit.",
                },
            ),
            # Then finish. The ObservationEvent from the fan-out is what
            # the driver would have seen before this finish step — the
            # loop processed them in order, so by the time finish lands
            # the helper's report is already in the View.
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), conversation_id=CID)
    # Wire a controllable fan-out seam (the default is a deterministic
    # stub; this override gives the test a known report to assert on).
    fanout = _StubFanout(
        [
            {
                "success": True,
                "content": (
                    "src/foo.py: best landing site is `Foo.bar` (existing helper, "
                    "consistent with the call sites in src/baz.py:42 and 88)."
                ),
                "structured": {
                    "recommendation": "src/foo.py:Foo.bar",
                    "call_sites": ["src/baz.py:42", "src/baz.py:88"],
                },
            }
        ]
    )
    loop._run_fanout = fanout

    await loop.send_message("go")
    state = await loop.run()
    # The run finished cleanly (the cap didn't fire).
    assert state.execution_status == ConversationStatus.FINISHED

    events = await store.get_events(CID)
    # Exactly one ActionEvent + one ObservationEvent for the fan-out.
    fanout_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "delegate_explore"
    ]
    fanout_observations = [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result is not None
        and e.tool_result.tool_name == "delegate_explore"
    ]
    assert len(fanout_actions) == 1, (
        f"expected exactly 1 delegate_explore ActionEvent, got {len(fanout_actions)}"
    )
    assert len(fanout_observations) == 1, (
        f"expected exactly 1 delegate_explore ObservationEvent, got {len(fanout_observations)}"
    )
    # Paired by action_id (the action that produced the observation).
    assert fanout_observations[0].action_id == fanout_actions[0].id
    # Paired by call_id (KV-cache stability: the assistant tool_call's id
    # matches the tool result's call_id, so the provider adapter sees a
    # properly-correlated pair — same discipline as the remember/serve
    # intercept paths).
    action_call_id = fanout_actions[0].tool_call.call_id
    assert fanout_observations[0].tool_result.call_id == action_call_id
    # The helper's response is folded back via the ObservationEvent's
    # content + structured (NOT a silent drop, NOT a generic placeholder).
    obs = fanout_observations[0]
    assert "src/foo.py" in obs.tool_result.content
    assert "Foo.bar" in obs.tool_result.content
    assert obs.tool_result.success is True
    assert obs.tool_result.structured == {
        "recommendation": "src/foo.py:Foo.bar",
        "call_sites": ["src/baz.py:42", "src/baz.py:88"],
    }
    # The fan-out count was incremented exactly once (under the cap).
    assert loop._fanout_count == 1
    assert loop._fanout_count < loop._fanout_max  # still room in the budget
    # And the seam was called exactly once, with the right args + events.
    assert len(fanout.calls) == 1
    call_args, call_events = fanout.calls[0]
    assert call_args["question"] == "Where should the new helper land in src/foo.py?"
    assert "focused second look" in call_args["context"]
    # The events list was passed in (the helper can read the log to ground
    # its answer — the seam is event-list-in / report-out).
    assert isinstance(call_events, list)


# ---------------------------------------------------------------------------
# Test 2 — over the cap: refused, no observation, no always-on cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c20_fanout_over_cap_is_refused_no_observation():
    """Exceeding the per-run-segment cap (>= _FANOUT_MAX_PER_RUN) is
    refused with a paired ``AgentErrorEvent`` (the model sees WHY on its
    next turn) AND NO successful ``ObservationEvent`` is produced
    (a refused fan-out is not a silent success). The count cap is the
    load-bearing piece — without it, a model that hammers the helper
    would saturate context with helper round-trips and burn tokens
    forever. With the cap, the Nth + 1 call is refused cleanly."""
    # Script: cap-1 successful dispatches, then a refused (cap+1)th.
    over_count = _FANOUT_MAX_PER_RUN + 1
    scripted_steps = [
        action_step(
            "delegate_explore",
            {"question": f"helper call #{i + 1}"},
        )
        for i in range(over_count)
    ]
    scripted_steps.append(finish_step())

    agent = ScriptedAgent(scripted_steps)
    loop, store = build_loop(agent, executor=FakeExecutor(), conversation_id=CID)
    # All successful (so the cap is the only reason the over-cap call
    # is refused — the helper would have answered if asked).
    fanout = _StubFanout([{"success": True, "content": f"ok #{i}"} for i in range(over_count + 1)])
    loop._run_fanout = fanout

    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED

    events = await store.get_events(CID)
    # Count the actions + observations + errors for the fan-out tool.
    fanout_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "delegate_explore"
    ]
    fanout_observations = [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result is not None
        and e.tool_result.tool_name == "delegate_explore"
    ]
    fanout_errors = [
        e
        for e in events
        if isinstance(e, AgentErrorEvent)
        and e.tool_call_id is not None
        # Match the proposed action's call_id to the error's tool_call_id
        # (every fan-out action has a unique call_id; the cap-refusal
        # AgentErrorEvent echoes the over-cap action's call_id).
        and e.tool_call_id
        in {
            a.tool_call.call_id
            for a in fanout_actions
            if a.tool_call is not None
        }
    ]
    # _FANOUT_MAX_PER_RUN successful dispatches, +1 cap-refused action
    # whose ObservationEvent is an AgentErrorEvent (not a successful
    # ToolResult). So we have cap+1 actions total.
    assert len(fanout_actions) == _FANOUT_MAX_PER_RUN + 1
    # _FANOUT_MAX_PER_RUN successful observations (one per under-cap
    # dispatch). The over-cap action has NO successful ObservationEvent
    # (it was refused).
    assert len(fanout_observations) == _FANOUT_MAX_PER_RUN
    # The cap-refused action produced exactly one AgentErrorEvent that
    # is paired to it (by call_id) AND names the cap in its error text.
    # (There may be other AgentErrorEvents in the log from unrelated
    # paths; we filter to those paired with a fan-out action.)
    cap_refused = [
        e
        for e in fanout_errors
        if "cap" in (e.error or "") and "delegate_explore" in (e.error or "")
    ]
    assert len(cap_refused) == 1, (
        f"expected exactly 1 cap-refused AgentErrorEvent, got {len(cap_refused)}; "
        f"errors: {[e.error for e in fanout_errors]}"
    )
    refused = cap_refused[0]
    assert f"{_FANOUT_MAX_PER_RUN}" in refused.error
    # And the refused error is paired to the over-cap action (KV-cache
    # pairing) — the call_id on the refused error matches the
    # call_id on the (cap+1)th fan-out action.
    over_cap_action = fanout_actions[_FANOUT_MAX_PER_RUN]
    assert over_cap_action.tool_call is not None
    assert refused.tool_call_id == over_cap_action.tool_call.call_id
    # The seam was called exactly cap times (the over-cap call did NOT
    # dispatch — the cap fires BEFORE the seam runs, so the seam has no
    # always-on cost on refused calls).
    assert len(fanout.calls) == _FANOUT_MAX_PER_RUN
    # The cap counter is at the cap (one past the cap would have been
    # dispatched; the cap fires before the seam, so the count stops
    # at the cap).
    assert loop._fanout_count == _FANOUT_MAX_PER_RUN


# ---------------------------------------------------------------------------
# Test 3 — the cap resets per run segment (a resume/steer gets a fresh budget)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c20_fanout_cap_resets_per_run_segment():
    """The cap is per-run-segment: a fresh ``run()`` resets the counter
    so a new segment gets a fresh budget. Mirrors the verify / DoD /
    plan-revision caps — a steered user message is a clean slate, the
    prior segment's helper round-trips are already visible in the log
    (and any context they informed is still in the View). The reset
    must happen on ``run()`` entry, NOT on construction (otherwise a
    process-restart resume that re-enters an old loop would carry a
    half-burned cap from a prior segment)."""
    # First segment: burn the cap with a single over-cap call.
    agent = ScriptedAgent(
        [
            action_step("delegate_explore", {"question": "burn 1"}),
            action_step("delegate_explore", {"question": "burn 2"}),
            action_step("delegate_explore", {"question": "burn 3"}),
            # Over-cap call: refused, no observation.
            action_step("delegate_explore", {"question": "burn 4 (refused)"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), conversation_id=CID)
    fanout = _StubFanout(
        [{"success": True, "content": f"ok #{i}"} for i in range(_FANOUT_MAX_PER_RUN + 2)]
    )
    loop._run_fanout = fanout
    await loop.send_message("go")
    await loop.run()
    # Cap was reached (3 successful, 1 refused). Counter at the cap.
    assert loop._fanout_count == _FANOUT_MAX_PER_RUN
    assert len(fanout.calls) == _FANOUT_MAX_PER_RUN  # over-cap was NOT dispatched

    # Second segment: a new user message + a fresh run(). The counter
    # must reset so the new segment gets a fresh budget. The user
    # message is the explicit "fresh segment" signal (mirrors
    # `_actions_since_last_resume`).
    await loop.send_message("a follow-up question")
    # The seam was reset between runs (otherwise the script would
    # have walked past its over-allocated 5 entries — sanity).
    assert loop._fanout_count == _FANOUT_MAX_PER_RUN  # not yet reset
    # Now drive a second segment. We use a fresh agent for the second
    # run so the steps are scripted from index 0 again.
    loop.agent = ScriptedAgent(
        [
            # This call should be dispatched: the cap was reset on
            # run() entry (the per-segment reset is the contract).
            action_step("delegate_explore", {"question": "fresh-segment call"}),
            finish_step(),
        ]
    )
    # Note: the same loop, same store, same conversation_id — a real
    # resume, not a new build.
    await loop.run()
    # The fresh-segment call was dispatched (counter incremented to 1,
    # NOT refused).
    assert loop._fanout_count == 1, (
        f"cap did NOT reset on run() entry: counter is "
        f"{loop._fanout_count} (expected 1 for the fresh segment)"
    )
    # And the seam was called for the fresh-segment call (the
    # over-cap-then-reset sequence did NOT lose the call).
    assert len(fanout.calls) == _FANOUT_MAX_PER_RUN + 1
    fresh_call_args, _ = fanout.calls[_FANOUT_MAX_PER_RUN]
    assert fresh_call_args["question"] == "fresh-segment call"


# ---------------------------------------------------------------------------
# Test 4 — input is length-bounded (no unbounded helper-prompt growth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c20_fanout_input_is_length_bounded():
    """The helper's input (question + context) is length-bounded to
    ``_FANOUT_INPUT_MAX_CHARS`` per field. A driver that passes a
    huge question or context sees it TRUNCATED in the seam's args (not
    silently forwarded — the seam's args are what would feed the
    production LLM call, so truncating at the seam keeps the helper's
    prompt bounded). The truncation is symmetric across question and
    context."""
    huge = "x" * (_FANOUT_INPUT_MAX_CHARS + 500)  # 500 chars over the cap
    agent = ScriptedAgent(
        [
            action_step(
                "delegate_explore",
                {"question": huge, "context": huge},
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), conversation_id=CID)
    fanout = _StubFanout([{"success": True, "content": "ok"}])
    loop._run_fanout = fanout

    await loop.send_message("go")
    await loop.run()

    # The seam's args were truncated (each field, not the join).
    assert len(fanout.calls) == 1
    args, _ = fanout.calls[0]
    assert len(args["question"]) <= _FANOUT_INPUT_MAX_CHARS
    assert len(args["context"]) <= _FANOUT_INPUT_MAX_CHARS
    # The truncation is visible (a marker, not a silent chop) so the
    # helper can detect it and act accordingly.
    assert "\u2026[truncated]" in args["question"]
    assert "\u2026[truncated]" in args["context"]


# ---------------------------------------------------------------------------
# Test 5 — the fan-out is non-blocking (driver continues after; one
#          action in / one observation out per call, like notify_user)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c20_fanout_one_action_in_one_observation_out_per_call():
    """The fan-out is non-blocking: a single ``delegate_explore`` call
    produces exactly ONE ``ActionEvent`` and ONE ``ObservationEvent``
    (paired, no extras). This is the execute-and-observe contract
    (§4.1) — the same shape as a normal tool call. A degenerate fan-
    out (e.g. the helper returns a degenerate report) is still
    observed; the actionless valve applies on the NEXT step if the
    driver takes no real action, not on the fan-out step itself."""
    agent = ScriptedAgent(
        [
            action_step("delegate_explore", {"question": "q1"}),
            action_step("delegate_explore", {"question": "q2"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=FakeExecutor(), conversation_id=CID)
    fanout = _StubFanout(
        [
            {"success": True, "content": "answer-1"},
            {"success": True, "content": "answer-2"},
        ]
    )
    loop._run_fanout = fanout
    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    fanout_actions = [
        e
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == "delegate_explore"
    ]
    fanout_observations = [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result is not None
        and e.tool_result.tool_name == "delegate_explore"
    ]
    assert len(fanout_actions) == 2
    assert len(fanout_observations) == 2
    # Each action is paired with exactly one observation (no dangling
    # actions, no double-observations — the execute-and-observe §4.1
    # invariant holds for the fan-out path the same way it holds for
    # a real tool call).
    observed_ids = {o.action_id for o in fanout_observations}
    assert observed_ids == {a.id for a in fanout_actions}
