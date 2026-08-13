"""Record/replay for the LLM seam — the router's `complete` + `stream_complete`.

Keyed on the complete provider-neutral semantic request, excluding only the
per-call correlation fields (`request_id` and opaque transport metadata), so the
same logical call replays the same response while different budgets, tool schemas,
prefills, response formats, or reasoning controls cannot collide. Recording wraps
the real `DefaultLLMRouter`; replay needs no real router (no network/LLM) — it only
exposes `complete`/`stream_complete` from the cassette, which is the entire surface
the agent + research pipeline call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from disco.core.llm.types import CompletionResponse, StreamChunk


def _req_payload(req) -> dict:
    payload = req.model_dump(mode="json", exclude={"request_id", "metadata"})
    # `requirements` is a frozenset in the contract. Its order is not semantic,
    # so canonicalize the JSON list before hashing it.
    profile = payload.get("profile")
    if isinstance(profile, dict) and isinstance(profile.get("requirements"), list):
        profile["requirements"] = sorted(profile["requirements"])
    return payload


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

    def __init__(self, cassette, config=None):
        self._cas = cassette
        # `a7ac7e60` made an INJECTED router the source of the router config:
        # `DriverRuntime.config_now()` returns `self._injected_router._config`
        # instead of loading the config store. `RecordingRouter` satisfies that
        # through its `__getattr__` delegation to the real inner router; a replay
        # run has no inner router, so it must carry the config itself. The value is
        # the one `config_now()` returned before the injected path existed — the
        # replay runtime's own in-memory ConfigStore — so replay resolution behaves
        # exactly as it did. This is configuration, not a recorded call, so it does
        # not weaken the "other attrs raise" rule above.
        if config is None:
            from disco.core.llm import ConfigStore

            config = ConfigStore(":memory:").load()
        self._config = config

    async def complete(self, req, *, context=None) -> CompletionResponse:
        row = self._cas.lookup("llm.complete", _req_payload(req))
        return CompletionResponse.model_validate(row)

    async def stream_complete(self, req, *, context=None) -> AsyncIterator[StreamChunk]:
        rows = self._cas.lookup("llm.stream", _req_payload(req))
        for r in rows:
            yield StreamChunk.model_validate(r)
