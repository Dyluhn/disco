"""Error-path extraction for the streaming router loop.

The stream loop remains the owner of provider selection and attempt counters;
this helper only preserves the existing transient-error branches so the loop's
control flow stays small and easy to audit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Literal

from ._router_attempts import end_stream_attempt
from ._router_backoff import sleep_before_retry
from .types import CompletionRequest

_LOG = logging.getLogger(__name__)

Fallback = Callable[
    [Any, CompletionRequest, Any, bool, int],
    tuple[Any, str, str, str, list[str], int, bool] | None,
]
Path = Literal["local", "overflow", "pinned", "manual", "role_fallback"]
FailureRoute = tuple[Path, str, str, str]
Fail = Callable[[Any, CompletionRequest, Any, FailureRoute, Exception | None], None]


def handle_stream_terminal(
    router: Any,
    req: CompletionRequest,
    entry: Any,
    state: Any,
    exc: Exception,
    route: FailureRoute,
    fail: Fail,
) -> None:
    """Preserve terminal stream errors after recording and routing failure."""
    end_stream_attempt(
        state.attempt, recorded=state.recorded,
        outcome="error", error=exc, retry_scheduled=False,
    )
    fail(router, req, entry, route, exc)
    raise exc


async def handle_stream_auth(
    router: Any,
    req: CompletionRequest,
    entry: Any,
    state: Any,
    exc: Exception,
    auth_retry_used: bool,
    auth_retry_delay_s: float,
    route: FailureRoute,
    fail: Fail,
) -> bool:
    """Retry one pre-yield auth error, or preserve its terminal behavior."""
    if not state.yielded_any and not auth_retry_used:
        end_stream_attempt(
            state.attempt, recorded=state.recorded,
            outcome="error", error=exc, retry_scheduled=True,
        )
        _LOG.warning("transient auth failure, retrying once: %s", exc)
        await asyncio.sleep(auth_retry_delay_s)
        return True
    end_stream_attempt(
        state.attempt, recorded=state.recorded,
        outcome="error", error=exc, retry_scheduled=False,
    )
    fail(router, req, entry, route, exc)
    raise exc


async def handle_stream_transient(
    router: Any,
    req: CompletionRequest,
    entry: Any,
    state: Any,
    exc: Exception,
    attempt: int,
    max_attempts: int,
    used_fallback: bool,
    fallback_max_attempts: int,
    active_path: Path,
    active_reason: str,
    model_id: str,
    provider_name: str,
    backoff_base_s: float,
    try_fallback: Fallback,
    fail: Fail,
) -> tuple[Any, str, str, str, list[str], int, bool] | None:
    """Handle one pre-yield stream transient, or raise its terminal error."""
    route = (active_path, active_reason, model_id, provider_name)
    if state.yielded_any:
        end_stream_attempt(
            state.attempt, recorded=state.recorded,
            outcome="error", error=exc, retry_scheduled=False,
        )
        fail(router, req, entry, route, None)
        raise exc
    if attempt >= max_attempts:
        fallback = try_fallback(router, req, entry, used_fallback, fallback_max_attempts)
        if fallback is not None:
            end_stream_attempt(
                state.attempt, recorded=state.recorded,
                outcome="error", error=exc, retry_scheduled=True,
            )
            return fallback
        end_stream_attempt(
            state.attempt, recorded=state.recorded,
            outcome="error", retry_scheduled=False,
        )
        fail(router, req, entry, route, None)
        raise exc
    end_stream_attempt(
        state.attempt, recorded=state.recorded,
        outcome="error", error=exc, retry_scheduled=True,
    )
    await sleep_before_retry(attempt, backoff_base_s)
    return None


__all__ = ["handle_stream_auth", "handle_stream_terminal", "handle_stream_transient"]
