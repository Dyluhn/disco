"""The keyless MCP search tier (L21): a refusal is never a zero.

Every fixture in this file is a VERBATIM capture from a live probe of the two
hosted endpoints on 2026-09-02 (see the module docstring of
``_mcp_search_providers``). The shapes that matter are the ugly ones:

* Exa answers a burst with HTTP 429 *and* — this is the trap — sometimes with
  HTTP 200, ``isError: false``, and its rate-limit prose sitting where the
  results should be. A parser that believed that body would report an empty web,
  which is exactly the ddgs failure this tier was built to end.
* Parallel refuses a bad key with a plain HTTP 401 and no JSON-RPC envelope.

So the assertions are all one assertion, wearing different clothes: ``hits ==
[]`` is only ever allowed to happen next to a NAMED marker.
"""

from __future__ import annotations

import json

import httpx
from disco.retrieval._mcp_search_providers import (
    ExaMcpSearchProvider,
    ParallelMcpSearchProvider,
)
from disco.retrieval._transport_retry import (
    OUTCOME_AUTH_REJECTED,
    OUTCOME_KEY,
    OUTCOME_RATE_LIMITED,
    degradation_markers,
    engines_cooling,
    marker_engine,
    search_with_degradation_retry,
)

# ---- verbatim live captures (2026-09-02) ------------------------------------

# Exa `tools/call web_search_exa` content block, two records, `---`-separated.
EXA_RESULT_TEXT = (
    "Title: Coroutines and tasks — Python 3.14.7 documentation\n"
    "URL: https://docs.python.org/3/library/asyncio-task.html\n"
    "Published: N/A\n"
    "Author: N/A\n"
    "Highlights:\n"
    "Return an asynchronous context manager that can be used to limit the amount "
    "of time spent waiting on something.\n"
    "...\n"
    "\n---\n\n"
    "Title: asyncio.timeout() To Wait and Cancel Tasks – SuperFastPython\n"
    "URL: https://superfastpython.com/asyncio-timeout/\n"
    "Published: 2023-11-26T00:00:00.000Z\n"
    "Author: N/A\n"
    "Highlights:\n"
    "The asyncio module provides the timeout() asynchronous context manager.\n"
)

# The exact JSON-RPC error Exa returned under a 12-request burst.
EXA_RATE_LIMIT_MESSAGE = (
    "You've hit Exa's free MCP rate limit. To continue using without limits, "
    "create your own Exa API key.\n\nFix: Create API key at "
    "https://dashboard.exa.ai/api-keys , then either:\n"
    "- Set the header: Authorization: Bearer YOUR_EXA_API_KEY\n"
    "- Or use the URL: https://mcp.exa.ai/mcp?exaApiKey=YOUR_EXA_API_KEY"
)

# The exact `isError: true` text Exa returned for `?exaApiKey=<bad>`.
EXA_AUTH_TEXT = "web_search_exa error (401): Invalid API key\nTimestamp: 2026-09-02T05:00:56.611Z"

# Parallel `structuredContent`, trimmed to two of its results.
PARALLEL_STRUCTURED = {
    "search_id": "search_25285286316c33277d76dc6c0d314707",
    "results": [
        {
            "url": "https://gpuopen.com/learn/using_matrix_core_amd_rdna4/",
            "title": "Using the Matrix Cores of AMD RDNA 4 architecture GPUs",
            "publish_date": "2025-07-11",
            "excerpts": ["WMMA operates on matrices of 16x16 dimension only."],
        },
        {
            "url": "https://docs.python.org/3/library/asyncio-task.html",
            "title": "Coroutines and tasks",
            "publish_date": None,
            "excerpts": ["asyncio.timeout(delay)"],
        },
    ],
}

# Parallel's bad-bearer body, returned with HTTP 401 and no JSON-RPC envelope.
PARALLEL_AUTH_BODY = {"code": 16, "message": "Invalid API key (C.1)"}


