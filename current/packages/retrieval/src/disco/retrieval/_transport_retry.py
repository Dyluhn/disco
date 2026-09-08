"""Below-the-model retries for degraded retrieval transport (search + extraction).

Provider and transport trouble is SYSTEM mechanics, never research signal. The
agentic loop entraps the model in a deterministic system; that system must never
hand the model an infrastructure failure dressed up as a semantic result, and
must never ask the model to work around one. This module holds the two bounded
retry loops that keep such failures BELOW the model:

* **search** — a zero-hit response that names unresponsive engines, or that
  carries a transport/provider error, is a provider outage, not a query miss.
  The SAME query is re-issued up to ``SEARCH_ATTEMPTS`` times with short jittered
  backoff; the first non-degraded attempt wins (real hits OR a clean zero). Only
  after exhaustion does the outage surface — classified as ``PROVIDER_DEGRADED``,
  never disguised as ``no_hits``.
* **search, when the marker is a BAN** — a degradation whose marker text says the
  provider cut us off ("too many requests", "suspended", "access denied",
  "CAPTCHA", quota, "rate limit") is not transient and is never retried. An
  immediate re-issue into a rate limit is guaranteed-useless load that extends
  the block; a live run was measured re-suspending a just-recovered engine that
  way within minutes. Such a response returns degraded on its FIRST attempt,
  carrying ``search_outcome: "rate_limited"``, and the engine the marker names
  goes into a process-local cooldown so its recurrence cannot buy retries for
  the window either. Queries are never blocked — the provider still reaches its
  healthy engines; only RETRYING into a pool known to be cooling is refused.
* **search, when the marker is a REJECTED CREDENTIAL** — a paid provider that
  answers 401/403 did not fail and did not refuse us for load: it refused our
  KEY. No retry can mint a working key, so the outage is reported after one
  attempt carrying ``search_outcome: "auth_rejected"``. It starts no cooldown
  either — a cooldown is a memory about an ENGINE, and the engine is fine; the
  moment the key is corrected the very next query must go straight out.
* **extraction** — a request-level batch failure is split into per-URL requests
  (bounded concurrency) so one bad URL can only fail itself, and a per-URL
  failure whose class is genuinely transient (timeout / upstream 5xx) gets ONE
  bounded retry. Anti-bot, paywall, and not-found are terminal: retrying those
  cannot help and only burns the run's deadline.

Above both loops sits one process-wide token bucket. Every outbound search —
first issue and retry alike, across every concurrent run in the process — takes
a token before it goes out, so three runs at three queries a turn cannot arrive
as a burst. Acquisition only ever waits; it never fails a search, and the wait
it cost is recorded in the diagnostic as ``rate_wait_ms``.

That bucket also LISTENS to the cooldown registry. Every rate-limit signal that
cools an engine halves its refill rate (floored, so a run with healthy engines
left still makes progress); once nothing is cooling the rate climbs back toward
the configured one gradually, a step per search. A run that keeps issuing at the
configured rate while its engines are being cut off one by one is how a whole
batch ended up with every engine suspended, and the pace it was actually held at
rides in the diagnostic as ``search_rate_per_min``.

Retries are invisible to the model while they run. The only record they leave is
the attempt log written into the provider diagnostic (for the harness, the
persisted trace, and the UI) and — after exhaustion — the honest classification
the research agent turns into specific, radiant feedback. The cooldown set and
the pacing waits ride in that same diagnostic (``engines_cooling``,
``rate_wait_ms``, ``search_rate_per_min``): infrastructure state belongs to the
trace, not to the prompt.

The module-level hooks ``_sleep``, ``_jitter``, ``_rate_sleep`` and ``_now``
exist so hermetic tests can drive backoff, pacing and cooldown expiry without
real sleeping and without a real clock.
"""

from __future__ import annotations

import asyncio
import random
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from disco.core.env import disco_env
from disco.core.host_egress import EgressDenied, validate_untrusted_url

from .models import ExtractedDoc, SearchHit

# --- knobs -------------------------------------------------------------------

