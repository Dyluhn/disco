"""Pure, transport-only probe helpers for the Settings "test" affordances.

These do ONE real, cheap network call and classify the outcome into the
ProbeResult vocabulary (ok / unauthorized / unreachable / error). They never
raise for an expected failure — auth rejection and an unreachable host are
RESULTS, not exceptions — so the route can return HTTP 200 with the honest
truth. Kept separate from ConfigState so they're trivially unit-testable with a
mocked httpx transport and reused by both the provider-key and data-source probes.
"""

from __future__ import annotations

import httpx
from disco.core._provider_contracts import (
    brave_search_request,
    firecrawl_extract_request,
    parse_brave_search_response,
    parse_firecrawl_response,
    parse_tavily_search_response,
    tavily_search_request,
)
from disco.core._provider_http import BoundedHttpExecutor, HttpAttemptPolicy, with_outcome


def _provider_probe_result(
    root: str,
    action: str,
    diagnostic: dict[str, object],
    *,
    usable_count: int,
) -> tuple[bool, str, str]:
    """Map the retrieval adapter's bounded diagnostic to ProbeResult vocabulary."""
    outcome = str(diagnostic.get("outcome", "upstream"))
    status_code = diagnostic.get("status_code")
    code = f" (HTTP {status_code})" if isinstance(status_code, int) else ""
    if (
        outcome == "ok"
        and usable_count > 0
        and isinstance(status_code, int)
        and 200 <= status_code < 300
    ):
        return True, "ok", f"{root} parsed {action} successfully ({usable_count} result(s))."
    if outcome == "auth":
        return False, "unauthorized", f"{root} rejected the credential{code}."
    if outcome == "timeout":
        return False, "unreachable", f"{root} timed out while testing {action}."
    return False, "error", f"{root} returned no usable {action} ({outcome}{code})."


# A short, fixed budget — a "test" button must feel instant and never hang the
# settings UI on a black-holed host. Connect + read both bounded.
_TIMEOUT = httpx.Timeout(6.0, connect=4.0)


def _provider_probe_executor(
    provider: str,
    transport: httpx.AsyncBaseTransport | None,
):
    """Use production request mapping under the Settings button's short SLA."""
    return BoundedHttpExecutor(
        provider,
        transport=transport,
        policy=HttpAttemptPolicy(deadline_s=6.0, max_attempts=1),
        concurrency=1,
    )


def _v1(base_url: str, suffix: str) -> str:
    """Join an OpenAI-compatible base (with or without a trailing /v1) to a path."""
    root = base_url.rstrip("/")
    return f"{root}{suffix}" if root.endswith("/v1") else f"{root}/v1{suffix}"


