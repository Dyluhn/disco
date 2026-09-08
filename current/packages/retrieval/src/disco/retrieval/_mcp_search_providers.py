"""Keyless hosted search over MCP streamable-HTTP (Exa, Parallel).

These are the BUNDLED discovery tier: two hosted MCP servers that answer web
searches with no account and no key, with a key optional for volume. They exist
because the tier they replaced (`ddgs`) could not do either half of the job — it
scraped the same HTML endpoints SearXNG does, so it inherited the same IP bans,
and its rate limit came back as an EMPTY LIST that was indistinguishable from
"the web has nothing on this". A research model answered that silence with query
rewrites, which is the loop doing the system's work with its own turns.

So the contract here is the opposite one, and it is the whole point of the
module: **a refusal is never a zero**. Every failure — a rate limit, a rejected
key, an exhausted plan, an upstream error, a transport fault — comes back
through ``search_detailed`` as a NAMED marker in the provider diagnostic, in the
one vocabulary ``_transport_retry`` already classifies. A rate limit names the
provider as the cooling engine, so the process stops retrying into it; a
rejected credential names no engine at all, because a key is not an engine and
the moment it is fixed the next query must go straight out.

The trap the live probe found (2026-09-02, both endpoints, from this host):
under burst, Exa answers some requests with **HTTP 200, ``isError: false``, and
its rate-limit prose sitting where the results should be**. Parsing that body
for records yields zero, and a naive adapter would report a clean empty world —
exactly the ddgs failure, reintroduced. Hence the rule in ``_records_or_marker``:
a non-empty body that yields no records is classified, never believed.

Wire shape, verified live:

* ``POST {endpoint}`` with a JSON-RPC 2.0 body and
  ``Accept: application/json, text/event-stream`` — the answer arrives as JSON
  (Parallel) or as a one-event SSE frame (Exa), so both are decoded here.
* The MCP handshake (``initialize`` → ``notifications/initialized``) runs ONCE
  per provider instance and its ``Mcp-Session-Id`` is reused; an expired session
  (HTTP 404, per the streamable-HTTP spec) re-handshakes once.
* ``tools/call`` with ``web_search_exa`` / ``web_search``.

Origin posture: these are third-party origins, so they go through the same
``search:<provider>`` approval purpose tavily and brave do. What crosses the
boundary keyless is the QUERY, never a secret — see ``_provider_wiring``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .models import SearchHit
from .url_policy import parse_source_date, url_allowed

_LOG = logging.getLogger(__name__)

EXA_ENDPOINT = "https://mcp.exa.ai/mcp"
PARALLEL_ENDPOINT = "https://search.parallel.ai/mcp"

# OpenCode's keyless websearch uses 25 s; the live probe measured Exa at ~0.7 s
# and Parallel at 1.6–2.5 s, so this is headroom, not an expectation.
_TIMEOUT = httpx.Timeout(25.0)
_PROTOCOL_VERSION = "2025-06-18"
_JSON_RPC = "2.0"

# MCP streamable-HTTP says a request carrying a session id the server no longer
# knows is answered 404. That is the ONE status worth a second attempt: it means
# re-handshake, not "the search failed".
_SESSION_EXPIRED_STATUS = 404


def _base_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
    }


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON or single-frame-SSE JSON-RPC response body."""
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        text = "\n".join(
            line[len("data:") :].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        )
    if not text.strip():
        return {}
    try:
        decoded = json.loads(text)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


# ---- failure vocabulary ------------------------------------------------------
#
# One bounded phrase per failure, shaped so `_transport_retry.marker_engine`
# reads the head before the first ":" as the ENGINE NAME. That is what makes the
# cooldown registry a registry of providers rather than of error prose.
_MARKER_CHARS = 120
_RATE_LIMIT_PHRASES = ("rate limit", "too many requests", "quota", "429")
_AUTH_PHRASES = ("invalid api key", "unauthorized", "api key", "401", "403")


def rate_limit_marker(provider: str, detail: str) -> str:
    """``provider: rate limit`` — cools ``provider`` and is never retried."""
    return f"{provider}: rate limit ({_bounded(detail)})"


def auth_marker(provider: str, detail: str) -> str:
    """``provider: auth rejected`` — refuses the KEY, so no engine cools."""
    return f"{provider}: auth rejected ({_bounded(detail)})"