# Total attempts (the first issue + 2 retries) for one degraded search.
SEARCH_ATTEMPTS = 3
# Backoff before retry N. Short enough to stay inside a research turn, long
# enough for a flapping upstream engine to come back.
_SEARCH_BACKOFF_S: tuple[float, ...] = (1.5, 4.0)
# +/- fraction applied to each backoff so concurrent reports in one wave do not
# re-issue in lockstep and re-synchronise the outage.
_SEARCH_JITTER = 0.25

# Per-URL requests opened at once when a batch extraction request is split.
EXTRACTION_SPLIT_CONCURRENCY = 3
# One short pause before the single retry of a transient per-URL failure.
_EXTRACTION_RETRY_BACKOFF_S = 0.75

# The yield_reason for a search whose infrastructure was degraded and stayed
# degraded across every attempt. Distinct from "no_hits" (a clean zero).
PROVIDER_DEGRADED = "provider_degraded"

# Diagnostic keys written by the search retry driver.
ATTEMPTS_KEY = "search_attempts"
OUTCOMES_KEY = "search_attempt_outcomes"
OUTCOME_KEY = "search_outcome"
# ...and by the pacing/cooldown machinery, for the trace only.
RATE_WAIT_KEY = "rate_wait_ms"
COOLING_KEY = "engines_cooling"
RATE_KEY = "search_rate_per_min"

# The outcome classes one search can end in. "degraded", "rate_limited" and
# "auth_rejected" are all exhausted outages downstream; they differ only in
# whether retrying could ever have helped, and in what a human has to go fix.
OUTCOME_DEGRADED = "degraded"
OUTCOME_RECOVERED = "recovered"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_AUTH_REJECTED = "auth_rejected"
_OUTCOME_OK = "ok"
_EXHAUSTED_OUTCOMES = frozenset(
    {OUTCOME_DEGRADED, OUTCOME_RATE_LIMITED, OUTCOME_AUTH_REJECTED}
)

# Marker text with which a provider says it CUT US OFF rather than failed. Frozen
# and deliberately small: matched case-insensitively as a substring of the same
# bounded marker text the diagnostic already carries. Every one of these was
# observed from a real engine ("Suspended: too many requests", "access denied",
# a CAPTCHA wall, an exhausted daily quota).
_RATE_LIMIT_MARKERS = (
    "too many requests",
    "suspended",
    "access denied",
    "captcha",
    "quota",
    "rate limit",
)

# Marker text with which a provider says it rejected OUR CREDENTIAL. Deliberately
# one exact phrase, written only by the adapters themselves (a paid provider's
# 401/403), so no unbounded upstream prose can wander into this class. It is kept
# disjoint from the ban markers above: a refused key and a refused rate are both
# unretryable, but only one of them is fixed by waiting.
_AUTH_REJECTED_MARKERS = ("auth rejected",)

# How long one named engine stays known-cut-off after a rate-limit marker.
_COOLDOWN_S = 300.0
# Process-wide outbound search pacing. 24/min with a burst of 6 lets a single
# turn's queries go out at once while three concurrent runs still average out.
_RATE_PER_MIN = 24.0
_RATE_BURST = 6.0
# How far a rate-limit signal may drive that rate down. Below this the pool is
# effectively closed, and a run with engines still healthy has to keep moving.
_RATE_FLOOR_PER_MIN = 2.0
# ...and how fast it climbs back, per search, once nothing is cooling. Gradual
# on purpose: snapping straight back to the configured rate is what re-suspended
# a just-recovered engine on the live run.
_RATE_RECOVERY = 1.25

# Diagnostic keys a provider uses to NAME its own degradation.
_DEGRADED_ENGINE_KEYS = ("unresponsive_engines", "failed_engines", "suspended_engines")
_DEGRADED_ERROR_KEY = "provider_error"
_MARKER_CHARS = 80

# httpx splits transport/status failures across HTTPError; a malformed JSON body
# raises ValueError. Both are request-level failures for our purposes.
_TRANSPORT_ERRORS = (httpx.HTTPError, ValueError)

# Test seams — replaced by hermetic tests, never by production code. Pacing gets
# its own sleep so a test asserting on retry backoff cannot see a pacing wait.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_jitter: Callable[[], float] = random.random
_rate_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_now: Callable[[], float] = time.monotonic


