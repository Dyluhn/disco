"""Stop, observed wherever the run can actually honour it.

The research loop has always polled the cancel flag — at its turn boundary
(``agent.py``) and while holding on a dead search pool (``_hold.py``) — so a
Stop pressed during searching produced a resumable checkpoint. The WRITER never
saw the flag at all: ``write_report`` was called without it, and the draft →
review → rework arc runs for ten to twenty-five minutes at ``exhaustive``. A
Stop pressed there did nothing at all until the report landed.

The flag cannot simply be returned back up that call stack. The writer's
helpers return prose, verdicts and candidates; threading an "or stopped"
variant through each of them would put the ship decision in six places and make
"do not publish" the optional branch. So Stop leaves the writer the way a
signal leaves any deep call stack — as one exception. Writer requests can be
cancelled back to their preceding durable boundary; their request task is
drained before the exception escapes. Completed responses are committed before
Stop, so no provider stream is left open and no partial report is published.

:class:`~disco.retrieval.deep_research.engine.DeepResearchRun` is the only
place that catches it, and it answers with exactly the checkpoint a
research-loop Stop produces. Committed writer boundaries retain the draft,
review decisions, inspections, and findings for resume. Uncommitted work cannot
be recovered, and a partial report is never published.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

#: The predicate the agent-server installs — the conversation's cancel flag.
#: ``None`` means no flag was installed (every direct-helper unit test), and
#: then nothing here ever stops anything.
ShouldCancelFn = Callable[[], bool] | None


class ResearchStopped(Exception):
    """The user pressed Stop and the run reached a boundary that can honour it.

    Carries the boundary's name so a log line and a test say WHERE the run
    stopped, not merely that it did — the difference between "Stop works" and
    "Stop works in the review loop but not during a continuation".
    """

    def __init__(self, boundary: str) -> None:
        super().__init__(f"stop observed at {boundary}")
        self.boundary = boundary


def stop_requested(should_cancel: ShouldCancelFn) -> bool:
    """Whether Stop has been pressed. False when no flag is installed."""
    return should_cancel is not None and should_cancel()


def raise_if_stopped(should_cancel: ShouldCancelFn, *, boundary: str) -> None:
    """Halt the run here when Stop has been pressed.

    The branch lives in this one function rather than at each call site so a
    new boundary costs one line and cannot arrive with the polarity reversed.
    """
    if stop_requested(should_cancel):
        raise ResearchStopped(boundary)


async def await_stoppable[T](
    operation: Awaitable[T], should_cancel: ShouldCancelFn, *, boundary: str
) -> T:
    """Cancel uncommitted provider work while preserving its preceding checkpoint.

    Callers keep durable commits outside this scope. Cancellation drains the
    request task before unwinding, so an HTTP stream cannot outlive the run.
    Completed work wins the race and follows the normal commit/Stop boundary.
    """
    if should_cancel is None:
        return await operation
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            raise_if_stopped(should_cancel, boundary=boundary)
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = ["ResearchStopped", "ShouldCancelFn", "raise_if_stopped", "stop_requested"]
