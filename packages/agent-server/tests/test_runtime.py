"""The conversation runtime — the loop runs in the server (Stage 2), hermetic.

A fake-backed router is injected so this runs in CI with no network: a message
sent over the WebSocket kicks the loop, the (fake) model answers, and the answer
streams back as an agent message + FINISHED.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from fastapi.testclient import TestClient


class _FakeProvider:
    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, req, *, model):
        return CompletionResponse(
            text=self._text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(delta_text=self._text)
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


def _runtime(store: SqliteEventStore, text: str) -> ConversationRuntime:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _FakeProvider(text)})
    return ConversationRuntime(store, router=router)


def _drain(ws, *, limit: int = 40) -> tuple[str | None, bool]:
    answer, finished = None, False
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] != "event":
            continue
        ev = frame["event"]
        if ev["kind"] == "message" and ev.get("source") == "agent":
            answer = ev["message"]["content"]
        elif ev["kind"] == "status" and ev["status"] in ("FINISHED", "ERROR"):
            finished = ev["status"] == "FINISHED"
            break
    return answer, finished


def test_message_kicks_the_loop_and_streams_the_answer():
    store = SqliteEventStore(":memory:")
    app = create_app(store, runtime=_runtime(store, "Hello from the loop."))
    client = TestClient(app)
    cid = client.post("/conversations", json={"owner_id": "local"}).json()["conversation_id"]

    with client.websocket_connect(f"/ws/conversations/{cid}") as ws:
        ws.send_json({"type": "send_message", "content": "hi"})
        answer, finished = _drain(ws)

    assert finished  # the loop ran to completion in the server
    assert answer == "Hello from the loop."  # the model's answer reached the client


def test_no_runtime_means_the_wire_layer_still_just_appends():
    # Backward-compat: create_app(store) with no runtime keeps Phase-0 behavior —
    # a message is appended but no loop runs (no agent answer).
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store))
    cid = client.post("/conversations", json={"owner_id": "local"}).json()["conversation_id"]
    resp = client.post(f"/conversations/{cid}/messages", json={"content": "hi"})
    assert resp.status_code == 200
    events = client.get(f"/conversations/{cid}/events").json()["events"]
    assert [e["kind"] for e in events] == ["message"]  # just the user message; no loop


def test_create_conversation_applies_depth_tier():
    """The POST /conversations `depth_tier` must reach the runtime — it was dropped
    (handler set surface+model but never depth), so every Deep Research run silently
    used the standard_deep default regardless of the UI picker."""
    from disco.retrieval.deep_research import DepthTier

    store = SqliteEventStore(":memory:")
    runtime = _runtime(store, "x")
    client = TestClient(create_app(store, runtime=runtime))

    cid = client.post(
        "/conversations",
        json={"owner_id": "local", "surface": "deep_research", "depth_tier": "exhaustive"},
    ).json()["conversation_id"]
    assert runtime._depth_for(cid) == DepthTier.EXHAUSTIVE

    cid_q = client.post(
        "/conversations",
        json={"owner_id": "local", "surface": "deep_research", "depth_tier": "quick"},
    ).json()["conversation_id"]
    assert runtime._depth_for(cid_q) == DepthTier.QUICK

    # omitted → the standard_deep default still applies
    cid_def = client.post(
        "/conversations", json={"owner_id": "local", "surface": "deep_research"}
    ).json()["conversation_id"]
    assert runtime._depth_for(cid_def) == DepthTier.STANDARD_DEEP


def test_create_conversation_applies_iterative():
    """A4: the POST /conversations `iterative` flag must reach the runtime — it
    flows UI→request→set_iterative exactly like depth_tier→set_depth. Default OFF."""
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store, "x")
    client = TestClient(create_app(store, runtime=runtime))

    cid_on = client.post(
        "/conversations",
        json={"owner_id": "local", "surface": "deep_research", "iterative": True},
    ).json()["conversation_id"]
    assert runtime._iterative_for(cid_on) is True

    cid_off = client.post(
        "/conversations",
        json={"owner_id": "local", "surface": "deep_research", "iterative": False},
    ).json()["conversation_id"]
    assert runtime._iterative_for(cid_off) is False

    # omitted → the OFF default still applies
    cid_def = client.post(
        "/conversations", json={"owner_id": "local", "surface": "deep_research"}
    ).json()["conversation_id"]
    assert runtime._iterative_for(cid_def) is False
