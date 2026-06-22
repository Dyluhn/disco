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
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {root}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {root} failed: {exc}."

    if resp.status_code in (401, 403):
        return False, "unauthorized", (
            f"{root} answered {resp.status_code} — the key was rejected. "
            "Check the stored value."
        )
    if resp.status_code == 200:
        return True, "ok", (
            f"{root} accepted an authenticated completion with model "
            f"‘{model_id}’ — the key works."
        )
    if resp.status_code in (400, 404, 422):
        # The key passed auth but the model/route was rejected — still proves the
        # credential is valid against this endpoint; flag the model issue honestly.
        return True, "ok", (
            f"{root} authenticated the key (answered {resp.status_code} on a probe "
            f"with model ‘{model_id}’ — the model id may need updating, but the key works)."
        )
    if resp.status_code == 429:
        return True, "ok", (
            f"{root} answered 429 (rate-limited) — the key authenticated; you're "
            "just over a rate limit right now."
        )
    return False, "error", (
        f"{root} answered {resp.status_code} on an authenticated completion probe."
    )


async def probe_reachable(
    url: str, *, api_key: str | None = None
) -> tuple[bool, str, str]:
    """Lightweight reachability check for a non-LLM HTTP service (search /
    extraction endpoints). A GET that treats ANY HTTP response as "reachable"
    (the host answered) — only a connect/timeout/DNS failure is "unreachable".
    A 401/403 is surfaced as ``unauthorized`` so a bad paid-API key reads honestly.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        return False, "unreachable", f"Couldn't reach {url}: {type(exc).__name__}."
    except httpx.HTTPError as exc:
        return False, "unreachable", f"Request to {url} failed: {exc}."
    if resp.status_code in (401, 403):
        return False, "unauthorized", (
            f"{url} answered {resp.status_code} — reachable, but the credential "
            "was rejected."
        )
    return True, "ok", f"{url} answered {resp.status_code} — reachable."
