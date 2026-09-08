"""Rate limits are not transient: no retry, a cooldown, and paced outbound load.

Three defects were measured on a live run and all three are host mechanics:

1. a degraded search whose marker text is a BAN ("Suspended: too many requests",
   "access denied", a CAPTCHA wall, an exhausted quota) was retried twice more
   with backoff — guaranteed-useless load that extends the block;
2. the same engine, named again minutes later, restarted the whole retry budget;
3. nothing paced outbound searches at all, so concurrent runs arrived in bursts.

Everything here is hermetic: the clock is a counter, both sleeps are recorded
rather than slept, and the process-wide bucket/registry are reset per test by
the suite conftest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval import _transport_retry as tr
from disco.retrieval.deep_research._search_outcomes import zero_admission_feedback
from disco.retrieval.models import SearchHit
from research_fakes import FakeExtractionProvider, FakeSearchProvider, hit

# SearXNG names an unresponsive engine as an [engine, reason] pair; by the time
# it reaches this layer the pair has been stringified. Both shapes appear below
# because both are real.
_SUSPENDED = "['brave', 'Suspended: too many requests']"
_TIMED_OUT = "['mojeek', 'timeout']"


class _Clock:
    """A monotonic clock a test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(tr, "_now", fake)
    return fake


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> Iterator[list[float]]:
    """Record pacing waits instead of sleeping them, and yield the record.

    The yield point matters: a recorded wait releases control, so concurrent
    acquirers really interleave instead of running to completion one at a time.
    """
    waits: list[float] = []

    async def fake_rate_sleep(delay: float) -> None:
        waits.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(tr, "_rate_sleep", fake_rate_sleep)
    yield waits


def _degraded(*markers: str) -> dict[str, object]:
    """The provider's own outage shape: HTTP 200, zero hits, engines named."""
    return {"status_code": 200, "unresponsive_engines": list(markers)}


def _counting_attempt(
    *responses: dict[str, object],
) -> tuple[tr.SearchAttempt, list[int]]:
    """A search attempt that walks `responses`, repeating the last one forever."""
    calls = [0]

    async def attempt() -> tuple[list[SearchHit], dict[str, object]]:
        index = min(calls[0], len(responses) - 1)
        calls[0] += 1
        return [], responses[index]

    return attempt, calls


def _enable_pacing(
    monkeypatch: pytest.MonkeyPatch, *, per_minute: str, burst: str
) -> None:
    """Opt this test back in to the pacing the conftest disables by default."""
    monkeypatch.setenv("DISCO_SEARCH_RATE_PER_MIN", per_minute)
    monkeypatch.setenv("DISCO_SEARCH_BURST", burst)
    tr.reset_search_pacing()


def _engine(search: object, docs: dict[str, str]) -> DefaultRetrievalEngine:
    return DefaultRetrievalEngine(
        search,  # pyright: ignore[reportArgumentType]
        FakeExtractionProvider(docs),
        LexicalReranker(),
    )


# ── a ban is not a transient fault ───────────────────────────────────────────


async def test_rate_limited_marker_is_never_retried(
    no_real_backoff: list[float], clock: _Clock
) -> None:
    """One attempt, one honest outage class, and no backoff at all."""
    attempt, calls = _counting_attempt(_degraded(_SUSPENDED))

    hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert hits == []
    assert calls[0] == 1  # not 3 — a retry into a ban only extends it
    assert no_real_backoff == []
    assert diagnostic[tr.OUTCOME_KEY] == "rate_limited"
    assert diagnostic[tr.OUTCOMES_KEY] == ["rate_limited"]
    assert diagnostic[tr.ATTEMPTS_KEY] == 1


async def test_transient_degradation_keeps_its_retries_unchanged(
    no_real_backoff: list[float], clock: _Clock
) -> None:
    """The control: an unexplained outage still gets today's three attempts."""
    attempt, calls = _counting_attempt(_degraded("slow-engine"))

    _hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert calls[0] == 3
    assert no_real_backoff == [1.5, 4.0]
    assert diagnostic[tr.OUTCOME_KEY] == "degraded"
    assert diagnostic[tr.OUTCOMES_KEY] == ["degraded", "degraded", "degraded"]


