"""Transient-retry backoff for the router's provider-call loop.

Without a wait, the eight same-model attempts fire back-to-back: a provider blip
becomes eight instant failures and a recovering server is given nothing to
recover in. The schedule is exponential from 2s, capped at 45s, with symmetric
jitter so concurrent research lanes do not resynchronize onto the same retry
instant.

Kept out of ``_router_execution`` so that module stays inside its size budget.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Mapping
from typing import Any

from .errors import LLMTransientError
from .stream_progress import reporter_for

_RETRY_BACKOFF_BASE_S = 2.0
_RETRY_BACKOFF_MAX_S = 45.0
_RETRY_BACKOFF_JITTER = 0.25


def retry_backoff_delay_s(
    attempt: int,
    *,
    base_s: float = _RETRY_BACKOFF_BASE_S,
    max_s: float = _RETRY_BACKOFF_MAX_S,
) -> float:
    """Seconds to wait after 1-based ``attempt`` failed, before the next one.

    ``min(base * 2**(attempt-1), max)`` with +/-25% jitter. A non-positive base
    disables the wait entirely, which is how the test suites stay fast without
    pretending the schedule does not exist.
    """
    if base_s <= 0:
        return 0.0
    capped = min(base_s * (2 ** max(0, attempt - 1)), max_s)
    return capped * (1.0 + random.uniform(-_RETRY_BACKOFF_JITTER, _RETRY_BACKOFF_JITTER))


async def sleep_before_retry(
    attempt: int,
    base_s: float,
    *,
    error: Exception | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Wait out the backoff for a retry that is ALREADY scheduled.

    Only ever called on the branch that has decided to retry, so no delay is
    ever charged after the final attempt. The cap is a constant, not a knob:
    only the base is ever overridden (to zero, to keep test suites fast).
    """
    delay = retry_backoff_delay_s(attempt, base_s=base_s)
    hint = error.retry_after_s if isinstance(error, LLMTransientError) else None
    if hint is not None and math.isfinite(hint):
        delay = max(delay, min(300.0, max(0.0, hint)))
    reporter = reporter_for(metadata)
    if reporter is not None:
        status = error.http_status if isinstance(error, LLMTransientError) else None
        await reporter.retrying(attempt, delay, status)
    if delay > 0:
        await asyncio.sleep(delay)