SearchAttempt = Callable[[], Awaitable[tuple[list[SearchHit], dict[str, object]]]]
ExtractFetch = Callable[[list[str]], Awaitable[dict[str, ExtractedDoc]]]
FailedDoc = Callable[[str, str], ExtractedDoc]


# --- search: degradation classification --------------------------------------


@dataclass(frozen=True)
class SearchDegradation:
    """What was wrong with the search infrastructure — never with the query.

    ``rate_limited`` splits the one class that a retry cannot help: the provider
    did not fail, it refused us. ``auth_rejected`` splits the other: the provider
    refused our credential. Everything downstream still treats both as an outage
    (the query was never semantically tested); only the retry driver and the one
    model-facing clause care about the difference.
    """

    engines: tuple[str, ...]
    errors: tuple[str, ...]
    attempts: int
    rate_limited: bool = False
    auth_rejected: bool = False

    def describe(self) -> str:
        """A bounded, specific clause naming what failed."""
        parts: list[str] = []
        if self.engines:
            parts.append("unresponsive search engines: " + ", ".join(self.engines))
        if self.errors:
            parts.append("transport/provider errors: " + ", ".join(self.errors))
        return "; ".join(parts) or "the search provider returned no usable response"


def _diagnostic_nodes(value: object) -> Iterator[Mapping[str, Any]]:
    """Walk a provider diagnostic, which may nest per-provider sub-diagnostics."""
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _diagnostic_nodes(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _diagnostic_nodes(item)


def _marker(value: object) -> str:
    return " ".join(str(value or "").split())[:_MARKER_CHARS]


def degradation_markers(diagnostic: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull the named unresponsive engines and transport errors out of a diagnostic."""
    engines: list[str] = []
    errors: list[str] = []
    for node in _diagnostic_nodes(diagnostic):
        for key in _DEGRADED_ENGINE_KEYS:
            raw = node.get(key)
            if isinstance(raw, (list, tuple)):
                engines.extend(marker for marker in map(_marker, raw) if marker)
        error = _marker(node.get(_DEGRADED_ERROR_KEY))
        if error:
            errors.append(error)
    return tuple(dict.fromkeys(engines)), tuple(dict.fromkeys(errors))


# --- search: rate limits, per-engine cooldown, outbound pacing ---------------

# engine name -> monotonic deadline. Process-local on purpose: it is a memory of
# what THIS process was told, not a shared fact about the engine.
_cooldowns: dict[str, float] = {}
# Held only across dict work, never across an await — safe for threads and loops.
_cooldown_guard = threading.Lock()


def _env_number(suffix: str, default: float) -> float:
    """A numeric env knob, read through `disco_env`; junk or negative → default."""
    raw = disco_env(suffix)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0.0 else default


def _names_rate_limit(marker: str) -> bool:
    text = marker.casefold()
    return any(pattern in text for pattern in _RATE_LIMIT_MARKERS)


def marker_engine(marker: str) -> str:
    """The engine NAME carried by one degradation marker.

    SearXNG names an unresponsive engine as an ``[engine, reason]`` pair, which
    reaches this layer already stringified; other providers send a bare name or
    a ``name: reason`` line. All three put the name first, so take the head of
    whichever separator appears first and strip the quoting the repr left behind.
    """
    head = re.split(r"[,:]", marker.strip().strip("[]()").strip(), maxsplit=1)[0]
    return head.strip().strip("'\"").strip().casefold()


def note_rate_limited_engines(markers: Iterable[str]) -> tuple[str, ...]:
    """Start a cooldown for every engine whose own marker names a rate limit.

    Only the marker that carries the ban cools: a sibling engine that merely
    timed out in the same response is still worth retrying later.

    Cooling an engine also SLOWS the shared bucket. The engines have just named
    our pace as the problem, and the rate we ask again at is the only lever this
    process holds over that; leaving the bucket at its configured rate is how a
    batch kept issuing while its pool was being cut off one engine at a time.

    Only a NEWLY cooled engine slows it. An engine already inside its window is
    named again by every response until the window ends — a metasearch pool
    repeats "suspended" for the whole hour — and halving on each repeat drove a
    batch to the floor over one weak engine while the rest of the pool was
    answering (2026-09-04, pubmed).
    """
    deadline = _now() + _env_number("SEARCH_ENGINE_COOLDOWN_S", _COOLDOWN_S)
    now = _now()
    cooled: list[str] = []
    newly_cooled = False
    with _cooldown_guard:
        for marker in markers:
            engine = marker_engine(marker) if _names_rate_limit(marker) else ""
            if not engine:
                continue
            if _cooldowns.get(engine, 0.0) <= now:
                newly_cooled = True
            _cooldowns[engine] = max(_cooldowns.get(engine, 0.0), deadline)
            cooled.append(engine)
    if newly_cooled:
        _search_limiter().slow()
    return tuple(dict.fromkeys(cooled))


def engines_cooling() -> tuple[str, ...]:
    """Engines known to have cut us off and still inside their cooldown window."""
    return tuple(engine_cooldown_seconds())


def engine_cooldown_seconds() -> dict[str, float]:
    """Every cooling engine mapped to the seconds left on its window.

    ``engines_cooling`` answers "is this engine cut off?". A hold also has to
    answer "for how long", because a wait the product cannot name is a wait the
    user cannot judge. Expired entries are dropped here exactly as they were
    before, so the two views can never disagree about who is cooling.
    """
    now = _now()
    with _cooldown_guard:
        for engine in [name for name, until in _cooldowns.items() if until <= now]:
            del _cooldowns[engine]
        return {
            name: max(0.0, until - now) for name, until in sorted(_cooldowns.items())
        }


def _is_rate_limited(engines: tuple[str, ...], errors: tuple[str, ...]) -> bool:
    """Whether this degradation is a refusal rather than a failure.

    Either the marker text says so outright, or every engine it names is one
    this process already watched get cut off — a recurrence of a known ban buys
    no retries, however the provider happens to word it the second time.
    """
    if any(_names_rate_limit(marker) for marker in (*engines, *errors)):
        return True
    if errors or not engines:
        return False
    cooling = engines_cooling()
    return all(marker_engine(marker) in cooling for marker in engines)


def _is_auth_rejected(engines: tuple[str, ...], errors: tuple[str, ...]) -> bool:
    """Whether this degradation is a refused CREDENTIAL rather than a failure.

    Only the exact adapter-written phrase counts; there is no cooldown-derived
    second route into this class, because a key is not an engine.
    """
    return any(
        pattern in marker.casefold()
        for marker in (*engines, *errors)
        for pattern in _AUTH_REJECTED_MARKERS
    )


class _SearchRateLimiter:
    """One process-wide token bucket for SearXNG-bound searches.

    A monotonic clock, a float token count and an asyncio lock — nothing else.
    Tokens are allowed to go negative inside the critical section: that RESERVES
    the caller's slot before it sleeps, so waiters leave the lock in arrival
    order with strictly increasing deadlines and none can be starved by a later
    arrival. Acquisition can only ever cost time; it never fails a search.

    The refill rate is not fixed: it halves on every rate-limit signal and
    climbs back once nothing is cooling. The rate is kept in per-MINUTE units
    (the unit it is configured and reported in) so halving and recovery land on
    exact values instead of drifting through a per-second conversion.
    """

    def __init__(self, per_minute: float, burst: float) -> None:
        self._configured_per_minute = per_minute
        self._per_minute = per_minute
        self._burst = max(1.0, burst)
        self._tokens = self._burst
        self._updated = _now()
        self._lock = asyncio.Lock()

    @property
    def _per_second(self) -> float:
        return self._per_minute / 60.0

    async def acquire(self) -> float:
        """Wait until this search may go out; return the seconds it waited."""
        if self._per_second <= 0.0:  # rate 0 disables pacing entirely
            return 0.0
        async with self._lock:
            now = _now()
            refill = (now - self._updated) * self._per_second
            self._tokens = min(self._burst, self._tokens + refill) - 1.0
            self._updated = now
            wait = 0.0 if self._tokens >= 0.0 else -self._tokens / self._per_second
            self._recover()
        if wait > 0.0:
            await _rate_sleep(wait)
        return wait

    def slow(self) -> None:
        """Halve the refill rate — the engines said we are asking too fast.

        Floored rather than closed: a pool that still has healthy engines has to
        keep making progress, just slowly. A bucket configured at rate 0 is not
        paced at all and stays that way, because slowing a disabled bucket would
        silently switch pacing on.
        """
        if self._configured_per_minute <= 0.0:
            return
        floor = min(_RATE_FLOOR_PER_MIN, self._configured_per_minute)
        self._per_minute = max(floor, self._per_minute / 2.0)

    def _recover(self) -> None:
        """Climb one step back toward the configured rate.

        Only while nothing is cooling: an engine still inside its window is the
        engine that asked for the slowdown, so a hold lasts at least as long as
        the window does. Held under the lock, alongside the token arithmetic the
        new rate applies to.
        """
        if self._per_minute >= self._configured_per_minute or engines_cooling():
            return
        self._per_minute = min(
            self._configured_per_minute, self._per_minute * _RATE_RECOVERY
        )

    def held_per_minute(self) -> float | None:
        """The pace being enforced, or None when the bucket runs as configured.

        Only a HELD pace is worth a trace line; at the configured rate it would
        just copy the configuration onto every clean search.
        """
        if self._per_minute >= self._configured_per_minute:
            return None
        return round(self._per_minute, 2)

    def has_token(self) -> bool:
        """Whether a search could go out right now. A PROBE — it takes nothing.

        Deliberately outside the lock: reserving a slot to answer a question
        about the bucket would change the answer. A racing acquisition can make
        this stale by one token, which is exactly the precision the caller
        (the hold's starvation test, sampled over a whole probe interval)
        needs.
        """
        if self._per_second <= 0.0:
            return True
        refill = (_now() - self._updated) * self._per_second
        return min(self._burst, self._tokens + refill) >= 1.0


_limiter: _SearchRateLimiter | None = None


def _search_limiter() -> _SearchRateLimiter:
    """The one bucket every search in this process shares."""
    global _limiter
    if _limiter is None:
        _limiter = _SearchRateLimiter(
            _env_number("SEARCH_RATE_PER_MIN", _RATE_PER_MIN),
            _env_number("SEARCH_BURST", _RATE_BURST),
        )
    return _limiter


async def acquire_search_slot() -> float:
    """Take this search's token from the shared bucket; return the seconds waited.

    Every outbound search goes through here, including the ones that never reach
    the degradation retry driver (a provider with no ``search_detailed``), so the
    pacing is a property of the retrieval layer rather than of one code path.
    """
    return await _search_limiter().acquire()


def search_slot_available() -> bool:
    """Whether an outbound search could take a token right now, taking none.

    A bucket that has been empty for a while, with no engine left healthy, is
    one of the two ways the search pool is dead; the hold needs to be able to
    ask without spending a slot to find out. An unbuilt bucket has never paced
    anything, so it is available by definition.
    """
    limiter = _limiter
    return True if limiter is None else limiter.has_token()


def held_search_rate_per_min() -> float | None:
    """The pace the shared bucket is being held at, or None if it runs full.

    An unbuilt bucket has never been slowed, so it is running full by
    definition.
    """
    limiter = _limiter
    return None if limiter is None else limiter.held_per_minute()


def reset_search_pacing() -> None:
    """Drop the shared bucket and the cooldown registry.

    Both are process-global state; a test (or a reconfiguration) needs a way to
    start from a known-empty one. The next search rebuilds the bucket from the
    environment as it stands then — at its configured rate, since a slowdown is
    a memory of what the engines said and that memory goes with the registry.
    """
    global _limiter
    _limiter = None
    with _cooldown_guard:
        _cooldowns.clear()


# --- search: the retry driver ------------------------------------------------


def classify_search_response(
    hit_count: int, diagnostic: object
) -> SearchDegradation | None:
    """Degraded == zero hits AND the provider named an outage. A clean zero (no
    degradation markers) is a real, valid empty result and stays one."""
    if hit_count > 0:
        return None
    engines, errors = degradation_markers(diagnostic)
    if not engines and not errors:
        return None
    return SearchDegradation(
        engines=engines,
        errors=errors,
        attempts=1,
        rate_limited=_is_rate_limited(engines, errors),
        auth_rejected=_is_auth_rejected(engines, errors),
    )


def _backoff_delay(attempt: int) -> float:
    base = _SEARCH_BACKOFF_S[min(attempt, len(_SEARCH_BACKOFF_S) - 1)]
    return max(0.0, base * (1.0 + _SEARCH_JITTER * (2.0 * _jitter() - 1.0)))


def _attempt_outcome(degradation: SearchDegradation | None) -> str:
    if degradation is None:
        return _OUTCOME_OK
    if degradation.auth_rejected:
        return OUTCOME_AUTH_REJECTED
    return OUTCOME_RATE_LIMITED if degradation.rate_limited else OUTCOME_DEGRADED


def _search_record(
    diagnostic: dict[str, object],
    outcomes: list[str],
    degradation: SearchDegradation | None,
    waited: float,
) -> dict[str, object]:
    """The provider diagnostic plus whatever the driver actually did to it.

    An untouched clean first attempt — no retry, no pacing wait, no engine
    cooling, no slowed bucket — is returned exactly as the provider sent it.
    """
    cooling = engines_cooling()
    wait_ms = int(waited * 1_000)
    held_rate = held_search_rate_per_min()
    retried = len(outcomes) > 1 or degradation is not None
    if not (retried or wait_ms or cooling or held_rate is not None):
        return diagnostic
    record = dict(diagnostic)
    if retried:
        record[ATTEMPTS_KEY] = len(outcomes)
        record[OUTCOMES_KEY] = outcomes
        record[OUTCOME_KEY] = (
            OUTCOME_RECOVERED if degradation is None else _attempt_outcome(degradation)
        )
    if wait_ms:
        record[RATE_WAIT_KEY] = wait_ms
    if held_rate is not None:
        record[RATE_KEY] = held_rate
    if cooling:
        record[COOLING_KEY] = list(cooling)
    return record


async def search_with_degradation_retry(
    attempt: SearchAttempt,
) -> tuple[list[SearchHit], dict[str, object]]:
    """Re-issue one query while its search infrastructure is degraded.

    The first non-degraded attempt wins — real hits OR a clean zero. When every
    attempt stays degraded the last response is returned with the attempt record
    attached, so the caller can classify it as ``PROVIDER_DEGRADED`` instead of a
    semantic miss. A clean first attempt is returned completely untouched.

    A degradation that names a RATE LIMIT ends the loop on the spot. Retrying it
    is not a bounded recovery, it is extra load on a provider that already said
    no — so the outage is reported after one attempt, and the engines it named
    start cooling so the next response naming them cannot restart retries.
    A REJECTED CREDENTIAL ends it the same way and for the same reason, minus the
    cooldown: nothing about the engine is wrong, so nothing about the engine may
    be remembered.
    Every attempt, first and retried alike, takes a token from the shared bucket
    first.
    """
    outcomes: list[str] = []
    hits: list[SearchHit] = []
    diagnostic: dict[str, object] = {}
    degradation: SearchDegradation | None = None
    waited = 0.0
    for index in range(SEARCH_ATTEMPTS):
        waited += await acquire_search_slot()
        hits, diagnostic = await attempt()
        degradation = classify_search_response(len(hits), diagnostic)
        outcomes.append(_attempt_outcome(degradation))
        if degradation is None:
            break
        if degradation.auth_rejected:
            break
        if degradation.rate_limited:
            # Only the ENGINE markers cool: a provider-wide transport error names
            # no engine, and the registry must stay a registry of engines.
            note_rate_limited_engines(degradation.engines)
            break
        if index + 1 < SEARCH_ATTEMPTS:
            await _sleep(_backoff_delay(index))
    return hits, _search_record(diagnostic, outcomes, degradation, waited)


def search_degradation_from_notes(notes: Mapping[str, Any]) -> SearchDegradation | None:
    """Read the below-the-model verdict back out of a RetrievalResult's notes.

    Only the layer that actually retried can claim degradation, so this returns
    None unless a diagnostic carries an exhausted attempt record. A rate-limited
    or auth-rejected search is exhausted after ONE attempt — it is every bit as
    untested as a retried outage, so it reads back the same way and only carries
    the flag.
    """
    trace = notes.get("retrieval_trace")
    if not isinstance(trace, Mapping):
        return None
    engines: list[str] = []
    errors: list[str] = []
    attempts = 0
    rate_limited = False
    auth_rejected = False
    for node in _diagnostic_nodes(trace.get("provider_diagnostics")):
        outcome = node.get(OUTCOME_KEY)
        if outcome not in _EXHAUSTED_OUTCOMES:
            continue
        rate_limited = rate_limited or outcome == OUTCOME_RATE_LIMITED
        auth_rejected = auth_rejected or outcome == OUTCOME_AUTH_REJECTED
        found_engines, found_errors = degradation_markers(node)
        engines.extend(found_engines)
        errors.extend(found_errors)
        recorded = node.get(ATTEMPTS_KEY)
        attempts = max(attempts, recorded if isinstance(recorded, int) else 1)
    if attempts == 0:
        return None
    return SearchDegradation(
        engines=tuple(dict.fromkeys(engines)),
        errors=tuple(dict.fromkeys(errors)),
        attempts=attempts,
        rate_limited=rate_limited,
        auth_rejected=auth_rejected,
    )


# --- extraction: failure classification --------------------------------------

_ANTI_BOT_MARKERS = (
    "captcha",
    "cloudflare",
    "anti-bot",
    "antibot",
    "robot check",
    "access denied",
    "forbidden",
)
_TIMEOUT_MARKERS = ("timeout", "timed out", "readtimeout", "connecttimeout")
_EMPTY_MARKERS = ("empty content", "no readable content", "no result returned")
# What a PAID extractor says about our ACCOUNT rather than about the URL. Both
# classes are terminal (see `is_transient_extraction_failure`): no retry mints a
# key or refills a plan, and retrying a 402 on 20 URLs is how a credit balance
# disappears into an outage.
_AUTH_FAILURE_MARKERS = ("auth rejected",)
_QUOTA_MARKERS = ("quota", "rate limit", "too many requests", "payment required")
_TERMINAL_STATUS_CLASS = {"paywalled": "paywalled", "not_found": "not_found"}
_ERROR_CLASS_CHARS = 32
_ERROR_TEXT_CHARS = 240
_STATUS_CONTEXT = ("http", "status", "response", "error", "redirect")


def _bounded(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].casefold()


def _http_status_class(error: str) -> str | None:
    """Classify an HTTP status code that appears in an HTTP/status context.

    Only accept a numeric status in such a context; that avoids mistaking an IP
    octet (for example 127.0.0.1) for a response code. The result is bounded to
    a three-digit status and carries no arbitrary provider text.
    """
    for match in re.finditer(r"\b([3-5][0-9]{2})\b", error):
        context = error[max(0, match.start() - 20) : min(len(error), match.end() + 20)]
        if any(marker in context for marker in _STATUS_CONTEXT):
            code = match.group(1)
            if code.startswith("3"):
                return f"redirect/http_{code}"
            return f"upstream_http_{code}"
    return None


def _has_marker(error: str, markers: tuple[str, ...]) -> bool:
    return any(marker in error for marker in markers)


def _account_error_class(error: str) -> str | None:
    """The account-level failure a paid extractor named in its own marker.

    Checked FIRST, because it is a fact about our credential and not about the
    URL: a rejected key or an exhausted plan says nothing about whether that page
    is readable, and calling either one "blocked" would slander the source and
    hide the thing a human actually has to go fix.
    """
    if _has_marker(error, _AUTH_FAILURE_MARKERS):
        return "auth_rejected"
    if _has_marker(error, _QUOTA_MARKERS):
        return "rate_limited"
    return None


def _extraction_error_class(doc: ExtractedDoc) -> str | None:
    """Classify an extraction failure without retaining provider error text."""
    status = _bounded(doc.status, _ERROR_CLASS_CHARS)
    error = _bounded(doc.error, _ERROR_TEXT_CHARS)
    if doc.fetched_ok and status == "ok":
        return None
    account = _account_error_class(error)
    if account is not None:
        return account
    if status == "blocked" or _has_marker(error, _ANTI_BOT_MARKERS):
        return "anti_bot"
    if status in _TERMINAL_STATUS_CLASS:
        return _TERMINAL_STATUS_CLASS[status]
    if _has_marker(error, _TIMEOUT_MARKERS):
        return "timeout"
    if _has_marker(error, _EMPTY_MARKERS):
        return "empty_content"
    return _http_status_class(error) or "other"


def is_transient_extraction_failure(doc: ExtractedDoc) -> bool:
    """Only a timeout or an upstream 5xx can be helped by trying again.

    Anti-bot, paywall, and not-found are settled facts about the source; a
    rejected key and an exhausted plan are settled facts about the account. A
    retry spends the run's deadline — and, on a metered extractor, the credits —
    and cannot change any of them.
    """
    if doc.fetched_ok:
        return False
    error_class = _extraction_error_class(doc)
    if error_class is None:
        return False
    return error_class == "timeout" or error_class.startswith("upstream_http_5")


# --- extraction: per-URL isolation -------------------------------------------


async def _fetch_one(
    url: str, *, fetch: ExtractFetch, failed: FailedDoc, batch_error: str
) -> ExtractedDoc:
    try:
        docs = await fetch([url])
    except _TRANSPORT_ERRORS as exc:
        return failed(url, f"{type(exc).__name__}: {exc}")
    return docs.get(url) or failed(url, batch_error)


async def _extract_isolated(
    url: str,
    *,
    fetch: ExtractFetch,
    failed: FailedDoc,
    batch_error: str,
    gate: asyncio.Semaphore,
) -> tuple[str, ExtractedDoc]:
    async with gate:
        doc = await _fetch_one(url, fetch=fetch, failed=failed, batch_error=batch_error)
        if not is_transient_extraction_failure(doc):
            return url, doc
        await _sleep(_EXTRACTION_RETRY_BACKOFF_S)
        return url, await _fetch_one(
            url, fetch=fetch, failed=failed, batch_error=batch_error
        )


def _partition_egress(
    urls: Sequence[str], failed: FailedDoc
) -> tuple[list[str], dict[str, ExtractedDoc]]:
    """Split URLs into those host egress policy allows and per-URL denials.

    A denial is a fact about ONE url; it must not delete that url's siblings.
    """
    allowed: list[str] = []
    denied: dict[str, ExtractedDoc] = {}
    for url in urls:
        try:
            validate_untrusted_url(url)
        except EgressDenied as exc:
            denied[url] = failed(url, str(exc))
        else:
            allowed.append(url)
    return allowed, denied


async def extract_with_isolation(
    urls: Sequence[str], *, fetch: ExtractFetch, failed: FailedDoc
) -> dict[str, ExtractedDoc]:
    """Extract a batch so that a failure can only ever cost the URL that caused it.

    A single unreadable URL used to take its whole batch down with it — an egress
    denial failed every sibling, and one request-level 500 marked all ~8 URLs
    failed. That is how publisher PDFs and journal pages disappeared from the
    evidence pool while easy blogs survived. Denials are isolated up front; a
    request-level failure splits the batch into per-URL requests running at
    ``EXTRACTION_SPLIT_CONCURRENCY`` so the split cannot stampede the extractor.
    """
    requested, docs = _partition_egress(urls, failed)
    if not requested:
        return docs
    try:
        return docs | await fetch(requested)
    except _TRANSPORT_ERRORS as exc:
        batch_error = f"{type(exc).__name__}: {exc}"
    gate = asyncio.Semaphore(EXTRACTION_SPLIT_CONCURRENCY)
    isolated = await asyncio.gather(
        *(
            _extract_isolated(
                url, fetch=fetch, failed=failed, batch_error=batch_error, gate=gate
            )
            for url in requested
        )
    )
    return docs | dict(isolated)
