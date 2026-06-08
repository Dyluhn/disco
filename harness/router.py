"""Record/replay for the LLM seam — the router's `complete` + `stream_complete`.

Keyed on the SEMANTIC request (role + message history + tool names + temperature),
excluding the per-call `request_id` so the same logical call replays the same
response. Recording wraps the real `DefaultLLMRouter`; replay needs no real router
(no network/LLM) — it only exposes `complete`/`stream_complete` from the cassette,
which is the entire surface the agent + research pipeline call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from perpleximanus.core.llm.types import CompletionResponse, StreamChunk


def _req_payload(req) -> dict:
    return {
        "role": str(getattr(req.profile, "role", "")),
        "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        "tools": sorted(t.name for t in (req.tools or [])),
        "temperature": req.temperature,
    }


class RecordingRouter:
    """Wraps a real router; records each completion/stream verbatim, delegates
    everything else (cost sink, resolution) to the inner router."""

    def __init__(self, inner, cassette):
        self._inner = inner
        self._cas = cassette

    def __getattr__(self, name):  # delegate model_for / sinks / etc.
        return getattr(self._inner, name)

    async def complete(self, req, *, context=None) -> CompletionResponse:
        resp = await self._inner.complete(req, context=context)
        self._cas.record("llm.complete", _req_payload(req), resp.model_dump(mode="json"))
        return resp

    async def stream_complete(self, req, *, context=None) -> AsyncIterator[StreamChunk]:
        chunks: list[StreamChunk] = []
        async for ch in self._inner.stream_complete(req, context=context):
            chunks.append(ch)
            yield ch
        self._cas.record(
            "llm.stream", _req_payload(req), [c.model_dump(mode="json") for c in chunks]
        )


class ReplayRouter:
    """Serves completions/streams from the cassette — no network, no LLM. Only the
    call surface the agent + pipeline use; other attrs raise (a replay run that
    needs them is a signal the cassette is incomplete)."""

    def __init__(self, cassette):
        self._cas = cassette

    async def complete(self, req, *, context=None) -> CompletionResponse:
        row = self._cas.lookup("llm.complete", _req_payload(req))
        return CompletionResponse.model_validate(row)

    async def stream_complete(self, req, *, context=None) -> AsyncIterator[StreamChunk]:
        rows = self._cas.lookup("llm.stream", _req_payload(req))
        for r in rows:
            yield StreamChunk.model_validate(r)