async def probe_openai_auth(
    base_url: str, api_key: str | None, model_id: str
) -> tuple[bool, str, str]:
    """The REAL "is this key live?" probe: a minimal 1-token chat completion via
    ``POST {base}/v1/chat/completions`` with the model this key is configured for.

    Why not ``GET /models``? Live-verified: some providers (OpenRouter) serve
    ``/models`` UNAUTHENTICATED — a bogus key still returns 200, so a models-list
    green would be a FALSE green. A completion genuinely exercises the Bearer key:
    a bad key returns 401/403, a good key returns 200. ``max_tokens=1`` keeps it
    ~free. A 400/404 means the credential was ACCEPTED but the model id/route was
    rejected — reachable + authorized, surfaced honestly (not a clean ok).
    """
    url = _v1(base_url, "/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    root = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=4.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {root}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {root} failed: {exc}."

    if resp.status_code in (401, 403):
        return (
            False,
            "unauthorized",
            (f"{root} answered {resp.status_code} — the key was rejected. Check the stored value."),
        )
    if resp.status_code == 200:
        return (
            True,
            "ok",
            (
                f"{root} accepted an authenticated completion with model "
                f"‘{model_id}’ — the key works."
            ),
        )
    if resp.status_code in (400, 404, 422):
        # The key passed auth but the model/route was rejected — still proves the
        # credential is valid against this endpoint; flag the model issue honestly.
        return (
            True,
            "ok",
            (
                f"{root} authenticated the key (answered {resp.status_code} on a probe "
                f"with model ‘{model_id}’ — the model id may need updating, but the key works)."
            ),
        )
    if resp.status_code == 429:
        return (
            True,
            "ok",
            (
                f"{root} answered 429 (rate-limited) — the key authenticated; you're "
                "just over a rate limit right now."
            ),
        )
    return (
        False,
        "error",
        (f"{root} answered {resp.status_code} on an authenticated completion probe."),
    )


async def probe_reachable(url: str, *, api_key: str | None = None) -> tuple[bool, str, str]:
    """Lightweight reachability check for a non-LLM HTTP service (search /
    extraction endpoints). A GET that treats ANY HTTP response as "reachable"
    (the host answered) — only a connect/timeout/DNS failure is "unreachable".
    A 401/403 is surfaced as ``unauthorized`` so a bad paid-API key reads honestly.

    Used for the self-host tiers (searxng / crawl4ai), which have no fixed vendor
    endpoint to functionally exercise. The paid vendor tiers (Brave / Tavily /
    Firecrawl) use their own probes below instead, each of which makes a REAL call
    against the real search/scrape endpoint in that vendor's actual auth shape —
    a bare GET against the root with a guessed header would report a bad key as
    merely "reachable".
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {url}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {url} failed: {exc}."
    if resp.status_code in (401, 403):
        return (
            False,
            "unauthorized",
            (f"{url} answered {resp.status_code} — reachable, but the credential was rejected."),
        )
    return True, "ok", f"{url} answered {resp.status_code} — reachable."


def _classify_vendor_probe(resp: httpx.Response, root: str, action: str) -> tuple[bool, str, str]:
    """Shared status-code → (ok, status, detail) classification for a probe that
    made ONE real, credentialed call against a vendor's actual search/extract
    endpoint. ``action`` names what was attempted (e.g. "a real search query") for
    an honest detail string. A 401/403 means the credential itself was exercised
    and rejected — a bad key can no longer read as "reachable"."""
    if resp.status_code in (401, 403):
        return (
            False,
            "unauthorized",
            f"{root} answered {resp.status_code} on {action} — the key was rejected.",
        )
    if resp.status_code == 200:
        return True, "ok", f"{root} answered {action} — the key works."
    if resp.status_code == 429:
        return (
            False,
            "error",
            f"{root} answered 429 (rate-limited) on {action} — connection test did not succeed.",
        )
    return False, "error", f"{root} answered {resp.status_code} on {action}."


async def probe_brave_search(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str, str]:
    """The REAL Brave Search probe: one real query against
    ``GET {base}/res/v1/web/search`` using Brave's actual auth header —
    ``X-Subscription-Token`` (Brave does NOT accept ``Authorization: Bearer``).
    Exercises the credential against the real search endpoint, so a bad/missing
    key reports honestly as ``unauthorized`` rather than a root-URL "reachable".

    ``transport`` is test-only (a mocked ``httpx.AsyncBaseTransport``, e.g.
    ``httpx.MockTransport``) — production callers never pass it, so the default
    ``None`` makes a real connection.
    """
    root = base_url.rstrip("/")
    if not api_key:
        return _provider_probe_result(
            root, "search result", {"outcome": "auth", "status_code": None}, usable_count=0
        )
    request = brave_search_request(root, api_key, "disco connectivity test", limit=1)
    result = await _provider_probe_executor("brave", transport).request(
        request.method,
        request.url,
        headers=request.headers,
        params=request.params,
        json=request.json,
    )
    diagnostic = dict(result.diagnostic)
    hits: list[dict] = []
    if result.response is not None and diagnostic["outcome"] == "ok":
        try:
            hits = parse_brave_search_response(result.response)
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
    return _provider_probe_result(root, "search result", diagnostic, usable_count=len(hits))


async def probe_tavily_search(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str, str]:
    """The REAL Tavily probe through the production retrieval adapter. It makes
    one real query against ``POST {base}/search`` with Tavily's current Bearer
    auth shape and requires a parsed hit.

    ``transport`` is test-only, see ``probe_brave_search``.
    """
    root = base_url.rstrip("/")
    if not api_key:
        return _provider_probe_result(
            root, "search result", {"outcome": "auth", "status_code": None}, usable_count=0
        )
    try:
        request = tavily_search_request(root, api_key, "disco connectivity test", limit=1)
    except ValueError:
        return (
            False,
            "misconfigured",
            "Tavily requires the official API origin https://api.tavily.com.",
        )
    result = await _provider_probe_executor("tavily", transport).request(
        request.method,
        request.url,
        headers=request.headers,
        params=request.params,
        json=request.json,
    )
    diagnostic = dict(result.diagnostic)
    hits: list[dict] = []
    if result.response is not None and diagnostic["outcome"] == "ok":
        try:
            hits = parse_tavily_search_response(result.response)
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
    return _provider_probe_result(root, "search result", diagnostic, usable_count=len(hits))


async def probe_firecrawl_extract(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str, str]:
    """The REAL Firecrawl probe through the production retrieval adapter: one
    V2 scrape using ``Authorization: Bearer <key>`` and a readable passage.

    ``transport`` is test-only, see ``probe_brave_search``.
    """
    root = base_url.rstrip("/")
    if not api_key:
        return _provider_probe_result(
            root,
            "readable passage",
            {"outcome": "auth", "status_code": None},
            usable_count=0,
        )
    request = firecrawl_extract_request(root, api_key, "https://example.com")
    result = await _provider_probe_executor("firecrawl", transport).request(
        request.method,
        request.url,
        headers=request.headers,
        params=request.params,
        json=request.json,
    )
    diagnostic = dict(result.diagnostic)
    usable_count = 0
    if result.response is not None and diagnostic["outcome"] == "ok":
        try:
            _meta, content = parse_firecrawl_response(result.response)
            usable_count = int(any(len(part.strip()) >= 40 for part in content.split("\n\n")))
        except (TypeError, ValueError):
            diagnostic = with_outcome(diagnostic, "invalid_response")
    return _provider_probe_result(root, "readable passage", diagnostic, usable_count=usable_count)
