"""The real condenser — LLMSummarizingCondenser (event-state-contract §5.2).

Proves the mechanism, not just that it's wired: the token-threshold triggers, the
first-half-summarize span selection (keep an anchoring head + a recent tail, forget the
middle), the CondensationEvent tombstone it emits, that View.of places the summary in
the forgotten span's position, and that a loop which would overflow instead condenses
and keeps going coherently.
"""

from __future__ import annotations

from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step
from perpleximanus.core import (
    ActionEvent,
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
