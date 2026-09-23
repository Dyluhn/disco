"""Progress-based timeout policy for the OpenAI-compatible adapter.

Model-neutral by construction. Disco runs local/open-weight models whose decode
is 20-45 tok/s and whose thinking phases legitimately run for many minutes, so a
total wall-clock ceiling cannot tell a slow-but-working model from a hung
provider — it just punishes the models this product exists for. **A stall is the
absence of model progress, not the presence of duration.**

The adapter therefore bounds three things and never the whole call:

* ``connect_s`` — establishing the TCP/TLS connection (a real network fault).
* ``first_chunk_s`` — headers accepted but not one byte of generation yet.
  Prefill of a large evidence context on a local server at ~100 tok/s takes many
  minutes, so this is generous by default.
* ``idle_s`` — the gap BETWEEN streamed chunks. Decode emits chunks every few
  dozen milliseconds; three minutes of silence mid-stream is a stall.

``buffered_total_s`` is the only wall-clock ceiling left, and it applies solely
to the two buffered paths that cannot stream: the Responses-API endpoint and the
one-shot fallback taken when a server rejects the streamed request itself.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from ..env import disco_env
from .errors import LLMTransientError

_LOG = logging.getLogger("disco.llm.openai")

DEFAULT_CONNECT_S = 30.0
DEFAULT_FIRST_CHUNK_S = 900.0
DEFAULT_IDLE_S = 180.0
DEFAULT_BUFFERED_TOTAL_S = 600.0

_FIRST_CHUNK_ENV = "LLM_FIRST_TOKEN_TIMEOUT_S"
_IDLE_ENV = "LLM_IDLE_TIMEOUT_S"
_BUFFERED_TOTAL_ENV = "LLM_TIMEOUT_S"


def _env_seconds(suffix: str, default: float) -> float:
    """Read a positive-float seconds env var, falling back on anything unusable.

    A misconfigured timeout must not become a new failure mode: an unparseable
    or non-positive value logs once and keeps the default.
    """
    raw = disco_env(suffix)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        _LOG.warning("DISCO_%s is not a number; using %.0fs", suffix, default)
        return default
    if not math.isfinite(value) or value <= 0:
        _LOG.warning("DISCO_%s must be > 0; using %.0fs", suffix, default)
        return default
    return value


@dataclass(frozen=True)
class ProviderTimeouts:
    """Resolved progress-based timeout budget for one provider instance."""

    connect_s: float = DEFAULT_CONNECT_S
    first_chunk_s: float = DEFAULT_FIRST_CHUNK_S
    idle_s: float = DEFAULT_IDLE_S
    buffered_total_s: float = DEFAULT_BUFFERED_TOTAL_S

    def stream_httpx_timeout(self) -> httpx.Timeout:
        """Transport-level backstop for a streamed call — no total ceiling.

        ``read`` is the widest of the two progress budgets so httpx never fires
        before the precise first-chunk/idle enforcement in
        :func:`iter_with_progress_timeout` does.
        """
        return httpx.Timeout(
            connect=self.connect_s,
            read=max(self.first_chunk_s, self.idle_s),
            write=self.connect_s,
            pool=self.connect_s,
        )

    def buffered_httpx_timeout(self) -> httpx.Timeout:
        """Timeout for a buffered POST, whose read spans the whole generation."""
        return httpx.Timeout(
            connect=self.connect_s,
            read=self.buffered_total_s,
            write=self.connect_s,
            pool=self.connect_s,
        )


def resolve_timeouts(buffered_total_s: float | None = None) -> ProviderTimeouts:
    """Resolve the timeout budget from the environment, once per provider.

    ``buffered_total_s`` is the constructor override; when it is ``None`` the
    value comes from ``DISCO_LLM_TIMEOUT_S`` (or its ``PMX_`` legacy name).
    """
    total = (
        buffered_total_s
        if buffered_total_s is not None and buffered_total_s > 0
        else _env_seconds(_BUFFERED_TOTAL_ENV, DEFAULT_BUFFERED_TOTAL_S)
    )
    return ProviderTimeouts(
        connect_s=DEFAULT_CONNECT_S,
        first_chunk_s=_env_seconds(_FIRST_CHUNK_ENV, DEFAULT_FIRST_CHUNK_S),
        idle_s=_env_seconds(_IDLE_ENV, DEFAULT_IDLE_S),
        buffered_total_s=total,
    )


async def iter_with_progress_timeout(
    source: AsyncIterator[str],
    *,
    provider_name: str,
    first_chunk_s: float,
    idle_s: float,
    is_progress: Callable[[str], bool] | None = None,
) -> AsyncIterator[str]:
    """Yield from ``source``, bounding first meaningful output and meaningful-output idle.

    The two budgets are deliberately distinct: waiting for prefill to finish is
    not the same event as a stream going quiet halfway through decode. Either
    exhaustion raises :class:`LLMTransientError` — under progress-based
    semantics a timeout now genuinely means "no progress", so the router's
    retry of it is correct rather than harmful.
    """
    saw_first = False
    clock = asyncio.get_running_loop().time
    deadline = clock() + first_chunk_s
    while True:
        budget = max(0.0, deadline - clock())
        try:
            item = await asyncio.wait_for(anext(source), timeout=budget)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            phase = (
                f"stream idle for {idle_s:.0f}s"
                if saw_first
                else f"no first chunk within {first_chunk_s:.0f}s"
            )
            raise LLMTransientError(f"request stalled ({phase})", provider=provider_name) from exc
        if is_progress is None or is_progress(item):
            saw_first = True
            deadline = clock() + idle_s
        yield item