@pytest.mark.parametrize(
    "marker",
    [
        "['brave', 'Suspended: too many requests']",
        "['google', 'quota exceeded for today']",
        "['mojeek', 'access denied']",
        "duckduckgo: CAPTCHA challenge",
        "startpage: Rate limit reached",
        "TOO MANY REQUESTS",
    ],
)
async def test_every_frozen_ban_marker_stops_the_retries(
    marker: str, clock: _Clock
) -> None:
    attempt, calls = _counting_attempt(_degraded(marker))

    _hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert calls[0] == 1
    assert diagnostic[tr.OUTCOME_KEY] == "rate_limited"


@pytest.mark.parametrize(
    "marker", ["['mojeek', 'timeout']", "slow-engine", "['bing', 'connection reset']"]
)
async def test_ordinary_outage_text_is_not_read_as_a_ban(
    marker: str, clock: _Clock
) -> None:
    attempt, calls = _counting_attempt(_degraded(marker))

    _hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert calls[0] == 3
    assert diagnostic[tr.OUTCOME_KEY] == "degraded"


async def test_a_transport_error_that_names_a_rate_limit_stops_too(
    clock: _Clock,
) -> None:
    """The marker need not be an engine — a provider_error can carry the ban."""
    attempt, calls = _counting_attempt(
        {"provider_error": "HTTPStatusError: 429 Too Many Requests"}
    )

    _hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert calls[0] == 1
    assert diagnostic[tr.OUTCOME_KEY] == "rate_limited"
    # ...and it cools nothing: a provider-wide error names no engine, and the
    # registry would be lying if it listed the exception class as one.
    assert tr.engines_cooling() == ()
    assert tr.COOLING_KEY not in diagnostic


# ── the per-engine cooldown registry ─────────────────────────────────────────


async def test_only_the_engine_the_ban_named_starts_cooling(clock: _Clock) -> None:
    """A sibling that merely timed out in the same response is still worth a retry."""
    attempt, _calls = _counting_attempt(_degraded(_SUSPENDED, _TIMED_OUT))

    _hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert tr.engines_cooling() == ("brave",)
    assert diagnostic[tr.COOLING_KEY] == ["brave"]


async def test_cooldown_expires_after_its_window(clock: _Clock) -> None:
    tr.note_rate_limited_engines([_SUSPENDED])
    assert tr.engines_cooling() == ("brave",)

    clock.advance(299.0)
    assert tr.engines_cooling() == ("brave",)

    clock.advance(2.0)  # past the 300s default
    assert tr.engines_cooling() == ()


