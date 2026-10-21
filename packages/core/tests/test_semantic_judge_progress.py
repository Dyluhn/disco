"""A productive judge can finish; silence and incomplete output cannot pass."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from disco.core.llm.stream_progress import (
    await_with_progress_timeout,
    observe_stream_progress,
    reporter_for,
)
from disco.core.loop import TypedVerifierVerdict, VerifierContextSeed
from disco.core.loop.finish.verify_gate_parts.semantic_claims import _run_semantic_judge

_IDLE = 0.10


def _gate():
    return SimpleNamespace(_loop=SimpleNamespace(_verifier_judge_timeout_s=_IDLE))


def _reporter():
    reporter = reporter_for({"inspect_stage": "semantic_verifier"})
    assert reporter is not None
    reporter._interval_s = 0
    return reporter


async def test_productive_reasoning_and_retry_finish_beyond_original_deadline():
    seen = []

    async def outer(progress):
        seen.append(progress)

    class Judge:
        async def judge(self, seed):
            # Both calls share the inactivity budget, but have distinct counters.
            for _ in range(2):
                reporter = _reporter()
                await reporter.waiting()
                for _ in range(5):
                    await asyncio.sleep(0.025)
                    await reporter.observed(0, 1)
                await reporter.close()
            return TypedVerifierVerdict(verified=True, verdict="pass", detail="Evidence matches")

    with observe_stream_progress(outer):
        verdict, applied, cause = await _run_semantic_judge(_gate(), Judge(), VerifierContextSeed())
        await _reporter().waiting()  # The original observer is restored afterwards.
    assert verdict.verified and applied and not cause
    assert len({row.stream_id for row in seen}) == 3
    assert sum(row.reasoning_tokens for row in seen if row.final) == 10
    assert reporter_for(None) is None


@pytest.mark.parametrize("noise", ["silence", "waiting", "backoff", "buffered", "duplicate"])
async def test_no_output_or_duplicate_reports_do_not_prevent_timeout(noise):
    cancelled = False

    class Judge:
        async def judge(self, seed):
            nonlocal cancelled
            reporter = _reporter()
            if noise == "duplicate":
                await reporter.observed(0, 1)
            try:
                while True:
                    if noise == "waiting":
                        await _reporter().waiting()
                    elif noise == "backoff":
                        await reporter.retrying(1, 1.0, 429)
                    elif noise == "buffered":
                        await _reporter().buffered_start()
                    elif noise == "duplicate":
                        await reporter._report(0, final=True)
                    await asyncio.sleep(0.01)
            finally:
                cancelled = True

    verdict, applied, cause = await _run_semantic_judge(_gate(), Judge(), VerifierContextSeed())
    assert cancelled and not verdict.verified and not applied
    assert verdict.verdict == "unavailable"
    assert "inactivity" in cause


async def test_active_judge_cancellation_drains_work_and_preserves_cancellation():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work():
        try:
            await _reporter().observed(1, 0)
            started.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(await_with_progress_timeout(work(), timeout=10))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert reporter_for(None) is None


async def test_failed_outer_observer_cannot_disable_progress_watchdog():
    async def broken(progress):
        raise RuntimeError("observer failure")

    async def work():
        reporter = _reporter()
        for _ in range(8):
            await asyncio.sleep(0.025)
            await reporter.observed(1, 0)
        return "complete"

    with observe_stream_progress(broken):
        assert await await_with_progress_timeout(work(), timeout=_IDLE) == "complete"


async def test_judge_exception_stays_unavailable():
    class Judge:
        async def judge(self, seed):
            await _reporter().observed(1, 0)
            raise ValueError("invalid verdict")

    verdict, applied, cause = await _run_semantic_judge(_gate(), Judge(), VerifierContextSeed())
    assert not verdict.verified and not applied
    assert verdict.verdict == "unavailable" and "ValueError" in cause
