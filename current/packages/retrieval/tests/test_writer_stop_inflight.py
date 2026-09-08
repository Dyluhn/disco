"""A slow provider must not make Stop wait for the entire writer request."""

import asyncio

import pytest
from _writer_doubles import RecordingRouter, collect_emits, outcome, pool
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.writer import write_report
from test_deep_research import _FakeNLI
from test_writer_recovery import FAILURE, arc


class StalledRouter(RecordingRouter):
    def __init__(self, stage):
        super().__init__([FAILURE])
        self.stage = stage
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def complete(self, request, **kwargs):
        if (request.metadata or {}).get("inspect_stage") == self.stage:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.closed.set()
        return await super().complete(request, **kwargs)


@pytest.mark.parametrize("stage", ["report_draft", "report_review", "report_rework"])
async def test_stop_cancels_pending_writer_request_and_preserves_prior_commit(stage):
    router = StalledRouter(stage)
    stop = asyncio.Event()
    saved = []
    if stage == "report_draft":
        _, emit = collect_emits()
        operation = write_report(
            "Compare measured evidence.",
            outcome(pool(1)),
            router=router,
            nli=_FakeNLI(),
            bound=bounds_for("quick"),
            emit=emit,
            conversation_id="stalled-writer",
            should_cancel=stop.is_set,
        )
    else:
        operation = arc(router, saved, stop=stop.is_set)
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(router.entered.wait(), timeout=2)
        stop.set()
        with pytest.raises(ResearchStopped):
            await asyncio.wait_for(task, timeout=1)
        assert router.closed.is_set()
        if saved:
            assert saved[-1].budget.used >= 1
            assert not saved[-1].budget.complete or stage == "report_rework"
            assert saved[-1].repair_started == (stage == "report_rework")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
