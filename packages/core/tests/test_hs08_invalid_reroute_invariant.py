"""HS-08 — Reroute-to-hidden-`invalid`-tool repair: INVARIANT PIN.

The engine's Rung-7 "Invalid-tool reroute" turns a valid-JSON-but-unknown
tool call into a hint + retry within the requery bound (cap=2). This test
pins the message invariant on the EXHAUSTED path:

    every assistant tool_call gets a matching tool_result
    (or, equivalently for the provider's pairing rule, an AgentErrorEvent
    carrying `tool_call_id`),
    and the turn is NEVER aborted with an orphaned tool_call.

The invariant is what the OpenAI provider enforces: a request that submits
an assistant tool_call with no matching tool message (per `tool_call_id`)
gets a 400. F1 (provider recovery) cannot fix a missing tool_result on the
outgoing wire — it operates on the incoming empty-structured response — so
the loop's defense has to be local and unconditional.

The test is a PIN: if a future change to the Rung-7 hint/retry/requery-bound
logic accidentally re-routes the unknown call away from `_execute_and_observe`
(bypassing the executor's `unknown_tool` failure), the loop would emit an
ActionEvent with no paired observation/error and the next provider call
would 400. This test fails in that exact scenario.

Verdict (engine.py):
  - hint-retry exits: requery hint appended, continue, NEVER aborts the turn.
  - name-gated exits: requery is skipped (gates_by_name returns True), the
    step falls through to the action path — the executor returns a failed
    ToolResult, the engine emits AgentErrorEvent with tool_call_id, invariant
    preserved (engine.py:3311-3321 / :3335-3341).
  - requery-exhausted exits: same as name-gated — break out of the inner
    while loop, the step falls through, _execute_and_observe emits the
    paired result.
  - In ALL three exit paths the engine NEVER leaves an ActionEvent without
    a paired tool message, regardless of what the executor does (the
    except-Exception arm at engine.py:3310 catches any misbehavior).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest
from disco.core import ToolResult
from disco.core.llm import ToolSpec
from disco.core.events import (
    ActionEvent,
    AgentErrorEvent,
    ObservationEvent,
)
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step


CID = "conv"


class _UnknownFailingExecutor:
    """Mirrors the production DefaultToolExecutor's contract for unknown
    tools: returns a structured, model-readable FAILURE for any name that
    is not in the offered/registered set, NEVER raises. This is the
    production shape — see packages/tools/src/disco/tools/executor.py
    `_fail(call, "unknown_tool", ...)`.

    The loop relies on this contract to preserve the message invariant on
    the Rung-7 exhausted path: `_execute_and_observe` catches any raise
    defensively, but the natural exit is a returned failure result that
    the engine then emits as a paired AgentErrorEvent.
    """

    def __init__(self, *, known: Iterable[str]):
        self._known = frozenset(known)
        self.calls: list = []

    def available_tools(self):
        return [
            ToolSpec(name=n, description=f"known {n}", parameters_schema={}) for n in self._known
        ]

    def callable_tool_names(self):
        # Rung-7's name-set uses this when present; for unknown tools it's
        # the same as available_tools() — both omit the hallucinated name.
        return self._known

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name in self._known:
            return ToolResult(
                call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
            )
        # Production-shaped failure for unknown tools.
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content=f"unknown or out-of-scope tool {call.tool_name!r}",
            error=f"unknown or out-of-scope tool {call.tool_name!r}",
        )


class _RaisingExecutor:
    """The pathological case: the executor's `execute()` raises (e.g. a
    network error to the sandbox, a bug in DefaultToolExecutor). The
    engine's `_execute_and_observe` MUST catch it and emit a paired
    AgentErrorEvent anyway — otherwise an executor bug would orphan the
    tool_call and break the next provider call.
    """

    def __init__(self, *, known: Iterable[str]):
        self._known = frozenset(known)
        self.calls: list = []

    def available_tools(self):
        return [ToolSpec(name=n, description="x", parameters_schema={}) for n in self._known]

    def callable_tool_names(self):
        return self._known

    async def execute(self, call):
        self.calls.append(call)
        if call.tool_name in self._known:
            return ToolResult(
                call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
            )
        # Simulate a misbehaving executor: raises instead of returning
        # a structured failure. The engine must still preserve the invariant.
        raise RuntimeError(f"sandbox blew up on {call.tool_name!r}")


def _paired_ids(events: list) -> dict[str, str]:
    """Map tool_call_id -> the kind of paired result emitted.

    ObservationEvent uses tool_result.call_id; AgentErrorEvent uses
    tool_call_id. Both serve the same purpose for the provider pairing
    rule. A missing entry in this map for an ActionEvent's call_id is
    the exact orphan this test pins against.
    """
    paired: dict[str, str] = {}
    for e in events:
        if isinstance(e, ObservationEvent):
            paired[e.tool_result.call_id] = "observation"
        elif isinstance(e, AgentErrorEvent) and e.tool_call_id:
            paired[e.tool_call_id] = "agent_error"
    return paired


def _unknown_actions(events: list, name: str) -> list[ActionEvent]:
    return [
        e for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name == name
    ]


# ---------------------------------------------------------------------------
# (1) Exhausted path with a well-behaved executor (production shape):
#     the executor returns success=False for the unknown tool, the
#     engine emits a paired AgentErrorEvent. Pin the invariant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requery_exhausted_unknown_tool_emits_paired_result_no_abort():
    """The Rung-7 / requery-exhausted path must always pair the unknown
    ActionEvent's tool_call with a result (Observation OR AgentError) —
    and run() must NOT raise. This is the load-bearing invariant for
    the provider's wire pairing (F1 cannot fix a missing tool_result).

    The ScriptedAgent repeats the unknown tool step until the engine
    burns through its requery bound (cap=2), at which point the unknown
    call falls through to the normal action path. With max_iterations=2
    the loop terminates via the cap AFTER emitting the paired
    AgentErrorEvent — the cap's own ErrorEvent is unrelated to the
    unknown tool and is NOT a violation of the invariant.
    """
    unknown = "totally_made_up_tool_xyz"
    executor = _UnknownFailingExecutor(known={"shell", "file_read"})
    # Single scripted step; ScriptedAgent repeats it on exhaustion.
    agent = ScriptedAgent([action_step(unknown)])
    loop, store = build_loop(agent, executor=executor, max_iterations=2)

    await loop.send_message("go")

    # (1) run() must NOT raise / hang. ERROR status from max_iterations
    #     is fine — the test is about the unknown tool's paired result,
    #     not the iteration cap's terminal.
    try:
        await asyncio.wait_for(loop.run(), timeout=5.0)
    except TimeoutError:
        pytest.fail(
            "AgentLoop.run() hung on the Rung-7 exhausted path. The turn "
            "should have terminated cleanly with a paired tool_result."
        )

    events = await store.get_events(CID)
    unknown_actions = _unknown_actions(events, unknown)

    # The unknown action WAS emitted (the engine's normal action path
    # runs after the requery bound is hit).
    assert len(unknown_actions) >= 1, (
        f"Expected at least one ActionEvent for the unknown tool {unknown!r}; "
        f"event kinds: {[type(e).__name__ for e in events]!r}"
    )

    # (2) The invariant: EVERY unknown ActionEvent's tool_call.call_id
    #     is paired with an AgentErrorEvent (or ObservationEvent). The
    #     production DefaultToolExecutor returns success=False for
    #     unknown tools, so the natural pairing is an AgentErrorEvent.
    paired = _paired_ids(events)
    for action in unknown_actions:
        call_id = action.tool_call.call_id
        assert call_id in paired, (
            f"Orphaned tool_call detected: {unknown!r} (call_id={call_id!r}) "
            f"has no matching ObservationEvent.tool_result.call_id OR "
            f"AgentErrorEvent.tool_call_id. The Rung-7 exhausted path "
            f"violated the message invariant — the next provider call "
            f"will 400 with a 'messages with role \"tool\" must be a "
            f"response to a preceeding tool_call' error. "
            f"Paired ids so far: {paired!r}"
        )

    # Pin the natural production shape: a paired AgentErrorEvent.
    # (Accept ObservationEvent too so this test survives a refactor
    # that returns success=False via an ObservationEvent.)
    for action in unknown_actions:
        call_id = action.tool_call.call_id
        assert paired[call_id] in ("agent_error", "observation"), (
            f"Unexpected pair kind {paired[call_id]!r} for {unknown!r}"
        )

    # The natural shape: AgentErrorEvent with the offending tool name
    # so the model can self-correct on its next turn.
    err = next(
        (
            e for e in events
            if isinstance(e, AgentErrorEvent) and e.tool_call_id in {
                a.tool_call.call_id for a in unknown_actions
            }
        ),
        None,
    )
    assert err is not None, (
        f"Expected an AgentErrorEvent for the unknown tool {unknown!r}; "
        f"DefaultToolExecutor returns success=False for unknown tools, "
        f"which the engine emits as an AgentErrorEvent."
    )
    assert unknown in err.error, (
        f"AgentErrorEvent.error should mention the unknown tool name; "
        f"got {err.error!r}"
    )
    # F1 (c) defense: the AgentErrorEvent MUST carry the tool_call_id
    # so the OpenAI provider's _message() renders it as role:"tool"
    # (a user-role downgrade would still keep the invariant but would
    # make the model less likely to read the error as paired).
    assert err.tool_call_id is not None
    # The action_id correlates the error back to the ActionEvent —
    # the event log is auditable.
    assert err.action_id in {a.id for a in unknown_actions}

    # Requery bookkeeping: the engine fired ≥2 requery bounces before
    # breaking. (1 initial + 2 requery = 3 agent.step() calls for one
    # outer iteration that processes the unknown tool, plus possibly
    # one more for the cap-hit iteration.)
    assert agent.calls >= 3, (
        f"Expected ≥3 agent calls (initial + 2 requery bounces) to prove "
        f"the bound was actually exercised; got {agent.calls}. The test "
        f"would pass on a 'no-requery' bug."
    )


# ---------------------------------------------------------------------------
# (2) Exhausted path ALSO holds when the executor RAISES (the engine's
#     defensive `except Exception` arm at engine.py:3310). The invariant
#     must not depend on the executor's good behavior.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requery_exhausted_with_raising_executor_still_pairs():
    """Even if the executor RAISES (the path the engine guards against
    defensively), the Rung-7 exhausted path must still produce a paired
    AgentErrorEvent for the unknown tool_call. The invariant cannot
    depend on executor cooperation.
    """
    unknown = "broken_unknown_tool"
    executor = _RaisingExecutor(known={"shell"})
    agent = ScriptedAgent([action_step(unknown)])
    loop, store = build_loop(agent, executor=executor, max_iterations=2)

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    events = await store.get_events(CID)
    paired = _paired_ids(events)
    unknown_actions = _unknown_actions(events, unknown)
    assert len(unknown_actions) >= 1, (
        f"Expected at least one ActionEvent for the unknown tool {unknown!r}"
    )
    for action in unknown_actions:
        call_id = action.tool_call.call_id
        assert call_id in paired, (
            f"Raising executor orphaned tool_call {call_id!r}: the engine's "
            f"`except Exception` arm failed to emit a paired AgentErrorEvent. "
            f"Engine line: packages/core/src/disco/core/loop/engine.py:3310-3321. "
            f"Paired ids so far: {paired!r}"
        )
        # The error from the executor is what surfaces.
        err = next(
            e for e in events
            if isinstance(e, AgentErrorEvent) and e.tool_call_id == call_id
        )
        assert "sandbox blew up" in err.error, (
            f"AgentErrorEvent should carry the executor's exception text; "
            f"got {err.error!r}"
        )


# ---------------------------------------------------------------------------
# (3) Exhausted path AND a clean finish: after the paired error, the
#     ScriptedAgent calls `finish` — the loop terminates as FINISHED
#     with a non-fatal status. Proves the run is not "aborted" — the
#     conversation continues normally after the invariant-paired error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requery_exhausted_then_finish_yields_finished_status():
    """A model that hallucinates an unknown tool, sees the paired error,
    then re-aligns and calls `finish` — the loop reaches FINISHED. This
    proves the turn was NOT aborted by the Rung-7 exhausted path; the
    error is model-recoverable, not conversation-fatal.
    """
    from disco.core import ConversationStatus

    unknown = "phantom_tool"
    executor = _UnknownFailingExecutor(known={"shell"})
    # Three steps: hallucinate, hallucinate again, hallucinate once more
    # (so the requery bound caps + 1 fall-through to the action path),
    # THEN finish. The script's last entry sticks for any further calls.
    agent = ScriptedAgent([
        action_step(unknown),
        action_step(unknown),
        action_step(unknown),
        finish_step("recovered"),
    ])
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("go")
    state = await asyncio.wait_for(loop.run(), timeout=5.0)

    # Status is FINISHED — the unknown tool did NOT abort the turn.
    assert state.execution_status == ConversationStatus.FINISHED, (
        f"Expected FINISHED after the unknown tool was paired and finish "
        f"was called; got {state.execution_status!r}. The Rung-7 exhausted "
        f"path must be model-recoverable, not conversation-fatal."
    )

    events = await store.get_events(CID)
    unknown_actions = _unknown_actions(events, unknown)
    assert len(unknown_actions) >= 1, (
        f"Expected the unknown tool's ActionEvent before finish; "
        f"event kinds: {[type(e).__name__ for e in events]!r}"
    )

    # The paired AgentErrorEvent is still there — the run terminated
    # cleanly with the invariant satisfied.
    paired = _paired_ids(events)
    for action in unknown_actions:
        assert action.tool_call.call_id in paired, (
            f"Orphaned tool_call on the finish path: {action.tool_call.call_id!r}"
        )


# ---------------------------------------------------------------------------
# (4) Exhausted path with the BOUND actually hit: the requery hint user
#     message should have been appended (so the proof file:line matches
#     the verdict). This nails the "requery fired" pre-condition for the
#     invariant test — without the hint we can't be sure the bound ran.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requery_exhausted_proof_requery_actually_fired():
    """Pinning the pre-condition: the requery hint ('Unknown tool ...')
    must be visible in the views the agent saw, proving the Rung-7
    bound was actually exercised. Without this, an unrelated code path
    (e.g. actionless valve) could have produced a paired result and the
    invariant test would pass for the wrong reason.
    """
    unknown = "definitely_not_a_real_tool"
    executor = _UnknownFailingExecutor(known={"shell"})
    agent = ScriptedAgent([action_step(unknown)])
    loop, _ = build_loop(agent, executor=executor, max_iterations=2)

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    # The ScriptedAgent captured the views the loop sent it. The
    # requery hint is appended as a transient user message BEFORE the
    # requery call. Inspect the views to confirm the hint fired.
    hint_views = [
        v for v in agent.seen_views
        if any(
            m.role == "user" and "Unknown tool" in (m.content or "")
            for m in v.messages
        )
    ]
    assert hint_views, (
        f"Expected at least one agent.step() view to carry the Rung-7 "
        f"'Unknown tool' hint; saw {len(agent.seen_views)} view(s). The "
        f"requery bound (cap=2) must have been exercised for this test "
        f"to be a meaningful pin of the exhausted path. Agent calls: {agent.calls}."
    )
    # The hint references the offending name (so the model can self-correct).
    hint = next(
        m.content for v in hint_views
        for m in v.messages
        if m.role == "user" and "Unknown tool" in (m.content or "")
    )
    assert unknown in hint, (
        f"Rung-7 hint should name the offending tool; got {hint!r}"
    )
