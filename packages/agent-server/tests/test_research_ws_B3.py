"""B3 — research WS emits an honest error frame on EncoderUnavailable.

Acceptance:
  (A) Reranker raises EncoderUnavailable → the WS sends a {"type": "error"}
      frame with the actionable message; the socket is NOT closed without a frame.
  (B) A healthy reranker (ample RAM) → normal research pipeline completes; no
      spurious EncoderUnavailable frame in the stream.

Both cases use injected fake providers (no network, no fastembed download).
The WS handler is exercised through FastAPI's TestClient WebSocket path —
same infrastructure as test_research.py so the real routing code runs.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import (
    CompletionResponse,
    ConfigStore,
    DefaultLLMRouter,
    ModelEntry,
    RouterConfig,
    SecretBox,
    SecretStore,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.local_encoders import EncoderUnavailable
from disco.retrieval.models import ExtractedDoc, Passage, SearchHit
from fastapi.testclient import TestClient


# ── shared fakes ────────────────────────────────────────────────────────────────


class _FakeProvider:
    name = "fake"

    async def complete(self, req, *, model):
        return CompletionResponse(
            text="Paris is the capital. [[wiki_p0]]",
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        text = "Paris is the capital. [[wiki_p0]]"
        yield StreamChunk(delta_text=text[:10])
        yield StreamChunk(delta_text=text[10:])
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


class _FakeSearch:
    async def search(self, query, *, limit=10, domains_allow=None, domains_deny=None):
        return [
            SearchHit(
                url="https://en.wikipedia.org/Paris",
                title="Paris",
                snippet="...",
                source_engine="fake",
                rank=1,
            )
        ]


class _FakeExtraction:
    async def extract_many(self, urls):
        return [
            ExtractedDoc(
                url=urls[0],
                title="Paris",
                content="Paris is the capital of France.",
                fetched_ok=True,
                status="ok",
                passages=[
                    Passage(
                        id="wiki_p0",
                        source_url=urls[0],
                        source_title="Paris",
                        text="Paris is the capital of France.",
                    )
                ],
            )
        ]


class _OOMReranker:
    """Simulates a reranker that hits the RAM guard: raises EncoderUnavailable."""

    async def rerank(self, query, passages, *, top_k):
        raise EncoderUnavailable(
            "encoder load aborted: 0.8 GB RAM available, need ~4 GB for "
            "'intfloat/multilingual-e5-large'. "
            "Lower PMX_ENCODER_TIER to 'lite' or free RAM before retrying."
        )


class _HealthyReranker:
    """Normal pass-through reranker: returns the top_k passages unchanged."""

    async def rerank(self, query, passages, *, top_k):
        return passages[:top_k]


class _FakeNLI:
    def entail(self, premise, hypothesis):
        return "entail"

    def score(self, premise, hypothesis):
        return 0.97


def _make_runtime(store, reranker) -> ConversationRuntime:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _FakeProvider()})
    return ConversationRuntime(
        store,
        router=router,
        research_providers={
            "search": _FakeSearch(),
            "extraction": _FakeExtraction(),
            "reranker": reranker,
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )


# ── (A) LOW RAM path: WS emits honest error frame ────────────────────────────


def test_research_ws_encoder_unavailable_emits_error_frame():
    """When the reranker raises EncoderUnavailable the WS must send a
    {"type": "error"} frame with the actionable message.  The socket MUST NOT
    close without a frame — the client must see the reason."""
    store = SqliteEventStore(":memory:")
    runtime = _make_runtime(store, _OOMReranker())
    app = create_app(store, runtime=runtime)
    client = TestClient(app)

    frames = []
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "What is the capital of France?"})
        # Collect until we see an error or state-finished (whichever comes first).
        for _ in range(30):
            try:
                f = ws.receive_json()
                frames.append(f)
                if f["type"] == "error":
                    break
                if f["type"] == "state" and f.get("status") == "finished":
                    break
            except Exception:
                break

    error_frames = [f for f in frames if f["type"] == "error"]
    assert error_frames, (
        "WS must emit an error frame when EncoderUnavailable is raised; "
        f"got frame types: {[f['type'] for f in frames]}"
    )
    msg = error_frames[0]["message"]
    # The message must carry the actionable hint (not just a bare exception name).
    assert "encoder" in msg.lower() or "RAM" in msg or "GB" in msg, (
        f"Error message must be actionable, got: {msg!r}"
    )
    # The socket must NOT have been closed before sending the error frame.
    assert len(frames) >= 1, "at least one frame (the error) must have been received"


def test_research_ws_error_frame_message_contains_tier_hint():
    """The error frame message must tell the operator how to fix the problem
    (lower the tier or free RAM) rather than just a bare exception class name."""
    store = SqliteEventStore(":memory:")
    runtime = _make_runtime(store, _OOMReranker())
    app = create_app(store, runtime=runtime)
    client = TestClient(app)

    frames = []
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "Capital of France?"})
        for _ in range(20):
            try:
                f = ws.receive_json()
                frames.append(f)
                if f["type"] in ("error", "final"):
                    break
            except Exception:
                break

    error_frames = [f for f in frames if f["type"] == "error"]
    assert error_frames, "Expected an error frame"
    msg = error_frames[0]["message"]
    # Must contain the operational hint embedded in EncoderUnavailable's message.
    assert "PMX_ENCODER_TIER" in msg or "lite" in msg, (
        f"Expected tier hint in error message, got: {msg!r}"
    )


# ── (B) AMPLE RAM path: normal stream, no spurious EncoderUnavailable ─────────


def test_research_ws_ample_ram_no_encoder_unavailable_frame():
    """With a healthy reranker (ample RAM), the research stream must complete
    normally with no EncoderUnavailable error frame in the output."""
    store = SqliteEventStore(":memory:")
    runtime = _make_runtime(store, _HealthyReranker())
    app = create_app(store, runtime=runtime)
    client = TestClient(app)

    frames = []
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "What is the capital of France?"})
        for _ in range(60):
            try:
                f = ws.receive_json()
                frames.append(f)
                if f["type"] == "state" and f.get("status") == "finished":
                    break
            except Exception:
                break

    error_frames = [f for f in frames if f["type"] == "error"]
    assert not error_frames, (
        f"No error frames expected with a healthy reranker; got: {error_frames}"
    )
    final_frames = [f for f in frames if f["type"] == "final"]
    assert final_frames, "Expected a final answer frame with a healthy reranker"
