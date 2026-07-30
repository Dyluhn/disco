"""Engine-rekick fix (PART 1) — `signals.has_unprocessed_user_message` must NOT
count a PURE terminal/status marker as the progress that "consumes" a user turn.

Root cause this guards: the engine `AgentLoop._run` appends the terminal marker
(FINISHED/STUCK/ERROR) at run end. A follow-up appended while the run finalizes
lands just BEFORE that marker. The old predicate counted RUNNING/FINISHED/STUCK/
ERROR StatusEvents as "progress", so the marker masked the follow-up as already
processed forever — the run() re-entry + the runtime re-kick both no-op'd and the
turn was stranded. With status markers excluded, only REAL work (tool calls /
observations / plan events / assistant messages) consumes a turn, so a follow-up
after a terminal marker (no real work since) is correctly UNPROCESSED.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    ConversationState,
    ConversationStatus,
    ErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    WorkspaceMutationEvent,
    current_workspace_agent_view_id,
    pending_workspace_run_intent,
    workspace_run_intent_admission_required,
    workspace_terminal_matches_current_run,
)
from disco.core.events import PlanStep, agent_view_consistent_events
from disco.core.llm import OperatingMode
from disco.core.loop import signals
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step


def _seq(events: list[Event]) -> list[Event]:
    """Assign monotonically increasing seqs (the store does this on append)."""
    return [e.model_copy(update={"seq": i + 1}) for i, e in enumerate(events)]


def _user(text: str = "do the thing") -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))


def _agent(text: str = "working on it") -> MessageEvent:
    return MessageEvent(
        source=EventSource.AGENT, message=LLMMessage(role="assistant", content=text)
    )


def _action(tool: str = "file_write") -> ActionEvent:
    return ActionEvent(
        thought="acting",
        tool_call=ToolCall(tool_name=tool, arguments={"path": "x", "content": "y"}),
    )


def _obs(tool: str = "file_write") -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(call_id="c1", tool_name=tool, success=True, content="ok"),
        action_id="a1",
    )


def _plan() -> PlanEvent:
    return PlanEvent(summary="plan", steps=[PlanStep(title="step 1")], revision=1)


def _status(status: ConversationStatus, detail: str | None = None) -> StatusEvent:
    return StatusEvent(status=status, detail=detail)


def _reminder() -> MessageEvent:
    # A synthetic system-reminder injection — role="user" in the LLM view but
    # EventSource.ENVIRONMENT, so it is NOT a real user turn.
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content="<system-reminder>\n[F9 dedup: file_read(x)] …\n</system-reminder>",
        ),
    )


class _AttributedExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.attributed_view_ids: list[str | None] = []

    async def execute_attributed(self, call, agent_view_id, on_result=None, prepare=None):
        if prepare is not None:
            await prepare()
        self.attributed_view_ids.append(agent_view_id)
        result = await self.execute(call)
        if on_result is not None:
            await on_result(result)
        return result


async def test_run_executes_the_exact_view_attributed_action_returned_by_emit() -> None:
    store = SqliteEventStore(":memory:")
    await store.append("attributed", _user())
    await store.append(
        "attributed",
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-message",
            run_protocol_version=1,
        ),
    )
    executor = _AttributedExecutor()
    loop, _ = build_loop(
        ScriptedAgent([action_step()]),
        store=store,
        executor=executor,
        conversation_id="attributed",
        mode=OperatingMode.LONG_HORIZON,
        max_iterations=1,
    )
    await loop.run()
    actions = [
        event for event in await store.get_events("attributed") if isinstance(event, ActionEvent)
    ]
    assert len(actions) == 1
    assert actions[0].agent_view_id is not None
    assert executor.attributed_view_ids == [actions[0].agent_view_id]


# --- un-masking: terminal marker after the user turn does NOT consume it -------


def test_followup_after_finished_is_unprocessed():
    events = _seq(
        [_action(), _obs(), _user("now also add a footer"), _status(ConversationStatus.FINISHED)]
    )
    assert signals.has_unprocessed_user_message(events) is True


def test_followup_after_stuck_is_unprocessed():
    events = _seq([_action(), _user("change the title"), _status(ConversationStatus.STUCK)])
    assert signals.has_unprocessed_user_message(events) is True


def test_followup_after_error_is_unprocessed():
    events = _seq([_action(), _user("try a different approach"), _status(ConversationStatus.ERROR)])
    assert signals.has_unprocessed_user_message(events) is True


def test_followup_after_running_marker_is_unprocessed():
    # The engine emits RUNNING at entry; that pure status marker must not mask a
    # follow-up either.
    events = _seq([_action(), _user("and fix the colors"), _status(ConversationStatus.RUNNING)])
    assert signals.has_unprocessed_user_message(events) is True


# --- negative: a turn that WAS processed (real work since) is NOT unprocessed --


def test_processed_turn_is_not_unprocessed():
    events = _seq([_user("do it"), _action(), _obs(), _status(ConversationStatus.FINISHED)])
    assert signals.has_unprocessed_user_message(events) is False


def test_agent_reply_after_user_is_not_unprocessed():
    events = _seq(
        [_user("question?"), _agent("here is the answer"), _status(ConversationStatus.FINISHED)]
    )
    assert signals.has_unprocessed_user_message(events) is False


def test_plan_event_after_user_is_not_unprocessed():
    # A plan emitted in response to the user counts as real work.
    events = _seq([_user("build a site"), _plan(), _status(ConversationStatus.RUNNING)])
    assert signals.has_unprocessed_user_message(events) is False


def test_strict_old_run_output_cannot_consume_new_intent_before_view_admission():
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
        id="intent_1",
    )
    events = _seq(
        [
            _user("change the design"),
            intent,
            _agent("late output from the previous model request"),
        ]
    )
    assert workspace_run_intent_admission_required(events) is True
    assert pending_workspace_run_intent(events) is not None
    assert signals.has_unprocessed_user_message(events) is True

    events = _seq(
        [
            *events,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_1",
                agent_view_id="aview_1",
                run_protocol_version=1,
            ),
        ]
    )
    assert workspace_run_intent_admission_required(events) is False
    assert pending_workspace_run_intent(events) is not None
    assert signals.has_unprocessed_user_message(events) is True

    events = _seq(
        [
            *events,
            _agent("response from the fresh admitted view").model_copy(
                update={"agent_view_id": "aview_1"}
            ),
        ]
    )
    assert pending_workspace_run_intent(events) is None
    assert signals.has_unprocessed_user_message(events) is False


async def test_materialized_model_view_durably_admits_latest_run_intent_once():
    cid = "conv-model-view-admission"
    store = SqliteEventStore(":memory:")
    store.create_conversation(cid)
    events = await store.append_many(
        cid,
        [
            _user("change the design"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
        ],
    )
    intent = next(
        e
        for e in events
        if isinstance(e, WorkspaceMutationEvent) and e.operation == "agent.run-intent.user-turn"
    )
    loop, _ = build_loop(
        ScriptedAgent([finish_step()]),
        store=store,
        conversation_id=cid,
    )

    await loop._materialize_current_view()
    admitted = await store.get_events(cid)
    views = [
        event
        for event in admitted
        if isinstance(event, WorkspaceMutationEvent) and event.operation == "agent.view-admitted"
    ]
    assert len(views) == 1
    assert views[0].run_intent_id == intent.id
    assert views[0].agent_view_id is not None
    assert views[0].run_protocol_version == 1

    await loop._materialize_current_view()
    # Each _materialize_view call creates a fresh view generation in strict
    # protocol, so there will be two view-admitted events (one per call).
    total_views = await store.get_events(cid)
    view_admissions = [
        event
        for event in total_views
        if isinstance(event, WorkspaceMutationEvent) and event.operation == "agent.view-admitted"
    ]
    assert len(view_admissions) == 2
    # Each admission must reference the same run intent but have a distinct
    # agent_view_id.
    assert view_admissions[0].run_intent_id == intent.id
    assert view_admissions[1].run_intent_id == intent.id
    assert view_admissions[0].agent_view_id != view_admissions[1].agent_view_id


# --- synthetic no-op turns must NOT count as processing the user turn ----------


def test_system_reminder_after_user_does_not_process_the_turn():
    # A system-reminder / F9 injection lands after the follow-up but is NOT real
    # work — the user turn stays UNPROCESSED.
    events = _seq(
        [
            _action(),
            _obs(),
            _user("also add dark mode"),
            _reminder(),
            _status(ConversationStatus.FINISHED),
        ]
    )
    assert signals.has_unprocessed_user_message(events) is True


def test_no_user_message_is_not_unprocessed():
    events = _seq([_action(), _obs(), _status(ConversationStatus.FINISHED)])
    assert signals.has_unprocessed_user_message(events) is False


# --- latest_unprocessed_user_text stays consistent with the fixed basis -------


def test_latest_unprocessed_user_text_reads_the_stranded_followup():
    events = _seq(
        [_action(), _obs(), _user("add a footer please"), _status(ConversationStatus.FINISHED)]
    )
    assert signals.latest_unprocessed_user_text(events) == "add a footer please"


def test_latest_unprocessed_user_text_none_when_processed():
    events = _seq([_user("do it"), _action(), _obs(), _status(ConversationStatus.FINISHED)])
    assert signals.latest_unprocessed_user_text(events) is None


def test_same_intent_view_rollover_does_not_resurrect_ingress_as_followup() -> None:
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.workflow-schedule",
        run_protocol_version=1,
        id="intent_schedule",
    )
    first_view = WorkspaceMutationEvent(
        operation="agent.view-admitted",
        run_intent_id=intent.id,
        agent_view_id="view-1",
        run_protocol_version=1,
    )
    events = _seq(
        [
            _user("build the scheduled report"),
            intent,
            WorkspaceMutationEvent(operation="agent.run-claimed"),
            _status(ConversationStatus.RUNNING),
            first_view,
            _plan(),
            _status(ConversationStatus.RUNNING, "plan_approved"),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-2",
                run_protocol_version=1,
            ),
        ]
    )

    # The second view still owes progress, so the run remains executable, but
    # its already-owned launch instruction is not a new mid-step steer.
    assert signals.has_unprocessed_user_message(events) is True
    assert signals.latest_unprocessed_user_text(events) is None

    followup = _seq([*events, _user("also change the chart colors")])
    assert signals.latest_unprocessed_user_text(followup) == "also change the chart colors"


# --- strict v1 regression: old-view tail after newer admission remains pending -


def test_old_view_tail_after_newer_admission_remains_pending():
    """Agent output tagged with the OLD view id after a newer view was admitted
    must NOT count as progress — the pending intent stays unresolved."""
    events = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_x",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_x",
                agent_view_id="aview_v1",
                run_protocol_version=1,
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_x",
                agent_view_id="aview_v2",
                run_protocol_version=1,
            ),
            _agent("late output from old view").model_copy(update={"agent_view_id": "aview_v1"}),
        ]
    )
    assert pending_workspace_run_intent(events) is not None
    assert signals.has_unprocessed_user_message(events) is True


# --- strict v1 regression: two admissions, latest wins -------------------------


def test_two_admissions_latest_wins():
    """When two view-admission events race for the same intent, the later one
    defines the active view. Progress from the first is ignored."""
    events = _seq(
        [
            _user("change it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_y",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_y",
                agent_view_id="aview_a",
                run_protocol_version=1,
            ),
            _agent("output from view a").model_copy(update={"agent_view_id": "aview_a"}),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_y",
                agent_view_id="aview_b",
                run_protocol_version=1,
            ),
            _agent("output from view b").model_copy(update={"agent_view_id": "aview_b"}),
        ]
    )
    assert pending_workspace_run_intent(events) is None
    assert signals.has_unprocessed_user_message(events) is False


# --- strict v1 regression: matching current view progress clears pending --------


def test_matching_current_view_progress_clears_pending():
    """Agent progress carrying the exact active agent_view_id must make the
    pending intent resolved."""
    events = _seq(
        [
            _user("do it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_z",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_z",
                agent_view_id="aview_z",
                run_protocol_version=1,
            ),
            _action("file_write").model_copy(update={"agent_view_id": "aview_z"}),
            _obs("file_write").model_copy(update={"agent_view_id": "aview_z"}),
        ]
    )
    assert pending_workspace_run_intent(events) is None
    assert signals.has_unprocessed_user_message(events) is False


# --- workspace_terminal_matches_current_run strict v1 regressions ---------------


def test_current_view_finished_can_seal():
    """A FINISHED StatusEvent that carries the exact active agent_view_id
    and follows real view progress is accepted as the terminal for the run."""
    before = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_s",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_s",
                agent_view_id="aview_s",
                run_protocol_version=1,
            ),
            _action("file_write").model_copy(update={"agent_view_id": "aview_s"}),
            _obs("file_write").model_copy(update={"agent_view_id": "aview_s"}),
        ]
    )
    terminal = StatusEvent(
        status=ConversationStatus.FINISHED,
        agent_view_id="aview_s",
    )
    assert workspace_terminal_matches_current_run(before, terminal) is True


def test_stale_agent_view_finished_cannot_resolve():
    """A FINISHED whose agent_view_id differs from the active admitted view is
    rejected — it belongs to a stale/unseated generation."""
    before = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_t",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_t",
                agent_view_id="aview_t",
                run_protocol_version=1,
            ),
            _action("file_write").model_copy(update={"agent_view_id": "aview_t"}),
        ]
    )
    terminal = StatusEvent(
        status=ConversationStatus.FINISHED,
        agent_view_id="aview_other",
    )
    assert workspace_terminal_matches_current_run(before, terminal) is False


def test_untagged_finished_cannot_resolve_strict():
    """A FINISHED with no agent_view_id and no host_mutation_id cannot resolve
    a strict-v1 run — it is an old/unattributed terminal."""
    before = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_u",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_u",
                agent_view_id="aview_u",
                run_protocol_version=1,
            ),
            _action("file_write").model_copy(update={"agent_view_id": "aview_u"}),
        ]
    )
    terminal = StatusEvent(status=ConversationStatus.FINISHED)
    assert terminal.agent_view_id is None
    assert terminal.host_mutation_id is None
    assert workspace_terminal_matches_current_run(before, terminal) is False


# --- legacy untyped sequence behavior still works ------------------------------


def test_legacy_untyped_sequence_behavior_remains():
    """Old events without run_protocol_version must retain the legacy
    sequence-only reducer behavior with agent.run-admitted."""
    events = _seq(
        [
            _user("change the design"),
            WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
            WorkspaceMutationEvent(operation="agent.run-admitted"),
            _action("file_write"),
            _obs("file_write"),
        ]
    )
    assert pending_workspace_run_intent(events) is None
    assert signals.has_unprocessed_user_message(events) is False


def test_legacy_untyped_admission_required_and_pending():
    """Legacy intent (no run_protocol_version) still requires admission and
    tracks pending status through the sequence-only path."""
    events = _seq(
        [
            _user("change the design"),
            WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
            _agent("late output from old request"),
        ]
    )
    assert workspace_run_intent_admission_required(events) is True
    assert pending_workspace_run_intent(events) is not None

    events = _seq([*events, WorkspaceMutationEvent(operation="agent.run-admitted")])
    assert workspace_run_intent_admission_required(events) is False
    assert pending_workspace_run_intent(events) is not None

    events = _seq([*events, _agent("response after admitted")])
    assert pending_workspace_run_intent(events) is None


# --- agent_view_consistent_events ----------------------------------------------


def test_agent_view_consistent_preserves_concrete_input_instances():
    """The typed facade projection returns the same concrete event objects."""

    message = _user("preserve the canonical event type")
    events: list[MessageEvent] = [message]

    visible = agent_view_consistent_events(events)

    assert visible == events
    assert visible[0] is message


def test_agent_view_consistent_keeps_matching_untagged_in_legacy():
    """agent_view_consistent_events must NOT strip untagged agent events when
    no strict v1 view-admission has been seen."""
    events = _seq(
        [
            _user("hello"),
            _agent("response"),
        ]
    )
    visible = agent_view_consistent_events(events)
    assert len(visible) == 2
    assert visible[-1] is events[-1] or visible[-1].id == events[-1].id


def test_agent_view_consistent_quarantines_stale_view_output():
    """Under strict v1, agent events tagged with an old/non-current agent_view_id
    are quarantined from the visible event list."""
    events = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_v",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_v",
                agent_view_id="aview_v1",
                run_protocol_version=1,
            ),
            _agent("response from v1").model_copy(update={"agent_view_id": "aview_v1"}),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_v",
                agent_view_id="aview_v2",
                run_protocol_version=1,
            ),
            _agent("stale response from v1").model_copy(update={"agent_view_id": "aview_v1"}),
            _agent("fresh response from v2").model_copy(update={"agent_view_id": "aview_v2"}),
        ]
    )
    visible = agent_view_consistent_events(events)
    visible_ids = {e.id for e in visible}
    # All events before the second admission are kept (including the view-admitted
    # and the first aview_v1 response).  Only events strictly AFTER the winner
    # that still carry the old view id are quarantined.
    aview_v1_messages = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source is EventSource.AGENT
        and getattr(e, "agent_view_id", None) == "aview_v1"
    ]
    aview_v2_messages = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source is EventSource.AGENT
        and getattr(e, "agent_view_id", None) == "aview_v2"
    ]
    # There are two aview_v1 messages: first response before v2 admission (kept),
    # stale response after v2 admission (quarantined)
    assert len(aview_v1_messages) == 2
    assert len(aview_v2_messages) == 1
    # The first v1 response (before v2 admission) is kept
    first_v1 = aview_v1_messages[0]
    assert first_v1.id in visible_ids
    # The stale v1 response after v2 admission is quarantined
    stale_v1 = aview_v1_messages[1]
    assert stale_v1.id not in visible_ids
    # The v2 response is kept
    assert aview_v2_messages[0].id in visible_ids
    assert len(visible) <= len(events)


# --- [1] v1 strict not downgraded by later legacy untyped intent -------------


def test_v1_strict_not_downgraded_by_later_legacy_intent():
    """Once strict v1 protocol is active, a later untyped legacy run-intent
    cannot downgrade strictness — strict mode persists, untagged progress
    stays unresolved, and untagged FINISHED is rejected."""
    events = _seq(
        [
            _user("v1 instruction"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_v1",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_v1",
                agent_view_id="aview_v1",
                run_protocol_version=1,
            ),
            _agent("v1 progress").model_copy(update={"agent_view_id": "aview_v1"}),
        ]
    )
    assert pending_workspace_run_intent(events) is None

    # Legacy untyped intent arrives — must NOT downgrade strictness
    events2 = _seq([*events, WorkspaceMutationEvent(operation="agent.run-intent.user-turn")])
    assert pending_workspace_run_intent(events2) is not None

    # Untagged action after legacy intent must NOT resolve pending
    events3 = _seq([*events2, _action("file_write")])
    assert pending_workspace_run_intent(events3) is not None

    # Untagged FINISHED must be rejected under strict v1
    terminal = StatusEvent(status=ConversationStatus.FINISHED)
    assert workspace_terminal_matches_current_run(events2, terminal) is False


# --- [5] two same-intent admissions: latest-only, no steal-back -------------


def test_two_same_intent_admissions_latest_only():
    """Two admissions for the same intent: the latest view defines the active
    view; stale agent_view_id progress is quarantined; the loser cannot steal
    back by re-emitting output with its old view id."""
    events = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_same",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_same",
                agent_view_id="aview_a",
                run_protocol_version=1,
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_same",
                agent_view_id="aview_b",
                run_protocol_version=1,
            ),
        ]
    )
    assert current_workspace_agent_view_id(events) == "aview_b"

    # Stale progress from aview_a after aview_b won
    events2 = _seq(
        [
            *events,
            _action("file_write").model_copy(update={"agent_view_id": "aview_a"}),
            _obs("file_write").model_copy(update={"agent_view_id": "aview_a"}),
        ]
    )
    assert pending_workspace_run_intent(events2) is not None

    # Progress from the current view resolves pending
    events3 = _seq(
        [
            *events2,
            _agent("fresh from b").model_copy(update={"agent_view_id": "aview_b"}),
        ]
    )
    assert pending_workspace_run_intent(events3) is None


def test_new_v1_intent_clears_parked_gate_state():
    """A durable v1 instruction must clear parked confirmation/plan/alternatives
    gate state to RUNNING."""
    events = _seq(
        [
            _user("original"),
            _action("risky_tool"),
            StatusEvent(status=ConversationStatus.WAITING_FOR_CONFIRMATION),
            # v1 run-intent — clears the parked gate
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_clear",
            ),
        ]
    )
    state = ConversationState.reconstruct("conv", events)
    assert state.execution_status == ConversationStatus.RUNNING
    assert state.pending_action_id is None


# --- run_intent_id authority: StatusEvent + AgentViewProjection ----------------

# [1] authority fields are mutually exclusive


def test_status_event_authority_fields_are_mutually_exclusive():
    """StatusEvent must reject more than one terminal authority field."""
    with pytest.raises(ValueError, match="status cannot carry more than one"):
        StatusEvent(
            status=ConversationStatus.ERROR,
            agent_view_id="aview_1",
            run_intent_id="intent_1",
        )
    with pytest.raises(ValueError, match="status cannot carry more than one"):
        StatusEvent(
            status=ConversationStatus.ERROR,
            agent_view_id="aview_1",
            host_mutation_id="host_1",
        )
    with pytest.raises(ValueError, match="status cannot carry more than one"):
        StatusEvent(
            status=ConversationStatus.ERROR,
            run_intent_id="intent_1",
            host_mutation_id="host_1",
        )
    with pytest.raises(ValueError, match="status cannot carry more than one"):
        StatusEvent(
            status=ConversationStatus.ERROR,
            agent_view_id="aview_1",
            run_intent_id="intent_1",
            host_mutation_id="host_1",
        )
    # Each authority field alone is valid
    StatusEvent(status=ConversationStatus.ERROR, agent_view_id="aview_1")
    StatusEvent(status=ConversationStatus.ERROR, run_intent_id="intent_1")
    StatusEvent(status=ConversationStatus.ERROR, host_mutation_id="host_1")


# [2] current pending intent-owned ERROR is semantically visible


def test_pending_intent_owned_error_is_visible():
    """A StatusEvent ERROR carrying the current pending run_intent_id (no view
    admitted yet) must be semantically visible via agent_view_consistent_events
    and drive reconstruct to ERROR."""
    events = _seq(
        [
            _user("do it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_e",
            ),
            StatusEvent(
                status=ConversationStatus.ERROR,
                run_intent_id="intent_e",
                detail="preflight failed",
            ),
        ]
    )
    visible = agent_view_consistent_events(events)
    assert len(visible) == 3
    assert visible[-1].status == ConversationStatus.ERROR  # type: ignore[union-attr]
    state = ConversationState.reconstruct("conv", events)
    assert state.execution_status == ConversationStatus.ERROR


# [3] late I1 intent-owned ERROR after I2 is raw-auditable but
#     reconstructs RUNNING for I2


def test_late_i1_intent_error_after_i2_reconstructs_running_for_i2():
    """An ERROR bound to I1 via run_intent_id after I2's run-intent has been
    recorded must be filtered from the consistent view (semantically stale)
    but remain in raw events for audit.  reconstruct shows RUNNING for I2."""
    raw = _seq(
        [
            _user("first ask"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_I1",
            ),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_I2",
            ),
            StatusEvent(
                status=ConversationStatus.ERROR,
                run_intent_id="intent_I1",
                detail="I1 crashed late",
            ),
        ]
    )
    assert len(raw) == 4  # raw audit trail is complete
    visible = agent_view_consistent_events(raw)
    visible_errors = [
        e for e in visible if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert len(visible_errors) == 0  # I1 ERROR filtered
    state = ConversationState.reconstruct("conv", raw)
    assert state.execution_status == ConversationStatus.RUNNING


# [4] I1 intent-owned ERROR after A1 is stale


def test_intent_error_after_view_admission_is_stale():
    """An ERROR bound to I1 via run_intent_id after view A1 has been admitted
    for the same intent is stale — the view is now the authority edge."""
    events = _seq(
        [
            _user("build it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_f",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_f",
                agent_view_id="aview_f",
                run_protocol_version=1,
            ),
            StatusEvent(
                status=ConversationStatus.ERROR,
                run_intent_id="intent_f",
                detail="late failure",
            ),
        ]
    )
    visible = agent_view_consistent_events(events)
    assert len(visible) == 3  # ERROR filtered out
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR for e in visible
    )


# [5] untagged ErrorEvent and untagged ERROR/STUCK/PAUSED/FINISHED cannot
#     poison a strict v1 run


def test_untagged_errors_cannot_poison_strict_v1_run():
    """Under strict v1, ErrorEvent and untagged ERROR/STUCK/PAUSED/FINISHED
    StatusEvents (no agent_view_id, no run_intent_id, no host_mutation_id)
    must all be filtered out by agent_view_consistent_events."""
    events = _seq(
        [
            _user("strict run"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_g",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_g",
                agent_view_id="aview_g",
                run_protocol_version=1,
            ),
            ErrorEvent(code="OOM", detail="out of memory"),
            StatusEvent(status=ConversationStatus.ERROR, detail="untagged crash"),
            StatusEvent(status=ConversationStatus.STUCK),
            StatusEvent(status=ConversationStatus.PAUSED),
            StatusEvent(status=ConversationStatus.FINISHED),
        ]
    )
    visible = agent_view_consistent_events(events)
    assert len(visible) == 3  # only user, intent, admission survive
    assert not any(isinstance(e, ErrorEvent) for e in visible)
    assert not any(
        isinstance(e, StatusEvent) and e.status != ConversationStatus.RUNNING for e in visible
    )


# [6] matching A1-attributed error/status remains visible


def test_matching_view_attributed_error_remains_visible():
    """A StatusEvent ERROR carrying the current agent_view_id must remain
    semantically visible."""
    events = _seq(
        [
            _user("do it"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_h",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_h",
                agent_view_id="aview_h",
                run_protocol_version=1,
            ),
            _action("file_write").model_copy(update={"agent_view_id": "aview_h"}),
            StatusEvent(
                status=ConversationStatus.ERROR,
                agent_view_id="aview_h",
                detail="tool error",
            ),
        ]
    )
    visible = agent_view_consistent_events(events)
    terminals = [
        e for e in visible if isinstance(e, StatusEvent) and e.status == ConversationStatus.ERROR
    ]
    assert len(terminals) == 1
    state = ConversationState.reconstruct("conv", events)
    assert state.execution_status == ConversationStatus.ERROR


# [7] explicit untagged host RUNNING and IDLE remain compatible


def test_untagged_running_and_idle_remain_compatible():
    """Under strict v1, untagged RUNNING and IDLE StatusEvents (no authority
    edge) must remain visible as explicit host/control transitions."""
    events = _seq(
        [
            _user("compat check"),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
                id="intent_k",
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id="intent_k",
                agent_view_id="aview_k",
                run_protocol_version=1,
            ),
            StatusEvent(status=ConversationStatus.RUNNING),
            StatusEvent(status=ConversationStatus.IDLE),
        ]
    )
    visible = agent_view_consistent_events(events)
    statuses = [e for e in visible if isinstance(e, StatusEvent)]
    assert len(statuses) == 2
    assert statuses[0].status == ConversationStatus.RUNNING
    assert statuses[1].status == ConversationStatus.IDLE
