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

# A short, fixed budget — a "test" button must feel instant and never hang the
# settings UI on a black-holed host. Connect + read both bounded.
_TIMEOUT = httpx.Timeout(6.0, connect=4.0)


def _v1(base_url: str, suffix: str) -> str:
    """Join an OpenAI-compatible base (with or without a trailing /v1) to a path."""
    root = base_url.rstrip("/")
    return f"{root}{suffix}" if root.endswith("/v1") else f"{root}/v1{suffix}"


async def probe_openai_auth(
    base_url: str, api_key: str | None, model_id: str
) -> tuple[bool, str, str]:
    """Attempt one minimal chat completion with the configured credentials.

    A successful completion proves this request worked. It does not establish
    that the endpoint enforces authentication. Rejections, including rate limits
    and model/route errors, prove neither valid credentials nor a usable model.
    No alternative request shapes are probed to infer capabilities.
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
            f"{root} answered {resp.status_code} — access was rejected. "
            "Check the credential and its permissions.",
        )
    if resp.status_code == 200:
        if not _has_completion_message(resp):
            return False, "error", f"{root} answered 200 without a recognizable chat completion."
        return True, "ok", f"{root} returned a completion with model ‘{model_id}’."
    if resp.status_code in (400, 404, 405, 422):
        return (
            False,
            "error",
            f"{root} answered {resp.status_code} for model ‘{model_id}’. "
            "Check the configured model, endpoint, and request compatibility; "
            "this response does not establish whether the credential is valid.",
        )
    if resp.status_code == 429:
        return False, "error", f"{root} answered 429; rate or quota limits prevented the test."
    return False, "error", f"{root} answered {resp.status_code} on a completion probe."


def _has_completion_message(resp: httpx.Response) -> bool:
    """Recognize the chat envelope without requiring a nonempty one-token answer."""
    try:
        payload = resp.json()
    except ValueError:
        return False
    choices = payload.get("choices") if isinstance(payload, dict) else None
    return (
        isinstance(choices, list)
        and bool(choices)
        and isinstance(choices[0], dict)
        and isinstance(choices[0].get("message"), dict)
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
            f"{root} answered 429 on {action}; rate or quota limits prevented the test.",
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
    url = f"{root}/res/v1/web/search"
    headers = {"X-Subscription-Token": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            resp = await client.get(
                url, params={"q": "disco connectivity test", "count": 1}, headers=headers
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {root}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {root} failed: {exc}."
    return _classify_vendor_probe(resp, root, "a real search query")


async def probe_tavily_search(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str, str]:
    """The REAL Tavily probe: one real query against ``POST {base}/search`` in
    Tavily's CURRENT auth shape — ``Authorization: Bearer <key>``, with the key
    absent from the JSON body. (The body-key form this probe used to send is the
    obsolete one; it made a good key look rejected and a bad one look fine.)

    The shape is deliberately identical to what
    ``disco.retrieval.bundled_providers.TavilySearchProvider`` sends, so a green
    here means the runtime adapter will work — the whole point of the button.

    ``transport`` is test-only, see ``probe_brave_search``.
    """
    root = base_url.rstrip("/")
    url = f"{root}/search"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"query": "disco connectivity test", "max_results": 1}
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {root}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {root} failed: {exc}."
    return _classify_vendor_probe(resp, root, "a real search query")


async def probe_firecrawl_extract(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str, str]:
    """The REAL Firecrawl probe: one real scrape against ``POST {base}/v2/scrape``
    using Firecrawl's actual auth shape — ``Authorization: Bearer <key>``. V2 is
    the current scrape contract, and the same one
    ``disco.retrieval.bundled_providers.FirecrawlExtractionProvider`` speaks:
    probing a contract the runtime does not use is how a green button lies.

    ``transport`` is test-only, see ``probe_brave_search``.
    """
    from disco.retrieval.bundled_providers import firecrawl_base_url

    root = firecrawl_base_url(base_url)
    url = f"{root}/v2/scrape"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"url": "https://example.com", "formats": ["markdown"]}
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {root}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {root} failed: {exc}."
    return _classify_vendor_probe(resp, root, "a real scrape request")
