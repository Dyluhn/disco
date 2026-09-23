"""Progress observation for an open provider stream.

The adapter already bounds a call by PROGRESS rather than by wall clock
(``_openai_timeouts``): it knows, chunk by chunk, whether a model is still
producing. Nothing above it did. A caller waiting on ``router.complete`` saw a
single ``await`` that returned after four seconds or after four minutes with no
way to tell a slow model from a dead one — so a UI built on top of it could only
guess, and a guessed heartbeat is a spinner telling a lie.

This module is the seam that publishes what the stream layer sees. A caller
installs an observer for the duration of its work; every OpenAI-compatible
stream opened underneath reports to it, THROTTLED to one report per
``PROGRESS_INTERVAL_S`` plus one on close. Both provider entry points ride the
streamed transport, so a buffered ``complete`` reports exactly as a streamed one
does — except on a transport that has no chunks at all (the Responses API, and
the one-shot fallback for a server that refuses to stream), which reports once
at call start with ``streams=False``.

Three rules make the signal worth trusting:

* **Only observed boundaries produce a report.** Request start and router
  backoff are labelled waiting/backoff, with zero delivered output. There is no timer. A stream that
  has gone quiet emits nothing, and the silence is the caller's to interpret —
  which is the honest reading of a quiet stream, and the one the UI renders.
* **The count is deliveries, never an estimate.** ``tokens_streamed`` counts SSE
  deltas: one delta is one decode step on every server this adapter targets. A
  provider that batches deltas UNDERCOUNTS, and undercounting is the safe
  direction — a number here is always something that actually arrived.
* **Observation can never fail a live call.** An observer that raises is logged
  and dropped; the stream continues.

The observer is ambient (a ``ContextVar``) rather than a parameter because the
thing being observed is one open socket several layers below the caller, and the
alternative is threading a callback through the router, the retry loop, the
``ModelProvider`` protocol and every fake that implements it — changing four
public signatures to carry a passive signal. The same reasoning already put
``_ACTIVE_AGENT_VIEW_ID`` and the execution fence on ContextVars.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Literal

_LOG = logging.getLogger(__name__)

#: Floor between two heartbeats of ONE stream. A decode step arrives every few
#: dozen milliseconds; reporting per token would be per-token event spam, and
#: reporting less often than this leaves the UI unable to distinguish a working
#: stream from a stalled one inside its own patience window.
PROGRESS_INTERVAL_S = 5.0

#: Identity for one open stream, so an observer can tell a retry of the same
#: call from a continuation of it. Process-scoped and monotonic; callers that
#: need a run-scoped ordinal derive it from the order these are first seen.
_STREAM_IDS = count(1)


@dataclass(frozen=True)
class StreamProgress:
    """What one open provider stream has actually DELIVERED so far.

    ``seconds`` is measured from the moment the call was made — not from the
    first chunk. Prefill on a local model is part of how long the caller has
    been waiting, and a duration that started counting at the first token would
    report a four-minute call as a two-second one.
    """

    #: Process-unique id of the stream these numbers describe.
    stream_id: int
    #: The caller's own label for this call (``metadata["inspect_stage"]``),
    #: or None when the caller did not name one.
    inspect_stage: str | None
    #: Deltas delivered so far — content plus reasoning. See the module note on
    #: why this is a count of deliveries and never an estimate.
    tokens_streamed: int
    #: The reasoning-channel share of ``tokens_streamed``. 0 when the provider
    #: exposes no separate reasoning channel.
    reasoning_tokens: int
    #: Seconds since the call was made.
    seconds: float
    #: True on the first report of this stream (normally the request-start boundary).
    first: bool
    #: True on the last report of this stream.
    final: bool
    #: False when the transport delivers the whole answer in one buffered
    #: response, so this is the ONLY report the call will ever make. See
    #: :meth:`StreamProgressReporter.buffered_start`.
    streams: bool = True
    state: Literal["waiting", "generating", "backoff"] = "generating"
    details: dict[str, Any] = field(default_factory=dict)


ProgressObserver = Callable[[StreamProgress], Awaitable[None]]

_OBSERVER: ContextVar[ProgressObserver | None] = ContextVar(
    "disco_llm_stream_progress", default=None
)


@contextmanager
def observe_stream_progress(observer: ProgressObserver) -> Iterator[None]:
    """Report every stream opened inside this block to ``observer``.

    Scoped to the current context, so concurrent work in other tasks is
    unaffected and the observer is gone the moment the block exits.
    """
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def _inspect_stage(metadata: Mapping[str, Any] | None) -> str | None:
    """The caller's label for this call, bounded, or None."""
    raw = (metadata or {}).get("inspect_stage")
    return str(raw)[:128] if isinstance(raw, str) and raw.strip() else None


