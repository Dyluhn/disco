"""Stop preserves completed research while cancelling uncommitted model work."""

import asyncio

from disco.retrieval.deep_research._recovery_state import AgentCheckpoint, RecoveryCheckpoint
from test_research_agent import _DONE, _TURN0, _bound, _FakeRetrieval, _run, _TurnRouter


async def test_stalled_research_stops_keeps_accepted_guidance_and_resumes_without_budget_reset():
    class Stalled(_TurnRouter):
        def __init__(self):
            super().__init__([_TURN0])
            self.entered = asyncio.Event()
            self.closed = asyncio.Event()

        async def complete(self, request, **kwargs):
            if self.requests:
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.closed.set()
            return await super().complete(request, **kwargs)

    router, retrieval = Stalled(), _FakeRetrieval()
    stop = asyncio.Event()
    saved, guidance = [], []

    async def checkpoint(state, cursor):
        saved.append((AgentCheckpoint.capture(state).model_copy(deep=True), cursor.model_copy()))

    def pop_guidance():
        queued = list(guidance)
        guidance.clear()
        return queued

    task = asyncio.create_task(
        _run(
            router,
            retrieval,
            should_cancel=stop.is_set,
            checkpoint=checkpoint,
            pop_steers=pop_guidance,
        )
    )
    try:
        await asyncio.wait_for(router.entered.wait(), timeout=2)
        guidance.append("Preserve the original measured limitations.")
        stop.set()
        outcome, _ = await asyncio.wait_for(task, timeout=1)
        assert router.closed.is_set()
        assert outcome.bounded_by == "stopped" and len(outcome.passages) == 4
        state, cursor = saved[-1]
        assert state.turns_charged == 2 and cursor.next_turn == 2
        assert state.trail[-1]["turn_counted_reason"] == "model_cancelled"
        assert any(
            r.get("text") == "Preserve the original measured limitations." for r in state.trail
        )
        recovery = RecoveryCheckpoint(
            conversation_id="conv_test",
            run_id="stopped",
            query="the state of X",
            depth_tier="quick",
            stage="research",
            bound=_bound(),
            state=state,
            cursor=cursor,
        )
        resumed = _TurnRouter([_DONE])
        finished, _ = await _run(resumed, retrieval, recovery=recovery, checkpoint=checkpoint)
        assert finished.bounded_by != "stopped"
        assert saved[-1][0].turns_charged >= 2
        assert {p.id for p in outcome.passages} <= {p.id for p in finished.passages}
        assert (
            "Preserve the original measured limitations."
            in resumed.requests[0].messages[-1].content
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
