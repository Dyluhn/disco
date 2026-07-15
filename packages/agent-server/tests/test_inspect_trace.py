"""DISCO_INSPECT per-conversation trace — Item 2.

Proves the "other end of the application" half of the testing rule: behind
DISCO_INSPECT=1, a real Build conversation's model calls are captured as a
per-conversation trace (routing decisions + agent.step spans), and the trace is
readable over REST at /api/debug/trace/{cid}. With the flag off, nothing is
captured and the endpoint is inert (404).

Hermetic: scripted MODEL only; the loop, router, sink, span handler, registry,
and REST route are all real.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime, create_app
from disco.core import (
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
)
from disco.core.inspect import (
    InspectRoutingSink,
    inspect_enabled,
    registry,
    routing_sink_for,
)
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.tools import ProcessSandboxService
from fastapi.testclient import TestClient

CID = "conv_c1"


class _ScriptedProvider:
    """Model double: per-call (text, [ProposedToolCall]); [] tool calls = finish."""

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
            usage=TokenUsage(input_tokens=3, output_tokens=5, cached_tokens=2),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


def _plan(steps: list[str]) -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="submit_plan",
        arguments={"summary": "scripted", "steps": [{"title": s} for s in steps]},
    )


def _plan_step_done(idx: int) -> ProposedToolCall:
    return ProposedToolCall(tool_name="plan_step", arguments={"index": idx, "state": "done"})


def _finish(summary: str = "done") -> ProposedToolCall:
    return ProposedToolCall(tool_name="finish", arguments={"summary": summary})


_SAFE = ProposedToolCall(
    tool_name="file_write", arguments={"path": "hello.txt", "content": "disco lives"}
)

# plan → approve → write a file → mark step done → finish. Several driver calls,
# so the trace should show multiple routing decisions + agent.step spans.
_STEPS = [
    ("here's the plan", [_plan(["write the file"])]),
    ("writing", [_SAFE]),
    ("marking done", [_plan_step_done(1)]),
    ("done", [_finish()]),
]


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


def _inspect_runtime(store: SqliteEventStore) -> ConversationRuntime:
    """A runtime whose injected router carries an InspectRoutingSink bound to CID.

    `_runtime`-style injection short-circuits `_router_now` (so the auto-wiring
    there isn't exercised — `test_routing_sink_for_*` cover that). What this DOES
    exercise end-to-end: the real loop emitting agent.step spans + the real
    DefaultLLMRouter emitting RoutingDecisions to the inspect sink, the installed
    span handler, the registry, and the REST route."""
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(
        cfg, {"fake": _ScriptedProvider(_STEPS)}, sink=InspectRoutingSink(CID)
    )
    return ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())


async def _drive_build_to_finish(runtime: ConversationRuntime) -> None:
    runtime.kick(CID)
    task = runtime._tasks.get(CID)
    if task is not None:
        await task
    # past the plan-approval gate → run the build to completion
    await runtime.approve_plan(CID)
    task = runtime._tasks.get(CID)
    if task is not None:
        await task


# ---- the capture mechanism (unit, no env churn beyond the flag) -------------


def test_routing_sink_for_enabled(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    assert inspect_enabled() is True
    sink = routing_sink_for(CID)
    assert isinstance(sink, InspectRoutingSink)


def test_routing_sink_for_disabled(monkeypatch):
    monkeypatch.delenv("DISCO_INSPECT", raising=False)
    monkeypatch.delenv("PMX_INSPECT", raising=False)
    assert inspect_enabled() is False
    # off → None → the router falls back to NullRoutingSink (zero overhead)
    assert routing_sink_for(CID) is None
    # ...and even with a cid, a disabled flag never builds a sink
    assert routing_sink_for("anything") is None


# ---- end-to-end: real loop → trace → REST -----------------------------------


async def test_build_conversation_is_traced_and_readable_over_rest(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()  # isolate from other tests sharing the singleton

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    runtime = _inspect_runtime(store)  # __init__ installs the span handler
    runtime.set_surface(CID, "build")
    await store.append(CID, _user("write hello.txt"))

    await _drive_build_to_finish(runtime)

    # (a) the registry captured this conversation
    assert CID in registry().conversations()
    snap = registry().snapshot(CID)
    assert snap is not None

    # (b) routing decisions were recorded — at least one per driver call, all on
    # the assigned model via the deterministic "pinned" path
    routing = snap["routing_decisions"]
    assert len(routing) >= 2, snap
    assert all(d["chosen_model"] == "m" for d in routing)
    assert all(d["path"] == "pinned" for d in routing)
    assert any(d["role"] == "agent_driver" for d in routing)

    # (c) agent.step spans were captured with measured fields (the loop attaches
    # model + token counts to the span end record — proof the call round-tripped)
    spans = snap["spans"]
    step_ends = [s for s in spans if s.get("span") == "agent.step" and s.get("event") == "end"]
    assert step_ends, spans
    assert any(s.get("model") == "m" and "out_tokens" in s for s in step_ends)
    # CW-7: the span also records cached_tokens (mirrors in/out) so cache-hit
    # ratio is measurable, and the count survives the debug-trace redactor.
    assert any(
        s.get("in_tokens") == 3 and s.get("out_tokens") == 5 and s.get("cached_tokens") == 2
        for s in step_ends
    ), step_ends

    # ...and the REST trace returns those counts as NUMBERS (not ***REDACTED***),
    # even though the key names contain "token".
    client_counts = TestClient(create_app(store, runtime=runtime))
    trace_body = client_counts.get(f"/api/debug/trace/{CID}").json()
    rest_step_ends = [
        s for s in trace_body["spans"] if s.get("span") == "agent.step" and s.get("event") == "end"
    ]
    assert any(s.get("in_tokens") == 3 and s.get("cached_tokens") == 2 for s in rest_step_ends), (
        rest_step_ends
    )

    # (d) the interleaved stream is ordered by true emission order (seq monotonic)
    seqs = [e["seq"] for e in snap["events"]]
    assert seqs == sorted(seqs)

    # (e) the REST snapshot mirrors the registry
    client = TestClient(create_app(store, runtime=runtime))
    res = client.get(f"/api/debug/trace/{CID}")
    assert res.status_code == 200
    body = res.json()
    assert body["conversation_id"] == CID
    assert len(body["routing_decisions"]) == len(routing)
    assert body["event_count"] == snap["event_count"]

    # the status endpoint lists the live conversation
    status = client.get("/api/debug/inspect")
    assert status.status_code == 200
    assert CID in status.json()["conversations"]


def test_endpoint_is_inert_when_inspect_disabled(monkeypatch):
    monkeypatch.delenv("DISCO_INSPECT", raising=False)
    monkeypatch.delenv("PMX_INSPECT", raising=False)
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=None))

    # both debug routes 404 with a hint, never leaking routing internals
    for path in (f"/api/debug/trace/{CID}", "/api/debug/inspect"):
        res = client.get(path)
        assert res.status_code == 404
        assert res.json()["error"] == "inspect disabled"


def test_unknown_conversation_404s_when_enabled(monkeypatch):
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=None))
    res = client.get("/api/debug/trace/conv_does_not_exist")
    assert res.status_code == 404
    assert res.json()["error"] == "no trace"
