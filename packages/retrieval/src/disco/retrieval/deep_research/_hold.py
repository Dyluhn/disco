"""The hold: the loop waits out a dead search pool instead of spending turns in it.

When every search engine the run has seen is inside its rate-limit cooldown,
issuing another query is guaranteed-useless load that extends the block, and the
old loop did it anyway — turn after turn, until the turn budget was gone and the
run died with "research exhausted its turn budget without usable evidence". 44
of 152 recorded torture runs ended exactly there. The turn budget bought nothing
and the user got an error instead of a report.

A dead pool is not a failure condition. It is a WAIT. The loop holds: it emits
``hold`` naming which engines are cut off and until when, sleeps until the
earliest of those windows expires, and looks again. Holds consume no research
turn (the model is not thinking; nothing is being spent), there is no wall-clock
cap on the total wait (wall clock is not this product's budget currency), and
Stop is polled throughout so the user's own choice lands immediately as the
existing resumable checkpoint with every gathered source retained.

**Re-probing is real work, not a synthetic ping.** When the registry says the
engines are live the loop's very next act is a real search — the re-issue queue
drains first, and failing that the model's next plan goes out. If the pool is
still dead that search re-cools the engines and the next iteration holds again
with the updated resume time. Nothing here spends a token to ask a question the
next real query answers for free.

WHAT IS NOT A HOLD
------------------
Only the rate-limit/cooldown class waits, because only it is fixed by waiting. A
rejected credential (``auth_rejected``) never enters the cooldown registry by
design, and an exhausted plan is not an engine — both stay loud errors that name
what a human has to go change. A provider that reports its rate limit as a
transport error rather than as a named engine also never reaches the registry;
it degrades loudly instead of holding, because a wait this module cannot put a
clock on is a wait it must not pretend to know.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .._transport_retry import (
    degradation_markers,
    engine_cooldown_seconds,
    marker_engine,
    search_slot_available,
)
from ..models import SearchHit
from ._progress_events import EmitFn, HoldReason, emit_hold, emit_hold_resumed

#: How long the loop waits before looking again when nothing names a deadline,
#: and the window the outbound bucket must stay empty for before starvation
#: counts as a dead pool rather than as ordinary pacing.
PROBE_INTERVAL_S = 15.0

#: How often Stop is checked while holding. Short enough that pressing Stop
#: feels immediate; long enough that a multi-minute cooldown costs nothing.
CANCEL_POLL_S = 0.5

# Test seams — replaced by hermetic tests, never by production code. They pair
# with ``_transport_retry._now`` so a test can drive a whole cooldown expiry
# without sleeping.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_now: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class HoldOutcome:
    """What one hold did: whether it held at all, how long, and who ended it.

    ``held`` is separate from ``waited_s`` on purpose: a Stop pressed during the
    first poll ends a hold that really happened and really told the user why, and
    an audit that inferred "a hold happened" from elapsed time would lose it.
    """

    held: bool
    stopped: bool
    waited_s: float
    engines_live: tuple[str, ...]


class StarvationClock:
    """How long the shared outbound bucket has had nothing to give.

    One sample per loop iteration. A bucket that is momentarily empty is
    ordinary pacing; a bucket that has been empty across a whole probe interval
    while no engine is healthy is a pool nothing can be issued into.
    """

    def __init__(self) -> None:
        self._since: float | None = None

    def sample(self, *, available: bool, now: float) -> float:
        """Record one observation; return the seconds starved so far."""
        if available:
            self._since = None
            return 0.0
        if self._since is None:
            self._since = now
        return now - self._since


def _hit_engines(hits: Iterable[SearchHit]) -> set[str]:
    """Engine names that actually served this run, split out of merged labels."""
    found: set[str] = set()
    for hit in hits:
        for name in str(hit.source_engine or "").split("+"):
            cleaned = name.strip().casefold()
            if cleaned:
                found.add(cleaned)
    return found


def _trail_engines(trail: Sequence[Mapping[str, Any]]) -> set[str]:
    """Engine names this run's own search diagnostics have named."""
    found: set[str] = set()
    for entry in trail:
        trace = entry.get("retrieval_trace")
        if not isinstance(trace, Mapping):
            continue
        engines, _errors = degradation_markers(trace.get("provider_diagnostics"))
        found.update(filter(None, (marker_engine(marker) for marker in engines)))
    return found