async def test_cooldown_window_honours_its_env_knob(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setenv("DISCO_SEARCH_ENGINE_COOLDOWN_S", "30")
    tr.note_rate_limited_engines([_SUSPENDED])

    clock.advance(29.0)
    assert tr.engines_cooling() == ("brave",)
    clock.advance(2.0)
    assert tr.engines_cooling() == ()


async def test_a_cooling_engines_recurrence_buys_no_new_retries(
    no_real_backoff: list[float], clock: _Clock
) -> None:
    """Second time round the provider names the engine with no reason at all.

    That bare name would have been an unexplained outage worth three attempts.
    Because this process watched the same engine get cut off ninety seconds ago,
    it is the same ban and buys nothing.
    """
    first, _calls = _counting_attempt(_degraded(_SUSPENDED))
    await tr.search_with_degradation_retry(first)
    no_real_backoff.clear()
    clock.advance(90.0)

    second, calls = _counting_attempt(_degraded("brave"))
    _hits, diagnostic = await tr.search_with_degradation_retry(second)

    assert calls[0] == 1
    assert no_real_backoff == []
    assert diagnostic[tr.OUTCOME_KEY] == "rate_limited"


async def test_recurrence_after_the_window_is_a_fresh_outage_again(
    clock: _Clock,
) -> None:
    await tr.search_with_degradation_retry(_counting_attempt(_degraded(_SUSPENDED))[0])
    clock.advance(301.0)

    second, calls = _counting_attempt(_degraded("brave"))
    _hits, diagnostic = await tr.search_with_degradation_retry(second)

    assert calls[0] == 3
    assert diagnostic[tr.OUTCOME_KEY] == "degraded"


async def test_a_cooling_engine_does_not_silence_a_healthy_ones_outage(
    clock: _Clock,
) -> None:
    """Only a response naming NOTHING but cooling engines is treated as the ban."""
    await tr.search_with_degradation_retry(_counting_attempt(_degraded(_SUSPENDED))[0])

    second, calls = _counting_attempt(_degraded("brave", "slow-engine"))
    _hits, diagnostic = await tr.search_with_degradation_retry(second)

    assert calls[0] == 3
    assert diagnostic[tr.OUTCOME_KEY] == "degraded"


async def test_cooldown_set_rides_in_a_later_clean_searchs_diagnostic(
    clock: _Clock,
) -> None:
    """The harness can see the cooling pool even on a search that went fine."""
    await tr.search_with_degradation_retry(_counting_attempt(_degraded(_SUSPENDED))[0])

    async def clean() -> tuple[list[SearchHit], dict[str, object]]:
        return [hit("http://x/a")], {"status_code": 200, "result_count": 1}

    _hits, diagnostic = await tr.search_with_degradation_retry(clean)

    assert diagnostic[tr.COOLING_KEY] == ["brave"]
    assert tr.OUTCOME_KEY not in diagnostic  # a clean search is not "recovered"


# ── the process-wide outbound token bucket ───────────────────────────────────


async def test_bucket_lets_the_burst_through_then_paces(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """24/min with a burst of 6: six go at once, then one every 2.5 seconds."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    limiter = tr._search_limiter()

    waits = [await limiter.acquire() for _ in range(9)]

    assert waits[:6] == [0.0] * 6
    assert waits[6:] == [2.5, 5.0, 7.5]
    assert paced == [2.5, 5.0, 7.5]  # only the waiting acquirers ever slept


async def test_bucket_refills_as_the_clock_moves(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    limiter = tr._search_limiter()
    for _ in range(6):
        await limiter.acquire()

    clock.advance(5.0)  # two tokens back at 0.4/s

    assert await limiter.acquire() == 0.0
    assert await limiter.acquire() == 0.0
    assert await limiter.acquire() == 2.5


async def test_concurrent_acquirers_are_served_in_order_and_none_starve(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """Four searches from three runs arrive together on an empty bucket."""
    _enable_pacing(monkeypatch, per_minute="60", burst="1")
    limiter = tr._search_limiter()

    waits = await asyncio.gather(*(limiter.acquire() for _ in range(4)))

    assert list(waits) == [0.0, 1.0, 2.0, 3.0]  # spaced, ordered, all served


async def test_rate_and_burst_honour_their_env_knobs(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="120", burst="2")
    limiter = tr._search_limiter()

    waits = [await limiter.acquire() for _ in range(4)]

    assert waits == [0.0, 0.0, 0.5, 1.0]


async def test_rate_zero_disables_pacing_entirely(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="0", burst="6")
    limiter = tr._search_limiter()

    assert [await limiter.acquire() for _ in range(20)] == [0.0] * 20
    assert paced == []


async def test_junk_env_values_fall_back_to_the_defaults(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="not-a-number", burst="-4")
    limiter = tr._search_limiter()

    waits = [await limiter.acquire() for _ in range(7)]

    assert waits == [0.0] * 6 + [2.5]  # the 24/min, burst-6 defaults


async def test_pacing_wait_is_recorded_in_the_search_diagnostic(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="60", burst="1")

    async def clean() -> tuple[list[SearchHit], dict[str, object]]:
        return [hit("http://x/a")], {"status_code": 200, "result_count": 1}

    _first, first_diagnostic = await tr.search_with_degradation_retry(clean)
    _second, second_diagnostic = await tr.search_with_degradation_retry(clean)

    assert tr.RATE_WAIT_KEY not in first_diagnostic  # the burst token was free
    assert second_diagnostic[tr.RATE_WAIT_KEY] == 1_000


async def test_retries_take_a_token_too(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """A degraded query is re-issued below the model — that is still outbound load."""
    _enable_pacing(monkeypatch, per_minute="60", burst="1")
    attempt, calls = _counting_attempt(_degraded("slow-engine"))

    _hits, diagnostic = await tr.search_with_degradation_retry(attempt)

    assert calls[0] == 3
    assert paced == [1.0, 2.0]  # attempts 2 and 3 waited their turn
    assert diagnostic[tr.RATE_WAIT_KEY] == 3_000


# ── the bucket listens to the engines ────────────────────────────────────────
#
# The fourth defect, measured after the first three were fixed: the bucket never
# reacted to the bans at all, so a batch kept issuing at 24/min while its engines
# were being suspended one by one, and finished with the whole pool cut off.

_QUOTA = "['google', 'quota exceeded for today']"


async def test_a_rate_limit_signal_halves_the_bucket_rate(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """24/min becomes 12/min — one ban, one halving, visible in the pacing."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    tr.note_rate_limited_engines([_SUSPENDED])
    limiter = tr._search_limiter()

    waits = [await limiter.acquire() for _ in range(7)]

    assert tr.held_search_rate_per_min() == 12.0
    assert waits == [0.0] * 6 + [5.0]  # 60/12, not the configured 60/24


async def test_a_second_signal_quarters_it(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    tr.note_rate_limited_engines([_SUSPENDED])
    tr.note_rate_limited_engines([_QUOTA])
    limiter = tr._search_limiter()

    waits = [await limiter.acquire() for _ in range(7)]

    assert tr.held_search_rate_per_min() == 6.0
    assert waits[6] == 10.0


async def test_an_engine_named_again_while_still_cooling_does_not_slow_it_twice(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """A suspended engine is repeated by every response for the whole window;
    the first naming halves the bucket, the repeats must not walk it to the
    floor while the rest of the pool is answering (2026-09-04, pubmed)."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    tr.note_rate_limited_engines([_SUSPENDED])
    tr.note_rate_limited_engines([_SUSPENDED])
    tr.note_rate_limited_engines([_SUSPENDED])

    assert tr.held_search_rate_per_min() == 12.0


async def test_one_response_naming_two_engines_is_one_signal(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """The unit is the response, not the engine count.

    A provider that lists four suspended engines in one answer told us ONE thing
    about our pace; scaling the slowdown by how verbose that answer was would put
    the bucket on the floor for a single bad response.
    """
    _enable_pacing(monkeypatch, per_minute="24", burst="6")

    tr.note_rate_limited_engines([_SUSPENDED, _QUOTA])

    assert tr.held_search_rate_per_min() == 12.0


async def test_the_slowdown_stops_at_its_floor(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """24 → 12 → 6 → 3 → 2, and 2 is where it stays however many bans arrive.

    Each ban names a different engine: a repeat of an engine already cooling
    is not a new signal (see the test above)."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    for index in range(8):
        tr.note_rate_limited_engines([f"['engine{index}', 'Suspended: too many requests']"])
    limiter = tr._search_limiter()

    waits = [await limiter.acquire() for _ in range(7)]

    assert tr.held_search_rate_per_min() == 2.0
    assert waits[6] == 30.0  # a search every half minute, but still a search


async def test_the_held_rate_does_not_climb_while_an_engine_is_cooling(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """The engine that asked for the slowdown still has its window open."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    tr.note_rate_limited_engines([_SUSPENDED])
    limiter = tr._search_limiter()

    for _ in range(5):
        clock.advance(30.0)  # inside the 300s cooldown throughout
        await limiter.acquire()

    assert tr.held_search_rate_per_min() == 12.0


async def test_the_rate_climbs_back_gradually_once_nothing_is_cooling(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """×1.25 a search, up to the configured rate — a climb, never a jump."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    tr.note_rate_limited_engines([_SUSPENDED])
    limiter = tr._search_limiter()
    clock.advance(301.0)  # the cooldown window closes
    assert tr.engines_cooling() == ()

    rates = []
    for _ in range(4):
        await limiter.acquire()
        rates.append(tr.held_search_rate_per_min())

    # None == home again: the bucket is no longer being held at all.
    assert rates == [15.0, 18.75, 23.44, None]


async def test_the_held_pace_is_reported_in_the_search_diagnostic(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """A run's trace has to show the pace it was actually held at."""
    _enable_pacing(monkeypatch, per_minute="24", burst="6")

    async def clean() -> tuple[list[SearchHit], dict[str, object]]:
        return [hit("http://x/a")], {"status_code": 200, "result_count": 1}

    _hits, before = await tr.search_with_degradation_retry(clean)
    tr.note_rate_limited_engines([_SUSPENDED])
    _hits, after = await tr.search_with_degradation_retry(clean)

    assert tr.RATE_KEY not in before  # running as configured says nothing
    assert after[tr.RATE_KEY] == 12.0
    assert after[tr.COOLING_KEY] == ["brave"]


async def test_reset_restores_the_configured_rate(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    _enable_pacing(monkeypatch, per_minute="24", burst="6")
    tr.note_rate_limited_engines([_SUSPENDED])
    assert tr.held_search_rate_per_min() == 12.0

    tr.reset_search_pacing()

    assert tr.held_search_rate_per_min() is None
    limiter = tr._search_limiter()
    assert [await limiter.acquire() for _ in range(7)] == [0.0] * 6 + [2.5]


async def test_a_ban_cannot_switch_pacing_on_when_it_is_disabled(
    paced: list[float], clock: _Clock
) -> None:
    """Rate 0 means unpaced; a slowdown must not quietly turn the bucket on."""
    tr.note_rate_limited_engines([_SUSPENDED])  # conftest default: rate 0
    limiter = tr._search_limiter()

    assert [await limiter.acquire() for _ in range(20)] == [0.0] * 20
    assert tr.held_search_rate_per_min() is None


# ── wired into the engine's real search path ─────────────────────────────────


class _RateLimitedSearch(FakeSearchProvider):
    """SearXNG's shape when an engine has been suspended for the day."""

    def __init__(self) -> None:
        super().__init__([])
        self.attempts = 0

    async def search_detailed(
        self, query: str, **kwargs: object
    ) -> tuple[list[SearchHit], dict[str, object]]:
        del query, kwargs
        self.attempts += 1
        return [], {"providers": {"searxng": _degraded(_SUSPENDED)}}

    async def search(self, query: str, **kwargs: object) -> list[SearchHit]:
        hits, _diagnostic = await self.search_detailed(query, **kwargs)
        return hits


async def test_engine_search_stops_at_one_attempt_and_traces_the_cooldown(
    no_real_backoff: list[float], clock: _Clock
) -> None:
    search = _RateLimitedSearch()

    result = await _engine(search, docs={}).retrieve(
        RetrievalRequest(query="suspended everywhere", depth="shallow")
    )

    assert search.attempts == 1
    assert no_real_backoff == []
    diagnostic = result.notes["retrieval_trace"]["provider_diagnostics"][0]
    assert diagnostic["search_outcome"] == "rate_limited"
    assert diagnostic["engines_cooling"] == ["brave"]


async def test_engine_searches_share_one_process_wide_bucket(
    monkeypatch: pytest.MonkeyPatch, paced: list[float], clock: _Clock
) -> None:
    """Two separate retrievals — the second pays for the first's token."""
    _enable_pacing(monkeypatch, per_minute="60", burst="1")
    engine = _engine(
        FakeSearchProvider([hit("http://x/a")]), docs={"http://x/a": "body text"}
    )

    async def _retrieve() -> dict[str, object]:
        result = await engine.retrieve(RetrievalRequest(query="paced", depth="shallow"))
        return result.notes["retrieval_trace"]

    first = await _retrieve()
    second = await _retrieve()

    assert first["provider_diagnostics"] == []  # nothing to say: no wait, no outage
    assert second["provider_diagnostics"] == [{"rate_wait_ms": 1_000}]


# ── what the model is told ───────────────────────────────────────────────────


async def test_rate_limited_search_reads_back_as_an_untested_outage(
    clock: _Clock,
) -> None:
    """One attempt is still an exhausted outage — the query never reached the world."""
    notes = {
        "retrieval_trace": {
            "provider_diagnostics": [
                {
                    "unresponsive_engines": [_SUSPENDED],
                    "search_attempts": 1,
                    "search_outcome": "rate_limited",
                }
            ]
        }
    }

    degradation = tr.search_degradation_from_notes(notes)

    assert degradation is not None
    assert degradation.rate_limited is True
    assert degradation.attempts == 1


def test_model_message_notes_the_cooldown_and_claims_no_retries() -> None:
    degradation = tr.SearchDegradation(
        engines=("brave",), errors=(), attempts=1, rate_limited=True
    )

    feedback = zero_admission_feedback(
        [("solid state batteries", "provider_degraded")], [("solid state batteries", degradation)]
    )

    assert "SEARCH INFRASTRUCTURE DEGRADED" in feedback
    # L19: the cooldown is STATE the message reports, not a retry the model is
    # asked to make, and the host — not the model — owns re-running the query.
    assert "the provider refused rather than failed" in feedback
    assert "the host refuses a query it has already queued" in feedback
    # It never claims retries it did not make, and never asks for a pivot.
    assert "attempts with backoff" not in feedback
    assert "Do not paraphrase those queries" not in feedback
    # The affordance that made the model do the host's retrying is gone.
    assert "re-issue them verbatim" not in feedback


def test_transient_outage_message_is_unchanged() -> None:
    degradation = tr.SearchDegradation(engines=("yahoo",), errors=(), attempts=3)

    feedback = zero_admission_feedback(
        [("q", "provider_degraded")], [("q", degradation)]
    )

    assert "3 attempts with backoff" in feedback
    assert "cooling down" not in feedback
