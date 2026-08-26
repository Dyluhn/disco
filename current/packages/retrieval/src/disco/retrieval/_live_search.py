"""SearXNG HTTP response handling kept separate from provider wiring."""

from __future__ import annotations

import time
from typing import Any

import httpx


class InvalidSearxngResponse(ValueError):
    def __init__(self, message: str, *, status_code: int | None) -> None:
        super().__init__(message)
        self.status_code = status_code


async def fetch_searxng_results(
    base: str,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
    params: dict,
    provider_name: str,
) -> tuple[list, dict[str, object]]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            resp = await client.get(f"{base}/search", params=params)
            resp.raise_for_status()
            results = _parse_results(resp)
            diagnostic: dict[str, object] = {
                "provider": provider_name,
                "outcome": "ok" if results else "empty",
                "status_code": resp.status_code,
                "result_count": len(results),
                "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
            }
            _add_unresponsive(diagnostic, resp)
            return results, diagnostic
    except InvalidSearxngResponse as exc:
        return [], _failure(provider_name, "invalid_response", exc, started)
    except (httpx.HTTPError, ValueError) as exc:
        return [], _failure(provider_name, "upstream", exc, started)


def _parse_results(response: httpx.Response) -> list:
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise InvalidSearxngResponse(
            "body was not valid JSON", status_code=response.status_code
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidSearxngResponse("payload must be an object", status_code=response.status_code)
    results = payload.get("results")
    if not isinstance(results, list):
        raise InvalidSearxngResponse("results must be a list", status_code=response.status_code)
    if any(
        not isinstance(result, dict) or ("url" in result and not isinstance(result.get("url"), str))
        for result in results
    ):
        raise InvalidSearxngResponse(
            "result entries have an invalid shape", status_code=response.status_code
        )
    return results


def _add_unresponsive(diagnostic: dict[str, object], response: httpx.Response) -> None:
    unresponsive = response.json().get("unresponsive_engines", [])
    if isinstance(unresponsive, list) and unresponsive:
        diagnostic["unresponsive_engines"] = [str(item)[:80] for item in unresponsive[:20]]


def _failure(provider: str, outcome: str, exc: Exception, started: float) -> dict[str, object]:
    status_code = exc.status_code if isinstance(exc, InvalidSearxngResponse) else None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
    return {
        "provider": provider,
        "outcome": outcome,
        "provider_error": type(exc).__name__,
        "status_code": status_code,
        "result_count": 0,
        "latency_ms": max(0, int((time.perf_counter() - started) * 1_000)),
    }