def upstream_marker(provider: str, detail: str) -> str:
    """A genuine fault: retryable, names no ban and no credential."""
    return f"{provider}: upstream error ({_bounded(detail)})"


def _bounded(detail: str) -> str:
    """Bound and flatten upstream prose — a marker is a name, not a transcript."""
    return " ".join(str(detail or "unknown").split())[:_MARKER_CHARS] or "unknown"


def classify_failure_text(provider: str, text: str, status_code: int | None) -> str:
    """Name one upstream failure in the fixed classification vocabulary.

    Both HTTP status and body text are consulted because the two endpoints
    disagree about which one carries the truth: Parallel refuses a bad key with
    HTTP 401, while Exa refuses one with HTTP 200 + ``isError`` and refuses load
    with either HTTP 429 or a 200 whose body is the rate-limit notice.
    """
    lowered = text.casefold()
    if status_code == 429 or any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES):
        return rate_limit_marker(provider, text or f"http {status_code}")
    if status_code in (401, 403) or any(phrase in lowered for phrase in _AUTH_PHRASES):
        return auth_marker(provider, text or f"http {status_code}")
    return upstream_marker(provider, text or f"http {status_code}")


def is_engine_cooling_marker(marker: str) -> bool:
    """Whether a marker is the kind that should park its provider in a cooldown.

    Only a refusal for LOAD is: the provider is fine and is telling us to stop.
    A rejected credential is a fact about a key, and a transport fault is a fact
    about one request — neither says the engine is cut off, and parking a
    healthy engine on either would delete it from the pool for nothing.
    """
    return "rate limit" in marker


def _diagnostic(marker: str, status_code: int | None, started: float) -> dict[str, object]:
    """The failure diagnostic the degradation classifier already reads.

    A load refusal is written into ``unresponsive_engines`` because that is the
    only key ``search_with_degradation_retry`` cools from. Everything else is a
    ``provider_error``: named and never silent, but not a claim that the engine
    is down.
    """
    diag: dict[str, object] = {
        "status_code": status_code,
        "result_count": 0,
        "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
    }
    if is_engine_cooling_marker(marker):
        diag["unresponsive_engines"] = [marker]
    else:
        diag["provider_error"] = marker
    return diag


def _ok_diagnostic(count: int, status_code: int | None, started: float) -> dict[str, object]:
    return {
        "status_code": status_code,
        "result_count": count,
        "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
    }


