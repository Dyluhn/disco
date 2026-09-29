"""Transport integration: real HTTP adapter -> real router -> attempt diagnostics.

Covers the seam the unit tests in ``test_model_attempt_diagnostics.py`` call
out but cannot prove: a real generic OpenAI-compatible adapter raising through
a real ``DefaultLLMRouter`` must retain ``http_status`` / ``error_cause_class``
in inspect bookkeeping on BOTH buffered (``complete``) and streaming
(``stream_complete``) failures. Hermetic: ``httpx.MockTransport`` / in-memory
byte streams only, no network. Endpoint, adapter, and model identifiers are
arbitrary; no familiar vendor name selects behavior.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from disco.core import LLMMessage
from disco.core.inspect import registry
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    DefaultLLMRouter,
    LLMTransientError,
    ModelEntry,
    ModelRole,
    RouterConfig,
)
from disco.core.llm.openai_provider import OpenAIProvider

RAW_MARKER = "probe-raw-response-marker-7f3a"
ENDPOINT = "http://probe-transport.invalid/v1"


def _req() -> CompletionRequest:
    return CompletionRequest(
        profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
        messages=[LLMMessage(role="user", content="probe")],
        max_tokens=50,
        temperature=0.0,
    )


def _adapter(transport: httpx.AsyncBaseTransport) -> OpenAIProvider:
    return OpenAIProvider(ENDPOINT, name="probe-provider-t1", transport=transport)


def _503_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        503,
        json={"error": {"message": f"upstream outage {RAW_MARKER}", "type": "server_error"}},
    )


class _ReadTimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)


class _PartialThenTimeout(httpx.AsyncByteStream):
    async def __aiter__(self):
        chunk = {"model": "probe-model-t1", "choices": [{"delta": {"content": "partial"}}]}
        yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        raise httpx.ReadTimeout("simulated mid-stream timeout")


def _transport(failure: str, mode: str) -> httpx.AsyncBaseTransport:
    if failure == "http503":
        return httpx.MockTransport(_503_handler)
    if mode == "streaming":
        return httpx.MockTransport(
            lambda _rq: httpx.Response(
                200,
                stream=_PartialThenTimeout(),
                headers={"content-type": "text/event-stream"},
            )
        )
    return _ReadTimeoutTransport()


def _rows(cid: str) -> list[dict]:
    snapshot = registry().snapshot(cid)
    assert snapshot is not None
    return snapshot["model_attempts"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "mode"),
    [
        ("http503", "buffered"),
        ("timeout", "buffered"),
        ("http503", "streaming"),
        ("timeout", "streaming"),
    ],
)
async def test_transport_failure_flows_through_router_to_diagnostics(
    monkeypatch, failure: str, mode: str
):
    """Real adapter -> real router preserves status/cause without raw text."""
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    provider = _adapter(_transport(failure, mode))
    config = RouterConfig(
        models={
            "probe-model-key": ModelEntry(
                model_id="probe-model-t1",
                provider="probe-provider-t1",
                context_window=65_536,
            ),
        },
        default_model="probe-model-key",
    )
    router = DefaultLLMRouter(config, {"probe-provider-t1": provider})
    cid = f"probe-transport-{failure}-{mode}"
    ctx = CallContext(conversation_id=cid)

    with pytest.raises(LLMTransientError) as excinfo:
        if mode == "buffered":
            await router.complete(_req(), context=ctx)
        else:
            async for _chunk in router.stream_complete(_req(), context=ctx):
                pass

    # Original exception type survives the router (no substitution/wrapping).
    assert type(excinfo.value) is LLMTransientError

    rows = _rows(cid)
    started = [r for r in rows if r["outcome"] == "started"]
    errors = [r for r in rows if r["outcome"] == "error"]
    # Exact attempt count: every start has exactly one terminal record, no
    # duplicates, no success, terminal retry flag only on the last failure.
    expected_attempts = 1 if (failure, mode) == ("timeout", "streaming") else 8
    assert len(started) == len(errors) == expected_attempts
    assert len(rows) == 2 * expected_attempts
    assert all(r["provider"] == "probe-provider-t1" for r in rows)
    assert all(r["model"] == "probe-model-t1" for r in rows)
    assert [r["outcome"] for r in rows] == ["started", "error"] * len(started)
    assert [r["call_ordinal"] for r in rows] == [
        n for i in range(1, len(started) + 1) for n in (i, i)
    ]
    assert [r["retry_scheduled"] for r in errors] == [True] * (len(errors) - 1) + [False]
    assert all(r["error_class"] == "LLMTransientError" for r in errors)

    if failure == "http503":
        assert all(r["http_status"] == 503 for r in errors)
    else:
        assert all(r["http_status"] is None for r in errors)
        assert all(r["error_cause_class"] == "ReadTimeout" for r in errors)

    blob = str(rows)
    assert RAW_MARKER not in blob
    assert "partial" not in blob  # no chunk/response text kept
