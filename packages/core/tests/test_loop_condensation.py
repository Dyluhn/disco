"""View materialization & condensation wiring — agent-loop-contract.md §10.8."""

from __future__ import annotations

from loop_fakes import (
    FakeCondenser,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)
from perpleximanus.core import (
    CondensationEvent,
    CondensationRequest,
    ConversationStatus,
    ErrorEvent,
)
from perpleximanus.core.llm import LLMContextWindowExceeded

CID = "conv"


def _tombstone():
    return CondensationEvent(forgotten_start_seq=1, forgotten_end_seq=1, summary="[summary]")


async def test_soft_request_with_no_tombstone_proceeds_uncondensed():
    cond = FakeCondenser(request=CondensationRequest(soft=True, reason="events"), tombstone=None)
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent, condenser=cond)
    await loop.send_message("go")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED  # non-fatal
    assert cond.should_calls > 0
    events = await store.get_events(CID)
    assert not any(isinstance(e, CondensationEvent) for e in events)  # nothing condensed


async def test_context_window_exceeded_triggers_hard_reset_then_retries():
    cond = FakeCondenser(request=None, tombstone=_tombstone())
    # Step 0 raises a context-window error; after the hard reset, step 1 finishes.
    agent = ScriptedAgent([LLMContextWindowExceeded("too big"), finish_step()])
    loop, store = build_loop(agent, condenser=cond)
    await loop.send_message("huge context")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert cond.condense_calls >= 1
    events = await store.get_events(CID)
    assert any(isinstance(e, CondensationEvent) for e in events)  # hard reset appended one


async def test_unrecoverable_hard_reset_goes_to_error():
    cond = FakeCondenser(request=None, tombstone=None)  # hard reset makes no progress
    agent = ScriptedAgent([LLMContextWindowExceeded("too big"), finish_step()])
    loop, store = build_loop(agent, condenser=cond)
    await loop.send_message("huge context")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.ERROR
    errs = [e for e in await store.get_events(CID) if isinstance(e, ErrorEvent)]
    assert len(errs) == 1 and errs[0].code == "context_window"


async def test_summarizer_is_wired_into_condensation():
    """The loop hands its summarizer to condense() and awaits both (async seam,
    event-state-contract v1.2 §5.2). That the summarizer routes the SUMMARIZER
    role specifically is verified in the router tests; here we confirm the loop
    awaits an async summarize during condensation."""

    summ = FakeSummarizer()

    class SummarizingCondenser(FakeCondenser):
        async def condense(self, events, view, *, summarizer):
            await summarizer.summarize(view.messages)  # exercise the (async) summarizer
            return await super().condense(events, view, summarizer=summarizer)

    cond = SummarizingCondenser(request=None, tombstone=_tombstone())
    agent = ScriptedAgent([LLMContextWindowExceeded("big"), finish_step()])
    loop, _ = build_loop(agent, condenser=cond, summarizer=summ)
    await loop.send_message("go")
    await loop.run()
    assert summ.calls >= 1  # the loop's summarizer was used during condensation