def reporter_for(metadata: Mapping[str, Any] | None) -> StreamProgressReporter | None:
    """A reporter for a stream about to open, or None when nobody is watching.

    Returning None rather than a null object keeps the un-observed path — every
    ordinary chat completion in the product — at one ContextVar read for the
    whole stream.
    """
    observer = _OBSERVER.get()
    if observer is None:
        return None
    return StreamProgressReporter(
        observer,
        inspect_stage=_inspect_stage(metadata),
        details={
            key: (metadata or {})[key]
            for key in ("source_id", "chunk", "chunks")
            if key in (metadata or {})
        },
    )


async def waiting_reporter_for(metadata: Mapping[str, Any] | None) -> StreamProgressReporter | None:
    reporter = reporter_for(metadata)
    if reporter is not None:
        await reporter.waiting()
    return reporter


class StreamProgressReporter:
    """Throttled reporting for ONE open stream."""

    def __init__(
        self,
        observer: ProgressObserver,
        *,
        inspect_stage: str | None = None,
        details: dict[str, Any] | None = None,
        interval_s: float = PROGRESS_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._observer = observer
        self._stage = inspect_stage
        self._details = details or {}
        self._state: Literal["waiting", "generating", "backoff"] = "generating"
        self._interval_s = interval_s
        self._clock = clock
        self._stream_id = next(_STREAM_IDS)
        self._started = clock()
        self._content = 0
        self._reasoning = 0
        self._last_report: float | None = None
        self._closed = False

    async def observed(self, content_deltas: int, reasoning_deltas: int) -> None:
        """Record deltas that just arrived, reporting at most once per interval.

        A call carrying no deltas is not progress and is dropped before any
        clock is read: an SSE frame that only re-states usage or a finish reason
        must never look like the model producing.
        """
        if content_deltas <= 0 and reasoning_deltas <= 0:
            return
        first_output = self._content + self._reasoning == 0
        self._state = "generating"
        self._content += max(0, content_deltas)
        self._reasoning += max(0, reasoning_deltas)
        now = self._clock()
        if (
            not first_output
            and self._last_report is not None
            and now - self._last_report < self._interval_s
        ):
            return
        await self._report(now, final=False)

    async def waiting(self) -> None:
        """A request was started; no model output has arrived yet."""
        self._state = "waiting"
        await self._report(self._clock(), final=False)

    async def retrying(self, attempt: int, delay: float, http_status: int | None) -> None:
        """The router scheduled a retry; this is not a generation heartbeat."""
        self._state = "backoff"
        self._details = {
            **self._details,
            "attempt": attempt,
            "retry_after_s": delay,
            "http_status": http_status,
        }
        await self._report(self._clock(), final=True)

    async def buffered_start(self) -> None:
        """Announce a call whose transport will report nothing until it ends.

        The rule above — only observed chunks produce a report — assumes a
        transport that has chunks. A buffered POST has none, so the caller
        waits out the whole call with no observation at all and the UI reads
        the silence as a stall. That reading is wrong, and it is wrong because
        of the transport rather than the model.

        So this reports exactly what IS observed at that moment: a call of this
        stage started, nothing has been delivered, and nothing will be until it
        returns (``streams=False``). It is the only report a buffered call
        makes; there is still no timer, and no number here is projected.
        """
        if self._last_report is not None or self._closed:
            return
        await self._report(self._clock(), final=False, streams=False)

    async def close(self) -> None:
        """Final report for this stream — only if it ever delivered anything.

        A stream that produced no chunk produced no evidence of activity, and
        announcing its close would be inventing the very signal this module
        exists to make honest.
        """
        if self._content + self._reasoning == 0 or self._closed:
            return
        self._closed = True
        await self._report(self._clock(), final=True)

    async def _report(self, now: float, *, final: bool, streams: bool = True) -> None:
        first = self._last_report is None
        # Stamped BEFORE the await so a slow observer cannot let a second
        # report through the interval gate behind it.
        self._last_report = now
        progress = StreamProgress(
            stream_id=self._stream_id,
            inspect_stage=self._stage,
            tokens_streamed=self._content + self._reasoning,
            reasoning_tokens=self._reasoning,
            seconds=max(0.0, now - self._started),
            first=first,
            final=final,
            streams=streams,
            state=self._state,
            details=dict(self._details),
        )
        try:
            await self._observer(progress)
        except Exception:  # noqa: BLE001 — passive observation cannot fail a live call
            _LOG.debug("stream-progress observer failed", exc_info=True)


__all__ = [
    "PROGRESS_INTERVAL_S",
    "ProgressObserver",
    "StreamProgress",
    "StreamProgressReporter",
    "observe_stream_progress",
    "reporter_for",
]