class _McpSearchProvider:
    """Shared MCP streamable-HTTP mechanics for the two keyless search servers.

    Subclasses supply the tool name, the call arguments, and the result mapping;
    everything about sessions, transport failure, and failure NAMING lives here
    so the two adapters cannot drift apart on the part that matters.
    """

    name = "mcp"
    tool_name = ""
    endpoint = ""

    def __init__(
        self,
        *,
        api_key: str = "",
        endpoint: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._endpoint = endpoint or self.endpoint
        self._transport = transport
        self._session_id: str | None = None
        self._handshaken = False
        self._lock = asyncio.Lock()

    # -- request plumbing ------------------------------------------------------

    def _request_url(self) -> str:
        return self._endpoint

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def _headers(self) -> dict[str, str]:
        headers = _base_headers() | self._auth_headers()
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _handshake(self, client: httpx.AsyncClient) -> None:
        """initialize → notifications/initialized, once per instance.

        Both servers were observed accepting ``tools/call`` with no handshake at
        all, but the spec does not promise that and a session-scoped server would
        reject it — so we pay two requests per process and keep the adapter
        correct against any MCP server, not just these two.
        """
        response = await client.post(
            self._request_url(),
            headers=_base_headers() | self._auth_headers(),
            json={
                "jsonrpc": _JSON_RPC,
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "disco", "version": "1"},
                },
            },
        )
        self._session_id = response.headers.get("mcp-session-id") or None
        self._handshaken = True
        await client.post(
            self._request_url(),
            headers=self._headers(),
            json={
                "jsonrpc": _JSON_RPC,
                "method": "notifications/initialized",
                "params": {},
            },
        )

    async def _call_tool(
        self, client: httpx.AsyncClient, arguments: dict[str, object]
    ) -> httpx.Response:
        return await client.post(
            self._request_url(),
            headers=self._headers(),
            json={
                "jsonrpc": _JSON_RPC,
                "id": 2,
                "method": "tools/call",
                "params": {"name": self.tool_name, "arguments": arguments},
            },
        )

    async def _invoke(self, arguments: dict[str, object]) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with self._lock:
                if not self._handshaken:
                    await self._handshake(client)
            response = await self._call_tool(client, arguments)
            if response.status_code != _SESSION_EXPIRED_STATUS:
                return response
            async with self._lock:
                self._handshaken = False
                await self._handshake(client)
            return await self._call_tool(client, arguments)

    # -- the SearchProvider surface -------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        hits, _diag = await self.search_detailed(
            query,
            limit=limit,
            domains_allow=domains_allow,
            domains_deny=domains_deny,
            time_filter=time_filter,
        )
        return hits

    async def search_detailed(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> tuple[list[SearchHit], dict[str, object]]:
        del time_filter  # neither server exposes a recency filter on its tool
        started = time.perf_counter()
        try:
            response = await self._invoke(self._arguments(query, limit))
        except httpx.HTTPError as exc:
            marker = upstream_marker(self.name, type(exc).__name__)
            _LOG.warning("%s search transport failure: %s", self.name, type(exc).__name__)
            return [], _diagnostic(marker, None, started)

        status = response.status_code
        body = _decode(response)
        marker = self._envelope_marker(body, status)
        if marker is not None:
            _LOG.warning("%s search refused: %s", self.name, marker)
            return [], _diagnostic(marker, status, started)

        result = body.get("result")
        result_map: dict[str, Any] = result if isinstance(result, dict) else {}
        rows, row_marker = self._records_or_marker(result_map, status)
        if row_marker is not None:
            _LOG.warning("%s search refused: %s", self.name, row_marker)
            return [], _diagnostic(row_marker, status, started)

        hits = self._to_hits(rows, limit, domains_allow, domains_deny)
        return hits, _ok_diagnostic(len(rows), status, started)

    def _envelope_marker(self, body: dict[str, Any], status: int) -> str | None:
        """Name a failure the JSON-RPC envelope or the HTTP status already made.

        An empty body from a non-2xx status is still a failure — the marker is
        the status itself rather than nothing, because "the server said no and
        explained nothing" must not read as "no results".
        """
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            return classify_failure_text(self.name, message, status)
        result = body.get("result")
        if isinstance(result, dict) and result.get("isError"):
            return classify_failure_text(self.name, _content_text(result), status)
        if status >= 400:
            return classify_failure_text(self.name, f"http {status}", status)
        if not body:
            return upstream_marker(self.name, f"unreadable response (http {status})")
        return None

    def _records_or_marker(
        self, result: dict[str, Any], status: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Rows, or the name of why a non-empty body yielded none.

        The one rule that keeps this tier from repeating ddgs: a body that
        carries text but parses to zero records is CLASSIFIED, never believed.
        Only a genuinely empty body is allowed to be a clean, honest zero.
        """
        rows = self._parse_rows(result)
        if rows:
            return rows, None
        text = _content_text(result)
        if text.strip():
            return [], classify_failure_text(self.name, text, status)
        return [], None

    # -- per-server hooks ------------------------------------------------------

    def _arguments(self, query: str, limit: int) -> dict[str, object]:
        raise NotImplementedError

    def _parse_rows(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _to_hits(
        self,
        rows: list[dict[str, Any]],
        limit: int,
        domains_allow: frozenset[str] | None,
        domains_deny: frozenset[str] | None,
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for row in rows:
            url = str(row.get("url") or "")
            if not url or not url_allowed(url, domains_allow, domains_deny):
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=str(row.get("title") or "") or url,
                    snippet=str(row.get("snippet") or ""),
                    source_engine=self.name,
                    rank=len(hits),
                    published_at=parse_source_date(row.get("published")),
                )
            )
            if len(hits) >= limit:
                break
        return hits


def _content_text(result: dict[str, Any]) -> str:
    """Concatenate the text blocks of an MCP tool result."""
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )


# ---- Exa ---------------------------------------------------------------------

# Exa's tool returns ONE text block holding formatted records, not JSON:
#   Title: …\nURL: …\nPublished: …\nAuthor: …\nHighlights:\n…
# separated by a bare "---" line. Verified live 2026-09-02.
_EXA_RECORD_SEPARATOR = "\n---\n"
_EXA_FIELDS = ("Title:", "URL:", "Published:", "Author:", "Highlights:")


class ExaMcpSearchProvider(_McpSearchProvider):
    """(c) BUNDLED search — Exa's hosted MCP endpoint, keyless, key optional.

    Keyless capacity measured live (2026-09-02): 12 concurrent searches from one
    IP returned three answers and nine ``http 429`` refusals, all within 150 ms;
    four concurrent were served cleanly. It is a small free tier, and the point
    of this adapter is that its smallness is REPORTED rather than hidden.

    An optional key raises that ceiling. Exa accepts it either as a bearer header
    or as an ``exaApiKey`` query parameter; the header is used, because a key in
    a URL ends up in logs and traces.
    """

    name = "exa"
    tool_name = "web_search_exa"
    endpoint = EXA_ENDPOINT

    def _arguments(self, query: str, limit: int) -> dict[str, object]:
        return {"query": query, "numResults": max(1, min(limit, 25))}

    def _parse_rows(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        text = _content_text(result)
        if not text.strip():
            return []
        rows: list[dict[str, Any]] = []
        for chunk in text.split(_EXA_RECORD_SEPARATOR):
            row = _parse_exa_record(chunk)
            if row is not None:
                rows.append(row)
        return rows


def _parse_exa_record(chunk: str) -> dict[str, Any] | None:
    """One ``Title:/URL:/Published:/Highlights:`` record → a row, or None."""
    fields: dict[str, str] = {}
    highlights: list[str] = []
    current: str | None = None
    for line in chunk.splitlines():
        label = next((f for f in _EXA_FIELDS if line.startswith(f)), None)
        if label is not None:
            current = label.rstrip(":")
            fields[current] = line[len(label) :].strip()
            continue
        if current == "Highlights" and line.strip() not in ("", "..."):
            highlights.append(line.strip())
    url = fields.get("URL", "")
    if not url:
        return None
    published = fields.get("Published", "")
    return {
        "url": url,
        "title": fields.get("Title", ""),
        "snippet": " ".join(highlights)[:2_000],
        "published": "" if published in ("", "N/A") else published,
    }


# ---- Parallel ----------------------------------------------------------------

# Parallel answers with a real ``structuredContent`` object:
#   {"search_id": …, "results": [{"url", "title", "publish_date", "excerpts": […]}]}
# and repeats it as JSON text in the content block. Verified live 2026-09-02.


class ParallelMcpSearchProvider(_McpSearchProvider):
    """(c) BUNDLED search — Parallel's hosted MCP endpoint, keyless, key optional.

    Keyless capacity measured live (2026-09-02): 12 concurrent searches from one
    IP all succeeded in 2.5 s wall. No published free-tier limit, so this adapter
    makes no promise about one — it only guarantees that when a limit does bite,
    it arrives as a marker and not as an empty result set.

    ``session_id`` is the field Parallel's own schema says free-tier rate
    limiting is keyed on, so one stable id per provider instance is sent: it lets
    the service pace us as one client instead of treating every query as a new
    anonymous one.
    """

    name = "parallel"
    tool_name = "web_search"
    endpoint = PARALLEL_ENDPOINT

    def __init__(
        self,
        *,
        api_key: str = "",
        endpoint: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        session_id: str = "",
    ) -> None:
        super().__init__(api_key=api_key, endpoint=endpoint, transport=transport)
        self._session = session_id or _stable_session_id()

    def _arguments(self, query: str, limit: int) -> dict[str, object]:
        del limit  # the tool caps its own result count
        return {
            "objective": query,
            "search_queries": [query],
            "session_id": self._session,
        }

    def _parse_rows(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            payload = _json_from_text(_content_text(result))
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        rows: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url:
                continue
            excerpts = item.get("excerpts")
            snippet = (
                " ".join(str(part) for part in excerpts) if isinstance(excerpts, list) else ""
            )
            rows.append(
                {
                    "url": url,
                    "title": str(item.get("title") or ""),
                    "snippet": snippet[:2_000],
                    "published": str(item.get("publish_date") or ""),
                }
            )
        return rows


def _json_from_text(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        decoded = json.loads(text)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _stable_session_id() -> str:
    """A per-instance opaque id — random, never derived from anything about the
    user or the host, and never persisted."""
    import uuid

    return uuid.uuid4().hex


__all__ = [
    "EXA_ENDPOINT",
    "PARALLEL_ENDPOINT",
    "ExaMcpSearchProvider",
    "ParallelMcpSearchProvider",
    "auth_marker",
    "classify_failure_text",
    "is_engine_cooling_marker",
    "rate_limit_marker",
    "upstream_marker",
]