def observed_engines(
    trail: Sequence[Mapping[str, Any]], hits: Iterable[SearchHit]
) -> frozenset[str]:
    """Every engine this run has evidence its search provider can use.

    There is no provider API that enumerates engines, and inventing a static
    list would be a second source of truth that drifts. What the run has SEEN —
    engines that returned a hit, plus engines its diagnostics named as
    unresponsive — is the honest universe, and it is exactly the set whose
    exhaustion means "nothing left to ask".
    """
    return frozenset(_hit_engines(hits) | _trail_engines(trail))


def hold_reason(
    observed: frozenset[str], cooling: Mapping[str, float], starved_for: float
) -> HoldReason | None:
    """Why the pool is dead, or None while any part of it is alive.

    Nothing cooling means there is no rate-limit outage to wait out, whatever
    else may be wrong — a plain provider failure is a loud error, not a wait.
    """
    if not cooling:
        return None
    if set(observed) - set(cooling):
        return None
    if observed:
        return "search_pool_cooling"
    return "search_rate_starved" if starved_for >= PROBE_INTERVAL_S else None


async def _wait_watching_stop(seconds: float, should_cancel: Callable[[], bool] | None) -> bool:
    """Sleep in short slices; True as soon as the user pressed Stop."""
    deadline = _now() + seconds
    while True:
        if should_cancel is not None and should_cancel():
            return True
        remaining = deadline - _now()
        if remaining <= 0.0:
            return False
        await _sleep(min(CANCEL_POLL_S, remaining))


async def hold_for_dead_pool(
    *,
    observed: frozenset[str],
    sources_retained: int,
    position: tuple[int, int],
    queued_queries: Sequence[str],
    starvation: StarvationClock,
    emit: EmitFn,
    should_cancel: Callable[[], bool] | None,
) -> HoldOutcome:
    """Hold while the search pool is dead. Returns immediately when it is not.

    Each pass re-reads the live cooldown registry, so a hold that outlasts one
    engine's window resumes the moment ANY engine comes back, and a pool that
    goes back down after a real query re-enters the hold with a fresh deadline.
    """
    started = _now()
    held = False
    while True:
        cooling = engine_cooldown_seconds()
        starved_for = starvation.sample(available=search_slot_available(), now=_now())
        reason = hold_reason(observed, cooling, starved_for)
        if reason is None:
            break
        resume_in = max(CANCEL_POLL_S, min(cooling.values()) if cooling else PROBE_INTERVAL_S)
        await emit_hold(
            emit,
            reason=reason,
            cooling=cooling,
            resume_in_s=resume_in,
            sources_retained=sources_retained,
            position=position,
            queued_queries=queued_queries,
        )
        held = True
        if await _wait_watching_stop(resume_in, should_cancel):
            return HoldOutcome(held=True, stopped=True, waited_s=_now() - started, engines_live=())
    if not held:
        return HoldOutcome(held=False, stopped=False, waited_s=0.0, engines_live=())
    live = tuple(sorted(set(observed) - set(engine_cooldown_seconds())))
    waited = _now() - started
    await emit_hold_resumed(emit, waited_s=waited, engines_live=live)
    return HoldOutcome(held=True, stopped=False, waited_s=waited, engines_live=live)


__all__ = [
    "CANCEL_POLL_S",
    "PROBE_INTERVAL_S",
    "HoldOutcome",
    "StarvationClock",
    "hold_for_dead_pool",
    "hold_reason",
    "observed_engines",
]
