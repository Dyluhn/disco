"""Bounded, cancellation-safe HTTP policy shared by provider integrations.

This private module deliberately contains transport execution only.  Provider
wire contracts live in :mod:`disco.core._provider_contracts`; keeping both in
core lets small app-server installations probe a configured provider without
installing the retrieval package.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx

ProviderOutcome = Literal[
    "ok",
    "empty",
    "rate_limited",
    "auth",
    "quota",
    "timeout",
    "upstream",
    "invalid_response",
    "partial_outage",
]


@dataclass(frozen=True)
class HttpAttemptPolicy:
    """Operation-wide bounds; attempts never get a fresh deadline."""

    deadline_s: float = 15.0
    max_attempts: int = 3
    backoff_s: float = 0.25
    max_backoff_s: float = 2.0
    max_retry_after_s: float = 2.0

    def __post_init__(self) -> None:
        if self.deadline_s <= 0:
            raise ValueError("deadline_s must be positive")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if self.backoff_s < 0 or self.max_backoff_s < 0 or self.max_retry_after_s < 0:
            raise ValueError("retry bounds must be non-negative")


@dataclass(frozen=True)
class HttpResult:
    """Raw response plus a bounded transport diagnostic."""

    response: httpx.Response | None
    diagnostic: dict[str, object]


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]


def _default_jitter(value: float) -> float:
    return random.uniform(0.0, value)


def _status_outcome(status_code: int) -> ProviderOutcome:
    if status_code == 429:
        return "rate_limited"
    if status_code in (401, 403):
        return "auth"
    if status_code in (402, 409, 413, 422, 432, 433):
        return "quota"
    return "upstream"


def _retry_after(value: str | None, cap: float) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            parsed = (target - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return min(parsed, cap)


def _request_outcome(status_code: int) -> ProviderOutcome:
    return "ok" if 200 <= status_code < 300 else _status_outcome(status_code)


async def _send_attempt(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    params: Mapping[str, object] | None,
    json: object | None,
    timeout: float,
) -> tuple[httpx.Response | None, ProviderOutcome, int | None, bool]:
    try:
        response = await asyncio.wait_for(
            client.request(
                method,
                url,
                headers=headers,
                params=cast(Any, params),
                json=json,
            ),
            timeout=timeout,
        )
        return response, _request_outcome(response.status_code), response.status_code, False
    except (TimeoutError, httpx.TimeoutException):
        return None, "timeout", None, True
    except httpx.TransportError:
        return None, "upstream", None, True
    except httpx.HTTPError:
        return None, "upstream", None, False


def _retryable(
    outcome: ProviderOutcome, status_code: int | None, transport_retryable: bool
) -> bool:
    return outcome in {"timeout", "rate_limited", "upstream"} and (
        outcome == "timeout" or transport_retryable or status_code in {408, 429, 500, 502, 503, 504}
    )


def _retry_delay(
    response: httpx.Response | None,
    attempts: int,
    policy: HttpAttemptPolicy,
    jitter: Jitter,
    remaining: float,
) -> float:
    retry_after = _retry_after(
        response.headers.get("Retry-After") if response is not None else None,
        policy.max_retry_after_s,
    )
    delay = (
        retry_after
        if retry_after is not None
        else min(policy.backoff_s * (2 ** (attempts - 1)), policy.max_backoff_s)
    )
    if retry_after is None:
        delay = jitter(delay)
    return max(0.0, min(delay, policy.max_backoff_s, remaining))


@dataclass
class BoundedHttpExecutor:
    """Execute one provider operation with a shared finite retry policy."""

    provider: str
    transport: httpx.AsyncBaseTransport | None = None
    policy: HttpAttemptPolicy = field(default_factory=HttpAttemptPolicy)
    concurrency: int = 8
    clock: Clock = time.monotonic
    sleep: Sleep = asyncio.sleep
    jitter: Jitter = _default_jitter
    _semaphore: asyncio.Semaphore | None = field(default=None, repr=False)
    _active: int = field(default=0, init=False, repr=False)
    _max_concurrency: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    async def request(
        self,
        method: str,
        url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json: object | None = None,
    ) -> HttpResult:
        """Make one bounded request; cancellation is intentionally propagated."""

        assert self._semaphore is not None
        started = self.clock()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self.policy.deadline_s)
        except TimeoutError:
            return HttpResult(
                response=None,
                diagnostic={
                    "provider": self.provider,
                    "outcome": "timeout",
                    "status_code": None,
                    "attempts": 0,
                    "latency_ms": max(0, int((self.clock() - started) * 1_000)),
                    "retry_wait_ms": 0,
                    "max_concurrency": self._max_concurrency,
                },
            )
        self._active += 1
        self._max_concurrency = max(self._max_concurrency, self._active)
        attempts = 0
        retry_wait_s = 0.0
        response: httpx.Response | None = None
        outcome: ProviderOutcome = "upstream"
        status_code: int | None = None
        try:
            async with httpx.AsyncClient(
                timeout=None,
                transport=transport if transport is not None else self.transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                deadline = started + self.policy.deadline_s
                while attempts < self.policy.max_attempts:
                    attempts += 1
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        outcome = "timeout"
                        break
                    response, outcome, status_code, transport_retryable = await _send_attempt(
                        client,
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json,
                        timeout=remaining,
                    )
                    if (
                        not _retryable(outcome, status_code, transport_retryable)
                        or attempts >= self.policy.max_attempts
                    ):
                        break
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        outcome = "timeout"
                        break
                    delay = _retry_delay(response, attempts, self.policy, self.jitter, remaining)
                    if delay <= 0:
                        continue
                    retry_wait_s += delay
                    await self.sleep(delay)
        finally:
            self._active -= 1
            self._semaphore.release()

        diagnostic: dict[str, object] = {
            "provider": self.provider,
            "outcome": outcome,
            "status_code": status_code,
            "attempts": attempts,
            "latency_ms": max(0, int((self.clock() - started) * 1_000)),
            "retry_wait_ms": int(retry_wait_s * 1_000),
            "max_concurrency": self._max_concurrency,
        }
        return HttpResult(response=response, diagnostic=diagnostic)


ProviderHttpExecutor = BoundedHttpExecutor


def with_outcome(diagnostic: Mapping[str, object], outcome: ProviderOutcome) -> dict[str, object]:
    result = dict(diagnostic)
    result["outcome"] = outcome
    return result


__all__ = [
    "BoundedHttpExecutor",
    "HttpAttemptPolicy",
    "HttpResult",
    "ProviderHttpExecutor",
    "ProviderOutcome",
    "with_outcome",
]
