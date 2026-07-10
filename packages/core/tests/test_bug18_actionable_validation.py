"""Bug 18 — tool-arg schema-validation errors are ACTIONABLE at the loop seam.

Live MiniMax-M3 finding (build_soak revise_after_finish): the model repeatedly
called `update_plan_progress({"steps": ["", "", ""]})` (empty strings where each
step must be an object). The bare pydantic message never showed the EXPECTED
nested shape/example, so the model couldn't self-correct and looped to STUCK.

The primary fix lives in the tool executor's validation-error formatter
(`describe_validation_failure`), which is exercised end-to-end here through the
REAL `DefaultToolExecutor` wired into the agent loop:

1. (recovery) the FIRST `AgentErrorEvent.to_llm_message()` the model sees carries
   the expected nested shape + enum + a concrete example, and the corrected call
   then produces a SUCCESS `ObservationEvent` — proving the actionable error makes
   a formatting failure recoverable in one turn.
2. (must-not-regress) the SAME malformed call repeated past the stuck threshold
   still trips the breaker (escape-then-halt → user-facing blocked question);
   the richer message is deterministic, so stuck detection is unaffected.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    NoOpCondenser,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
)
from disco.core.llm import ModelExecutionPolicy, OperatingMode
from disco.core.loop import NeverConfirm, StuckThresholds
from disco.core.loop.engine import AgentLoop
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from loop_fakes import (
    FakeAnalyzer,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
)

CID = "conv"

_STANDARD = ModelExecutionPolicy.standard()


def _real_executor() -> DefaultToolExecutor:
    """A real executor over the default tool registry (update_plan_progress is an
    in_process, read_only tool — no sandbox needed)."""
    return DefaultToolExecutor(
        build_default_registry(), agent_scope(model_policy=_STANDARD)
    )


def _make_loop(executor) -> tuple[AgentLoop, SqliteEventStore]:
    store = SqliteEventStore(":memory:")
    loop = AgentLoop(
        CID,
        store,
        ScriptedAgent([]),  # not stepped in the _drive tests
        executor,
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.LONG_HORIZON,
        model_policy=_STANDARD,
    )
    return loop, store


def _upp(call_id: str, steps) -> ActionEvent:
    return ActionEvent(
        thought="update progress",
        tool_call=ToolCall(
            tool_name="update_plan_progress", call_id=call_id, arguments={"steps": steps}
        ),
    )


async def _drive(loop: AgentLoop, action: ActionEvent) -> list:
    persisted = await loop.store.append(CID, action)
    await loop._execute_and_observe(persisted)
    return await loop.store.get_events(CID)


async def test_first_error_is_actionable_and_corrected_call_succeeds():
    """The malformed call's AgentErrorEvent — the only thing the model sees — must
    carry the nested shape + enum + example via to_llm_message(); the corrected
    call then produces a successful ObservationEvent (real recovery)."""
    loop, _ = _make_loop(_real_executor())

    # 1. malformed: empty strings where each step must be an object.
    events = await _drive(loop, _upp("bad1", ["", "", ""]))
    err = next(
        e for e in events if isinstance(e, AgentErrorEvent) and e.tool_call_id == "bad1"
    )
    seen = err.to_llm_message().content  # the EXACT bytes the model receives
    assert "steps.0" in seen
    assert "list of objects" in seen
    assert "index" in seen and "state" in seen
    assert "pending" in seen and "active" in seen and "done" in seen
    assert '{"index": 1, "state": "pending"}' in seen

    # 2. corrected: the shape the error pointed at validates + runs.
    events = await _drive(loop, _upp("good1", [{"index": 1, "state": "done"}]))
    obs = next(
        e
        for e in events
        if isinstance(e, ObservationEvent) and e.tool_result.call_id == "good1"
    )
    assert obs.tool_result.success is True
    # and no error was emitted for the corrected call.
    assert not any(
        isinstance(e, AgentErrorEvent) and e.tool_call_id == "good1" for e in events
    )


async def test_ignored_actionable_error_still_stucks():
    """MUST-NOT-REGRESS — if the model IGNORES the actionable error and keeps
    sending the identical malformed call (and does NOTHING else productive), the run
    still halts with an explained question (no infinite loop). NOTE:
    update_plan_progress is a NONCRITICAL
    cosmetic bookkeeping tool, so its malformed loop is intentionally EXEMPT from the
    fatal `repeated_action_error` pattern (signals._NONCRITICAL_FAILURE_TOOLS, mirrored
    into stuck.py) — a cosmetic hiccup must not kill an otherwise-productive build.
    Pure plan-tracker spam with no real work instead halts via the dedicated
    bookkeeping gate (detail="bookkeeping_only"), which IS the intended backstop."""
    # Real executor so the genuine (now-actionable) invalid_arguments fires each turn.
    agent = ScriptedAgent(
        [action_step("update_plan_progress", {"steps": ["", "", ""]})] * 8
        + [finish_step()]
    )
    loop, store = build_loop(
        agent,
        executor=_real_executor(),
        stuck_thresholds=StuckThresholds(repeat_action_error=3),
    )
    await loop.send_message("update the plan progress")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION

    events = await store.get_events(CID)
    # the cosmetic-tool loop halts via the BOOKKEEPING gate (not the fatal
    # repeated_action_error escape) — update_plan_progress is exempt from pattern 2.
    assert_blocked_question_landing(events, legacy_detail="bookkeeping_only")
    # and the malformed calls were genuinely rejected (never coerced into success).
    upp_obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "update_plan_progress"
    ]
    assert all(o.tool_result.success is False for o in upp_obs)
