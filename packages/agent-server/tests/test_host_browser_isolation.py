"""BF1 host-verifier ownership gates.

Host verification gets one fixed serialized page lane and always attempts to
release it. It never changes the model-facing verifier schema or the executor's
ordinary agent context.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.agent_server.verify.host import HostWebAppVerifier
from disco.core.loop import HostVerificationDeliverable
from disco.tools import ToolContext, ToolOutcome
from disco.tools.builtin.browser import BrowserTool
from disco.tools.builtin.verify_app import VerifyWebAppTool


class _Executor:
    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []

    async def _build_context(self, _definition) -> ToolContext:
        ctx = ToolContext(
            sandbox=object(),
            workspace_path=".",
            timeout_s=5,
            capabilities=None,
            owner_id="owner",
            conversation_id="conv",
            browser_workspace_epoch=4,
            browser_generation="a" * 32,
            browser_lane="agent",
        )
        self.contexts.append(ctx)
        return ctx


def _deliverable() -> HostVerificationDeliverable:
    return HostVerificationDeliverable(
        conversation_id="conv",
        artifact_path="site",
        artifact_kind="app",
        deployment_url="http://127.0.0.1:8123/",
    )


@pytest.mark.asyncio
async def test_host_verifier_uses_only_fixed_serial_lane_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _Executor()
    verifier = HostWebAppVerifier(executor)
    active = 0
    maximum_active = 0
    lanes: list[tuple[str, int | None, str]] = []
    closed: list[str] = []

    async def verify_run(self, args, ctx):  # noqa: ANN001
        nonlocal active, maximum_active
        del self, args
        lanes.append((ctx.browser_lane, ctx.browser_workspace_epoch, ctx.browser_generation))
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ToolOutcome(
            success=True,
            content="pass",
            structured={"passed": True, "verdict": "pass"},
        )

    async def close_lane(self, ctx):  # noqa: ANN001
        del self
        closed.append(ctx.browser_lane)
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    first, second = await asyncio.gather(
        verifier.verify(_deliverable()), verifier.verify(_deliverable())
    )

    assert first["passed"] is True and second["passed"] is True
    assert maximum_active == 1
    assert lanes == [
        ("host_verifier", 4, "a" * 32),
        ("host_verifier", 4, "a" * 32),
    ]
    assert closed == ["host_verifier", "host_verifier"]
    # model_copy derives the host lane; the executor's canonical contexts stay agent-owned.
    assert [ctx.browser_lane for ctx in executor.contexts] == ["agent", "agent"]


@pytest.mark.asyncio
async def test_host_verifier_closes_lane_after_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _Executor()
    verifier = HostWebAppVerifier(executor)
    closed: list[str] = []

    async def failing_run(self, args, ctx):  # noqa: ANN001
        del self, args
        assert ctx.browser_lane == "host_verifier"
        raise RuntimeError("probe failed")

    async def close_lane(self, ctx):  # noqa: ANN001
        del self
        closed.append(ctx.browser_lane)
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", failing_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    verdict = await verifier.verify(_deliverable())

    assert verdict["verdict"] == "unavailable"
    assert closed == ["host_verifier"]


@pytest.mark.asyncio
async def test_lane_cleanup_failure_does_not_replace_host_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = HostWebAppVerifier(_Executor())

    async def verify_run(self, args, ctx):  # noqa: ANN001
        del self, args
        assert ctx.browser_lane == "host_verifier"
        return ToolOutcome(
            success=True,
            content="pass",
            structured={"passed": True, "verdict": "pass"},
        )

    async def broken_close(self, ctx):  # noqa: ANN001
        del self, ctx
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(VerifyWebAppTool, "run", verify_run)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", broken_close)

    verdict = await verifier.verify(_deliverable())
    assert verdict == {"passed": True, "verdict": "pass"}


@pytest.mark.asyncio
async def test_host_verifier_cancellation_propagates_after_lane_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = HostWebAppVerifier(_Executor())
    entered = asyncio.Event()
    closed: list[str] = []

    async def wait_forever(self, args, ctx):  # noqa: ANN001
        del self, args, ctx
        entered.set()
        await asyncio.Future()

    async def close_lane(self, ctx):  # noqa: ANN001
        del self
        closed.append(ctx.browser_lane)
        return True

    monkeypatch.setattr(VerifyWebAppTool, "run", wait_forever)
    monkeypatch.setattr(BrowserTool, "close_host_verifier_lane", close_lane)

    task = asyncio.create_task(verifier.verify(_deliverable()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == ["host_verifier"]
