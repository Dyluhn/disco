"""The real condenser — LLMSummarizingCondenser (event-state-contract §5.2).

Proves the mechanism, not just that it's wired: the token-threshold triggers, the
first-half-summarize span selection (keep an anchoring head + a recent tail, forget the
middle), the CondensationEvent tombstone it emits, that View.of places the summary in
the forgotten span's position, and that a loop which would overflow instead condenses
and keeps going coherently.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    EventSource,
    LLMMessage,
    LLMSummarizingCondenser,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    ToolCall,
    ToolResult,
    View,
)
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"


class _Summarizer:
    def __init__(self, text: str = "SUMMARY-OF-OLD-WORK") -> None:
        self.text = text
        self.calls = 0
        self.last_messages: list[LLMMessage] | None = None

    async def summarize(self, messages: list[LLMMessage]) -> str:
        self.calls += 1
        self.last_messages = messages
        return self.text


async def _seed(store: SqliteEventStore, n_pairs: int, body: str) -> list:
    """A user instruction + n action/observation pairs, each carrying `body`."""
    await store.append(
        CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="TASK"))
    )
    for i in range(n_pairs):
        tc = ToolCall(tool_name="shell", arguments={"command": "echo"})
        a = await store.append(CID, ActionEvent(thought=f"step {i}: {body}", tool_call=tc))
        await store.append(
            CID,
            ObservationEvent(
                tool_result=ToolResult(call_id=a.id, tool_name="shell", success=True, content=body),
                action_id=a.id,
            ),
        )
    return await store.get_events(CID)


# ---- should_condense: token thresholds --------------------------------------


def test_should_condense_fires_only_past_the_bound():
    c = LLMSummarizingCondenser(max_tokens=100, hard_max_tokens=200)
    empty = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    assert c.should_condense(empty, token_count=50) is None  # under the bound
    soft = c.should_condense(empty, token_count=120)
    assert soft is not None and soft.soft is True  # over soft: maintain the bound
    hard = c.should_condense(empty, token_count=250)
    assert hard is not None and hard.soft is False  # over hard: must condense now
    assert c.should_condense(empty, token_count=None) is None  # no estimate → no decision


# ---- condense: span selection + the tombstone -------------------------------


async def test_condense_forgets_the_middle_keeps_head_and_recent():
    store = SqliteEventStore(":memory:")
    events = await _seed(store, n_pairs=6, body="detail " * 20)  # 1 msg + 12 events
    summarizer = _Summarizer()
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)

    tomb = await condenser.condense(events, View.of(events), summarizer=summarizer)
    assert isinstance(tomb, CondensationEvent)
    assert summarizer.calls == 1 and tomb.summary == "SUMMARY-OF-OLD-WORK"

    # the forgotten span is the MIDDLE: it starts after the anchoring head (the 1st
    # LLM-visible event) and ends before the recent tail (the last 2).
    live = [e for e in events if e.seq is not None]
    head_seq, last_two = live[0].seq, {live[-1].seq, live[-2].seq}
    assert tomb.forgotten_start_seq > head_seq
    assert tomb.forgotten_end_seq < min(last_two)

    # View.of applies the tombstone: the summary appears, the forgotten span is gone,
    # head + recent survive.
    after = await store.append(CID, tomb)  # noqa: F841 — persist so seq is assigned
    view = View.of(await store.get_events(CID))
    assert any("SUMMARY-OF-OLD-WORK" in m.content for m in view.messages)
    assert view.forgotten_count >= 2
    assert any(m.content == "TASK" for m in view.messages)  # the anchoring head survived


async def test_condense_makes_progress_guard_no_churn_on_tiny_history():
    store = SqliteEventStore(":memory:")
    events = await _seed(store, n_pairs=1, body="x")  # too little to forget
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)
    assert await condenser.condense(events, View.of(events), summarizer=_Summarizer()) is None


async def test_empty_summary_is_not_emitted():
    store = SqliteEventStore(":memory:")
    events = await _seed(store, n_pairs=6, body="detail " * 10)
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=2, min_forget=2)
    assert await condenser.condense(events, View.of(events), summarizer=_Summarizer("   ")) is None


# ---- the loop condenses instead of overflowing ------------------------------


async def test_long_task_condenses_and_keeps_going():
    # A loop whose context would balloon: tiny thresholds + chunky observations. The
    # real condenser fires mid-run, emits a tombstone, and the loop finishes coherently.
    big = "y" * 600
    agent = ScriptedAgent(
        [action_step(args={"command": f"work {i}"}) for i in range(6)] + [finish_step()]
    )  # distinct commands so stuck-detection doesn't fire — we're isolating condensation
    condenser = LLMSummarizingCondenser(
        max_tokens=40, hard_max_tokens=80, keep_head=1, keep_recent=2, min_forget=2
    )
    summarizer = _Summarizer()
    executor = FakeExecutor(
        result=ToolResult(call_id="x", tool_name="shell", success=True, content=big)
    )
    loop, store = build_loop(agent, executor=executor, condenser=condenser, summarizer=summarizer)
    await loop.send_message("a long multi-step task")
    state = await loop.run()

    events = await store.get_events("conv")
    assert any(isinstance(e, CondensationEvent) for e in events)  # it actually condensed
    assert summarizer.calls >= 1  # via the SUMMARIZER-role model (here a fake)
    assert state.execution_status.value == "FINISHED"  # and still completed coherently


# ---- C10 — keep_recent counts tool-TURNS, not raw events -------------------


async def _seed_pairs(n: int) -> list:
    """A user instruction + n action/observation pairs (no store, hand-assigned seqs).
    Each pair is one tool-turn: action + observation. Returned list has 1 + 2n events."""
    from conftest import with_seqs

    def act(i: int) -> ActionEvent:
        return ActionEvent(
            thought=f"t{i}",
            tool_call=ToolCall(tool_name="shell", arguments={"command": f"echo {i}"}),
        )

    def obs(action: ActionEvent) -> ObservationEvent:
        return ObservationEvent(
            tool_result=ToolResult(
                call_id=action.id, tool_name="shell", success=True, content=f"out{action.id}"
            ),
            action_id=action.id,
        )

    events: list = [
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="TASK"))
    ]
    for i in range(n):
        a = act(i)
        events.append(a)
        events.append(obs(a))
    return with_seqs(events)


async def test_c10_keep_recent_counts_complete_turns_not_raw_events():
    """With N=5 tool-turns and keep_recent=2, the condenser retains EXACTLY 2
    complete turns (4 events) in the recent tail — never a dangling action. The
    forgotten span covers the other 3 turns (6 events). This is the case the old
    raw-event slicing got right by accident (even number of events per turn) — the
    test pins the new contract: k complete turns, ≈2k events."""
    n = 5
    k = 2
    events = await _seed_pairs(n)
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=k, min_forget=2)
    summarizer = _Summarizer()
    tomb = await condenser.condense(events, View.of(events), summarizer=summarizer)
    assert isinstance(tomb, CondensationEvent)

    # The recent tail = last 2 turns = last 4 events (action,obs,action,obs).
    tail_seqs = [e.seq for e in events[-2 * k :]]
    head_seq = events[0].seq
    # The forgotten span is the MIDDLE: strictly after the head, strictly before
    # the tail, and exactly the (n - k) middle turns = 2*(n - k) events.
    assert tomb.forgotten_start_seq == head_seq + 1
    assert tomb.forgotten_end_seq == tail_seqs[0] - 1
    # 2 events per turn × (n - k) middle turns.
    assert tomb.forgotten_end_seq - tomb.forgotten_start_seq + 1 == 2 * (n - k)


async def test_c10_keep_recent_one_keeps_a_complete_turn_not_an_orphan_action():
    """The case the old slicing BROKE: keep_recent=1 and the last 2 raw events
    were [action, obs] — but with an ODD recent window, the old code could slice
    to leave just the obs (orphan observation) or just the action (orphan action)
    in the tail. With the new turn-based slicing, keep_recent=1 keeps EXACTLY
    one complete turn: the action AND its observation, no half-turns anywhere.

    Construct a transcript whose last 2 raw events are [action_5, obs_5]; an
    OLD-style `len(live) - 1` slice would have left just `obs_5` in the tail —
    an orphan observation with its action in the forgotten span. The new code
    keeps the full turn [action_5, obs_5]."""
    n = 4  # 1 head msg + 4 turns = 9 events
    events = await _seed_pairs(n)
    # 1 head + 4 turns: turn 1 at [1,2], turn 2 at [3,4], turn 3 at [5,6], turn 4 at [7,8]
    # (the head is event 0). keep_recent=1 must keep turn 4 (events 7,8) intact.
    k = 1
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=k, min_forget=2)
    summarizer = _Summarizer()
    tomb = await condenser.condense(events, View.of(events), summarizer=summarizer)
    assert isinstance(tomb, CondensationEvent)

    # The forgotten span ends just before the kept turn's action (turn 4's
    # action is at event index 2*n - 1 = 7, seq = 8).
    kept_action = events[2 * n - 1]
    kept_obs = events[2 * n]
    assert kept_action.seq == 8 and kept_obs.seq == 9
    # The forgotten span ends at the seq just before the kept action.
    assert tomb.forgotten_end_seq == kept_action.seq - 1
    # And it must NOT include the kept action (boundary never splits a turn).
    assert tomb.forgotten_end_seq < kept_action.seq
    assert tomb.forgotten_end_seq < kept_obs.seq
    # Apply the tombstone + assert the kept turn survives in the View.
    view = View.of(events + [tomb])
    blob = "\n".join(m.content for m in view.messages)
    assert f"out{kept_action.id}" in blob  # the kept turn's obs is still visible
    # The forgotten span's OBS for the last middle turn is gone (it got summarized).
    middle_obs = events[2 * (n - k) - 1]  # the obs at the end of the forgotten span
    # middle_obs is the obs of the (n-k)-th turn (= turn 3 here, event index 5)
    # — it's seq 6, and the forgotten span ends at seq 7 (the seq right before
    # the kept turn's action). That puts middle_obs INSIDE the forgotten span.
    assert middle_obs.seq > tomb.forgotten_start_seq
    assert middle_obs.seq <= tomb.forgotten_end_seq


async def test_c10_boundary_never_splits_action_from_observation():
    """Exhaustively assert the boundary-safety contract: for any odd keep_recent
    in [1, 5] and any N >= keep_recent + 1 turns, the kept turn at the boundary
    is COMPLETE — the kept range contains both the action AND its observation
    (verified by action_id correlation), never just one of them.

    The old slicing failed this for every ODD keep_recent: it could put a
    dangling action in the tail with its obs in the forgotten span, or vice
    versa."""
    for k in (1, 2, 3, 4, 5):
        for n in (k + 1, k + 2, k + 4, k + 8):
            events = await _seed_pairs(n)
            condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=k, min_forget=2)
            summarizer = _Summarizer()
            tomb = await condenser.condense(events, View.of(events), summarizer=summarizer)
            assert isinstance(tomb, CondensationEvent), (k, n)

            # Build a map of action.id -> observation.event (within the raw events,
            # pre-condensation) so we can check that no kept ActionEvent has its
            # ObservationEvent in the forgotten span.
            obs_by_action_id: dict[str, ObservationEvent] = {
                e.action_id: e for e in events if isinstance(e, ObservationEvent)
            }

            # Every event INSIDE the forgotten span must NOT be an ActionEvent
            # whose observation is OUTSIDE the span (orphan action in the span
            # is fine — its obs is also in the span or was already forgotten;
            # we're checking the OTHER direction: an action at the START of a
            # turn MUST be in the span only if its obs is also in the span OR
            # the obs is the first event AFTER the span end — never an action
            # whose obs is in the kept tail).
            #
            # Equivalently: if an action at position p is in the kept tail, its
            # obs (matched by action_id) must ALSO be in the kept tail.
            tail_start_seq = tomb.forgotten_end_seq + 1
            kept_actions = [
                e
                for e in events
                if isinstance(e, ActionEvent)
                and e.seq is not None
                and e.seq >= tail_start_seq
            ]
            for a in kept_actions:
                o = obs_by_action_id.get(a.id)
                assert o is not None, f"orphan kept action (no obs anywhere): {a}"
                assert o.seq is not None
                assert o.seq >= tail_start_seq, (
                    f"keep boundary splits turn: kept action seq={a.seq} but its "
                    f"obs seq={o.seq} is in the forgotten span "
                    f"(end={tomb.forgotten_end_seq}); keep_recent={k}, n_turns={n}"
                )


async def test_c10_agent_error_obs_pairs_like_a_turn_observation():
    """An AgentErrorEvent (a tool call that FAILED — loop-emitted error in place
    of a real observation) pairs with its action the same way an ObservationEvent
    does. The keep boundary must not split an action from its agent_error."""
    from conftest import with_seqs

    # 1 head user + 2 complete turn pairs (action,obs) + 1 incomplete turn:
    # action followed by an AgentErrorEvent (the tool failed) — the action +
    # agent_error is a complete turn and must not be split.
    a1 = ActionEvent(thought="t1", tool_call=ToolCall(tool_name="shell", arguments={"c": 1}))
    o1 = ObservationEvent(
        tool_result=ToolResult(call_id=a1.id, tool_name="shell", success=True, content="ok1"),
        action_id=a1.id,
    )
    a2 = ActionEvent(thought="t2", tool_call=ToolCall(tool_name="shell", arguments={"c": 2}))
    o2 = ObservationEvent(
        tool_result=ToolResult(call_id=a2.id, tool_name="shell", success=True, content="ok2"),
        action_id=a2.id,
    )
    a3 = ActionEvent(thought="t3", tool_call=ToolCall(tool_name="shell", arguments={"c": 3}))
    e3 = AgentErrorEvent(error="boom", action_id=a3.id, tool_call_id=a3.id)
    events = with_seqs(
        [
            MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="TASK")),
            a1,
            o1,
            a2,
            o2,
            a3,
            e3,
        ]
    )
    # The post-with_seqs instances are the ones with seqs assigned. Look them
    # up by identity (a1 was reassigned, e3 was too — re-find them in `events`).
    a3_seqs = [e.seq for e in events if isinstance(e, ActionEvent)]
    e3_seqs = [e.seq for e in events if isinstance(e, AgentErrorEvent)]
    assert len(a3_seqs) == 3 and len(e3_seqs) == 1
    a3_seq = a3_seqs[-1]  # the third (last) action's seq
    e3_seq = e3_seqs[-1]  # the agent_error's seq
    # 3 turns total. keep_recent=1 → keep the last turn (a3, e3) intact.
    condenser = LLMSummarizingCondenser(keep_head=1, keep_recent=1, min_forget=2)
    summarizer = _Summarizer()
    tomb = await condenser.condense(events, View.of(events), summarizer=summarizer)
    assert isinstance(tomb, CondensationEvent)
    # The forgotten span must end BEFORE the kept turn's action — boundary is
    # at the start of turn 3, so the third action's seq is the first kept event.
    assert tomb.forgotten_end_seq < a3_seq
    assert tomb.forgotten_end_seq < e3_seq
    # The kept tail contains the action AND its agent_error.
    assert a3_seq > tomb.forgotten_end_seq
    assert e3_seq > tomb.forgotten_end_seq