def _mcp_transport(*, on_call) -> httpx.MockTransport:
    """A transport that answers the handshake and routes tools/call to `on_call`."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "sess-1"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "x"}},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/call", method
        return on_call(request, body)

    return httpx.MockTransport(handler)


def _ok(result: dict, rid: int = 2) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})


# ---- handshake + happy-path mapping -----------------------------------------


async def test_exa_handshakes_once_then_maps_its_text_records_to_hits():
    seen: list[str] = []

    def on_call(request: httpx.Request, body: dict) -> httpx.Response:
        seen.append(request.headers.get("mcp-session-id", ""))
        assert body["params"]["name"] == "web_search_exa"
        if len(seen) == 1:
            assert body["params"]["arguments"] == {"query": "asyncio timeout", "numResults": 8}
        return _ok({"content": [{"type": "text", "text": EXA_RESULT_TEXT}]})

    provider = ExaMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("asyncio timeout", limit=8)

    assert [hit.url for hit in hits] == [
        "https://docs.python.org/3/library/asyncio-task.html",
        "https://superfastpython.com/asyncio-timeout/",
    ]
    assert hits[0].title.startswith("Coroutines and tasks")
    assert "asynchronous context manager" in hits[0].snippet
    assert hits[0].published_at is None  # "N/A" is not a date, and is not invented
    assert hits[1].published_at is not None and hits[1].published_at.year == 2023
    assert [hit.source_engine for hit in hits] == ["exa", "exa"]
    assert diagnostic["result_count"] == 2
    assert "provider_error" not in diagnostic and "unresponsive_engines" not in diagnostic

    # The session is negotiated once and reused, not renegotiated per query.
    await provider.search_detailed("second query", limit=8)
    assert seen == ["sess-1", "sess-1"]


async def test_parallel_maps_structured_content_and_sends_a_stable_session():
    sessions: list[str] = []

    def on_call(request: httpx.Request, body: dict) -> httpx.Response:
        args = body["params"]["arguments"]
        assert body["params"]["name"] == "web_search"
        if not sessions:
            assert args["objective"] == "rdna4 wmma"
            assert args["search_queries"] == ["rdna4 wmma"]
        sessions.append(args["session_id"])
        return _ok(
            {
                "content": [{"type": "text", "text": json.dumps(PARALLEL_STRUCTURED)}],
                "structuredContent": PARALLEL_STRUCTURED,
                "isError": False,
            }
        )

    provider = ParallelMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("rdna4 wmma", limit=8)

    assert [hit.url for hit in hits] == [
        "https://gpuopen.com/learn/using_matrix_core_amd_rdna4/",
        "https://docs.python.org/3/library/asyncio-task.html",
    ]
    assert hits[0].published_at is not None and hits[0].published_at.year == 2025
    assert hits[1].published_at is None  # publish_date: null stays null
    assert "16x16" in hits[0].snippet
    assert diagnostic["result_count"] == 2

    # Parallel's own schema says the free tier is paced per session_id, so the
    # same id has to ride every call from one provider instance.
    await provider.search_detailed("second query", limit=8)
    assert len(set(sessions)) == 1 and sessions[0]


async def test_parallel_falls_back_to_the_json_text_block_without_structured_content():
    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return _ok({"content": [{"type": "text", "text": json.dumps(PARALLEL_STRUCTURED)}]})

    provider = ParallelMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, _diagnostic = await provider.search_detailed("q", limit=8)
    assert len(hits) == 2


async def test_hits_honour_the_domain_allow_list():
    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return _ok({"structuredContent": PARALLEL_STRUCTURED})

    provider = ParallelMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, _diagnostic = await provider.search_detailed(
        "q", limit=8, domains_allow=frozenset({"docs.python.org"})
    )
    assert [hit.url for hit in hits] == ["https://docs.python.org/3/library/asyncio-task.html"]


# ---- refusals are named, never empty ----------------------------------------


async def test_exa_rate_limit_arrives_as_a_marker_that_cools_exa():
    def on_call(_request: httpx.Request, body: dict) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": EXA_RATE_LIMIT_MESSAGE},
            },
        )

    provider = ExaMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("q", limit=8)

    assert hits == []
    engines, _errors = degradation_markers(diagnostic)
    assert engines and "rate limit" in engines[0]
    # The cooldown registry is a registry of ENGINES: the marker's head must be
    # exactly the provider name, or the wrong thing (or nothing) cools.
    assert marker_engine(engines[0]) == "exa"


async def test_a_rate_limit_body_returned_with_http_200_is_still_a_refusal():
    """The trap the live burst exposed: Exa serves its rate-limit prose as a
    successful result. Zero parsed records + a non-empty body is classified."""

    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return _ok({"content": [{"type": "text", "text": EXA_RATE_LIMIT_MESSAGE}]})

    provider = ExaMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("q", limit=8)

    assert hits == []
    engines, errors = degradation_markers(diagnostic)
    assert engines or errors  # NEVER a clean zero
    assert "rate limit" in (engines + errors)[0]


async def test_exa_auth_rejection_names_the_key_and_cools_no_engine():
    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return _ok({"content": [{"type": "text", "text": EXA_AUTH_TEXT}], "isError": True})

    provider = ExaMcpSearchProvider(api_key="bad", transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("q", limit=8)

    assert hits == []
    engines, errors = degradation_markers(diagnostic)
    assert errors and "auth rejected" in errors[0]
    # A refused key says nothing about the engine, so nothing about the engine
    # may be remembered — no engine marker means no cooldown.
    assert engines == ()


async def test_parallel_http_401_without_a_json_rpc_envelope_is_an_auth_marker():
    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return httpx.Response(401, json=PARALLEL_AUTH_BODY)

    provider = ParallelMcpSearchProvider(
        api_key="bad", transport=_mcp_transport(on_call=on_call)
    )
    hits, diagnostic = await provider.search_detailed("q", limit=8)

    assert hits == []
    _engines, errors = degradation_markers(diagnostic)
    assert errors and errors[0].startswith("parallel: auth rejected")


async def test_a_quota_refusal_is_classified_as_a_rate_limit_and_cools():
    def on_call(_request: httpx.Request, body: dict) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": "monthly quota exhausted"},
            },
        )

    provider = ParallelMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("q", limit=8)
    assert hits == []
    engines, _errors = degradation_markers(diagnostic)
    assert engines and marker_engine(engines[0]) == "parallel"


async def test_a_transport_failure_is_named_and_stays_retryable():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    provider = ExaMcpSearchProvider(transport=httpx.MockTransport(handler))
    hits, diagnostic = await provider.search_detailed("q", limit=8)
    assert hits == []
    _engines, errors = degradation_markers(diagnostic)
    assert errors and errors[0].startswith("exa: upstream error")
    # Neither a ban nor a credential: this one is worth trying again.
    assert "rate limit" not in errors[0] and "auth rejected" not in errors[0]


async def test_a_genuinely_empty_answer_stays_a_clean_zero():
    """The other half of the contract: an honest no-results must NOT be dressed
    up as an outage, or every empty query becomes a fake infrastructure alarm."""

    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return _ok({"content": [], "structuredContent": {"results": []}, "isError": False})

    provider = ParallelMcpSearchProvider(transport=_mcp_transport(on_call=on_call))
    hits, diagnostic = await provider.search_detailed("q", limit=8)
    assert hits == []
    assert degradation_markers(diagnostic) == ((), ())


# ---- the refusal reaches the retry driver intact ----------------------------


async def test_the_rate_limit_marker_ends_the_retry_loop_and_starts_a_cooldown():
    calls = 0

    def on_call(_request: httpx.Request, body: dict) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32000, "message": EXA_RATE_LIMIT_MESSAGE},
            },
        )

    provider = ExaMcpSearchProvider(transport=_mcp_transport(on_call=on_call))

    async def attempt():
        return await provider.search_detailed("q", limit=8)

    hits, record = await search_with_degradation_retry(attempt)
    assert hits == []
    assert calls == 1  # a refusal is not retried into
    assert record[OUTCOME_KEY] == OUTCOME_RATE_LIMITED
    assert "exa" in engines_cooling()


async def test_an_auth_rejection_ends_the_loop_without_cooling_anything():
    def on_call(_request: httpx.Request, _body: dict) -> httpx.Response:
        return _ok({"content": [{"type": "text", "text": EXA_AUTH_TEXT}], "isError": True})

    provider = ExaMcpSearchProvider(api_key="bad", transport=_mcp_transport(on_call=on_call))

    async def attempt():
        return await provider.search_detailed("q", limit=8)

    _hits, record = await search_with_degradation_retry(attempt)
    assert record[OUTCOME_KEY] == OUTCOME_AUTH_REJECTED
    assert engines_cooling() == ()


async def test_an_expired_session_re_handshakes_once_instead_of_failing():
    state = {"calls": 0, "sessions": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            state["calls"] += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"sess-{state['calls']}"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        state["sessions"].append(request.headers.get("mcp-session-id"))
        if len(state["sessions"]) == 1:
            return httpx.Response(404, json={"error": {"message": "session not found"}})
        return _ok({"content": [{"type": "text", "text": EXA_RESULT_TEXT}]})

    provider = ExaMcpSearchProvider(transport=httpx.MockTransport(handler))
    hits, _diagnostic = await provider.search_detailed("q", limit=8)
    assert len(hits) == 2
    assert state["sessions"] == ["sess-1", "sess-2"]
