"""BP-12 — First-class Build resume: unit tests.

Tests the legality matrix, exactly-once environment message, and double-resume
race condition. Uses the same hermetic scripted-model harness as test_build_surface.

Legality tests inject events directly (no live loop) so they stay fast. Tests that
kick the loop cancel the background task after asserting return values.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
from perpleximanus.agent_server import ConversationRuntime, create_app
from perpleximanus.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from perpleximanus.core.events import PlanEvent
from perpleximanus.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from perpleximanus.tools import ProcessSandboxService

CID = "test-resume-cid"


# ---- shared scripted model ---------------------------------------------------


class _ScriptedProvider:
    name = "fake"

    def __init__(self, steps) -> None:
        self._steps = list(steps)
        self.calls = 0

    async def complete(self, req, *, model):
        i = min(self.calls, len(self._steps) - 1)
        self.calls += 1
        text, tcs = self._steps[i]
        return CompletionResponse(
            text=text,
            tool_calls=list(tcs),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


def _plan_event() -> PlanEvent:
    return PlanEvent(summary="scripted plan", steps=[], revision=1)


def _plan(steps: list[str]) -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="submit_plan",
        arguments={"summary": "scripted", "steps": [{"title": s} for s in steps]},
    )


def _finish(summary: str = "done") -> ProposedToolCall:
    return ProposedToolCall(tool_name="finish", arguments={"summary": summary})


def _runtime(store: SqliteEventStore, steps=None) -> ConversationRuntime:
    if steps is None:
        steps = [("done", [_finish()])]
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _ScriptedProvider(steps)})
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def _cancel_task(rt: ConversationRuntime) -> None:
    """Cancel the live loop task so tests don't hang after kicking."""
    task = rt._tasks.get(CID)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ---- legality matrix ---------------------------------------------------------
# These tests inject events directly (no live loop) and cancel the kicked task.


async def test_resume_from_paused_is_legal():
    """PAUSED → resume_conversation → ok=True."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    result = await rt.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    assert result["status"] == "RUNNING"


async def test_resume_from_idle_with_plan_is_legal():
    """IDLE + unfinished plan → resume_conversation → ok=True."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.IDLE))

    result = await rt.resume_conversation(CID)
    await _cancel_task(rt)

    assert result["ok"] is True
    assert result["status"] == "RUNNING"


async def test_resume_from_idle_without_plan_is_illegal():
    """IDLE with no plan → illegal (no work to resume)."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    # No plan event — conversation was never started.

    result = await rt.resume_conversation(CID)

    assert result["ok"] is False
    assert "illegal_state" in result["reason"]


async def test_resume_from_running_is_409():
    """RUNNING → resume_conversation → already_running."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))

    result = await rt.resume_conversation(CID)

    assert result["ok"] is False
    assert result["reason"] == "already_running"


async def test_resume_from_finished_is_409():
    """FINISHED → resume_conversation → conversation_finished."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))

    result = await rt.resume_conversation(CID)

    assert result["ok"] is False
    assert result["reason"] == "conversation_finished"


async def test_resume_from_error_is_409():
    """ERROR → resume_conversation → conversation_error."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.ERROR))

    result = await rt.resume_conversation(CID)

    assert result["ok"] is False
    assert result["reason"] == "conversation_error"


async def test_resume_from_stuck_is_illegal():
    """STUCK → resume_conversation → illegal_state (send_message is the path forward)."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.STUCK))

    result = await rt.resume_conversation(CID)

    assert result["ok"] is False
    assert "illegal_state" in result["reason"]


# ---- environment message exactly once ----------------------------------------


async def test_resume_appends_environment_message_exactly_once():
    """resume_conversation appends exactly one 'Resumed by user.' env message."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    events_before = await store.get_events(CID)
    resume_msgs_before = [
        e for e in events_before
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "Resumed by user." in e.message.content
    ]
    assert len(resume_msgs_before) == 0

    await rt.resume_conversation(CID)
    await _cancel_task(rt)

    events_after = await store.get_events(CID)
    resume_msgs_after = [
        e for e in events_after
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "Resumed by user." in e.message.content
    ]
    assert len(resume_msgs_after) == 1


# ---- double-resume race -------------------------------------------------------


async def test_double_resume_second_call_is_409():
    """Second resume_conversation call while RUNNING (after first resume) → already_running."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    # First resume: ok=True, flips status to RUNNING in the event log.
    result1 = await rt.resume_conversation(CID)
    assert result1["ok"] is True

    # Second resume: status is now RUNNING → 409.
    result2 = await rt.resume_conversation(CID)
    assert result2["ok"] is False
    assert result2["reason"] == "already_running"

    await _cancel_task(rt)


# ---- HTTP route (via TestClient) ---------------------------------------------


async def _async_client(store: SqliteEventStore, rt: ConversationRuntime):
    """An async httpx client backed by the ASGI app — route runs in THIS event loop
    so background tasks are in scope and can be cancelled after each test."""
    app = create_app(store, runtime=rt)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_http_resume_paused_returns_ok():
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build it"))
    await store.append(CID, _plan_event())
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    async with await _async_client(store, rt) as client:
        resp = await client.post(f"/conversations/{CID}/resume")

    await _cancel_task(rt)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "RUNNING"


async def test_http_resume_running_returns_409():
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))

    async with await _async_client(store, rt) as client:
        resp = await client.post(f"/conversations/{CID}/resume")

    assert resp.status_code == 409
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["ok"] is False
    assert detail["reason"] == "already_running"


async def test_http_resume_finished_returns_409():
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    await store.append(CID, StatusEvent(status=ConversationStatus.FINISHED))

    async with await _async_client(store, rt) as client:
        resp = await client.post(f"/conversations/{CID}/resume")

    assert resp.status_code == 409
    body = resp.json()
    detail = body.get("detail", body)
    assert detail["ok"] is False
    assert detail["reason"] == "conversation_finished"
