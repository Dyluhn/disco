"""Lane A1 — loop truth: whose turn was that, who retries, and when to wait.

Three rules, one loop:

* **Turn accounting.** ``max_research_turns`` is a budget for MODEL work. A turn
  whose every query died in infrastructure never asked the world anything and
  does not spend it. A mixed turn, a queryless turn, and a malformed turn do.
* **The re-issue queue.** Untested queries are the HOST's to run again. The loop
  owns a queue keyed to engine recovery, drains it itself with ``origin:
  "system"``, and refuses a model plan that tries to take that work back.
* **The hold.** A search pool where every engine is inside its rate-limit
  cooldown is a WAIT, not a failure. The loop holds (no turn charged, Stop live
  throughout) and resumes when the registry clears.

Everything here is hermetic: scripted router, fake retrieval, a fake clock for
the cooldown registry and the hold's sleeps.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

import pytest
from disco.core import LLMMessage
from disco.retrieval import _transport_retry as tr
from disco.retrieval.deep_research import _hold as hold_mod
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._exhaustion import ExtractionOutcomes, exhaustion_error
from disco.retrieval.deep_research._hold import (
    PROBE_INTERVAL_S,
    StarvationClock,
    hold_for_dead_pool,
    hold_reason,
    observed_engines,
)
from disco.retrieval.deep_research._progress_events import turn_position
from disco.retrieval.deep_research._reissue_queue import MAX_REISSUES, ReissueQueue
from disco.retrieval.deep_research._search_outcomes import (
    NarrowedQuery,
    narrowing_trail_rows,
    partition_fresh_queries,
    zero_admission_feedback,
)
from disco.retrieval.deep_research._search_turn import (
    ALREADY_SEARCHED,
    drain_reissue_queue,
    execute_search_turn,
)
from disco.retrieval.deep_research._turn_accounting import (
    ADMITTED,
    TRANSPORT_FAILED,
    charge_for_malformed_turn,
    charge_for_search_turn,
    turn_accounting_rollup,
)
from disco.retrieval.deep_research.agent import ResearchAgentError, run_research_agent
from disco.retrieval.models import RetrievalRequest, RetrievalResult, SearchHit
from test_research_agent import (  # the shared hermetic doubles
    _TURN0,
    _bound,
    _collect_events,
    _DegradedRetrieval,
    _FakeRetrieval,
    _passage,
    _provider_notes,
    _tested_trail,
    _TurnRouter,
)

# ---- fixtures / doubles -----------------------------------------------------


class _Clock:
    """A fake monotonic clock that only moves when something sleeps on it."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Drive the cooldown registry and the hold off one fake clock."""
    tr.reset_search_pacing()
    fake = _Clock()
    monkeypatch.setattr(tr, "_now", fake.now)
    monkeypatch.setattr(hold_mod, "_now", fake.now)
    monkeypatch.setattr(hold_mod, "_sleep", fake.sleep)
    yield fake
    tr.reset_search_pacing()


class _RateLimitedRetrieval:
    """Search infrastructure that says it CUT US OFF — the cooldown class.

    It cools its engines through the real registry, exactly as the retry driver
    does when a provider answers "too many requests".
    """

    def __init__(self, engines: tuple[str, ...] = ("yahoo", "brave")) -> None:
        self.engines = engines
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        self.requests.append(req)
        tr.note_rate_limited_engines(
            [f"{engine}: too many requests" for engine in self.engines]
        )
        notes = _provider_notes("rate_limited", attempts=1)
        diagnostic = notes["retrieval_trace"]["provider_diagnostics"][0]
        diagnostic["unresponsive_engines"] = list(self.engines)
        return RetrievalResult(
            passages=[], all_hits=[], extracted=[], issued_queries=[req.query], notes=notes
        )


class _RecoveringRetrieval:
    """Degraded until the queue re-runs it, then it answers normally."""

    def __init__(self) -> None:
        self.calls = 0
        self.queries: list[str] = []

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        self.calls += 1
        self.queries.append(req.query)
        if self.calls == 1:
            return RetrievalResult(
                passages=[],
                all_hits=[],
                extracted=[],
                issued_queries=[req.query],
                notes=_provider_notes("degraded"),
            )
        passages = [_passage(self.calls * 10)]
        return RetrievalResult(
            passages=passages,
            all_hits=[
                SearchHit(url=p.source_url, title=p.source_title, snippet="s", rank=0)
                for p in passages
            ],
            extracted=[],
            issued_queries=[req.query],
        )


async def _run_turn(state: _AgentState, retrieval: Any, queries: list[str], turn: int = 0):
    captured, emit = _collect_events()
    result = await execute_search_turn(
        state,
        queries,
        turn,
        retrieval_engine=retrieval,
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(1, 8),
    )
    return result, captured


# ---- 1. turn accounting -----------------------------------------------------


def test_an_all_infrastructure_turn_costs_the_model_nothing() -> None:
    charge = charge_for_search_turn(["provider_degraded", "extraction_failure"])

    assert charge.counted is False
    assert "infrastructure" in charge.reason


def test_one_query_that_reached_the_world_makes_the_turn_the_models() -> None:
    """A clean zero IS a research result — pivoting off it is model work."""
    mixed = charge_for_search_turn(["provider_degraded", "no_hits"])
    admitted = charge_for_search_turn([ADMITTED, TRANSPORT_FAILED])

    assert mixed.counted is True and admitted.counted is True


def test_a_turn_that_issued_nothing_is_still_the_models_to_spend() -> None:
    charge = charge_for_search_turn([])

    assert charge.counted is True and "no query was issued" in charge.reason


def test_a_malformed_turn_is_always_charged() -> None:
    assert charge_for_malformed_turn().counted is True


async def test_a_degraded_turn_does_not_move_the_budget_and_says_so_in_the_trail() -> None:
    state = _AgentState(budget=SourceBudget(10))
    result, _captured = await _run_turn(state, _DegradedRetrieval(), ["a fresh angle"])

    assert result.charge.counted is False
    summary = [row for row in state.trail if row.get("kind") == "search_turn_summary"][-1]
    assert summary["turn_counted"] is False
    assert "infrastructure" in summary["turn_counted_reason"]


async def test_the_budget_line_and_the_turn_event_show_the_same_countdown() -> None:
    """The model's BUDGET line and the UI's `turn` event are one countdown."""
    router = _TurnRouter([_TURN0, _TURN0])
    captured, emit = _collect_events()
    bound = _bound(max_research_turns=8)
    with pytest.raises(ResearchAgentError):
        await run_research_agent(
            "q",
            router=router,
            retrieval_engine=_DegradedRetrieval(),
            bound=bound,
            namespace="ns",
            emit=emit,
        )
    prompt = router.requests[0].messages[-1].content
    remaining = int(re.search(r"BUDGET: (\d+) research turns remaining of (\d+)", prompt)[1])
    total = int(re.search(r"BUDGET: (\d+) research turns remaining of (\d+)", prompt)[2])
    first_turn_event = next(payload for kind, payload in captured if kind == "turn")

    assert (first_turn_event["n"], first_turn_event["of"]) == turn_position(remaining, total)
    assert (first_turn_event["n"], first_turn_event["of"]) == (1, 8)


async def test_every_refused_query_becomes_one_event_on_the_wire() -> None:
    """A refusal used to be invisible: it lived only in the run's in-memory
    trail and in the next prompt's feedback line, neither of which reaches a
    reader. Batch B lost 104 of 490 queries and 14 whole turns that way, and
    the UI showed "turn 11 searching" followed by nothing.

    The event is built from the same audit row the trail gets, so the two
    cannot disagree about the reason.
    """
    repeat_turn = (
        '{"brief": "b", "decision_summary": "re-propose the same query", '
        '"coverage": {"covered": [], "open": ["players"], '
        '"contradictions_checked": []}, "queries": ["X overview"], '
        '"ready_to_write": false}'
    )
    router = _TurnRouter([_TURN0, repeat_turn, repeat_turn])
    captured, emit = _collect_events()
    with contextlib.suppress(ResearchAgentError):
        await run_research_agent(
            "q",
            router=router,
            retrieval_engine=_FakeRetrieval(),
            bound=_bound(max_research_turns=3),
            namespace="ns",
            emit=emit,
        )

    refusals = [payload for kind, payload in captured if kind == "query_refused"]
    assert refusals, "a refused query must be observable on the wire"
    assert refusals[0] == {
        "query": "X overview",
        "reason": "repeat",
        "duplicates": "X overview",
        "duplicates_turn": 0,
    }


def test_the_refusal_event_payload_carries_each_walls_own_reason() -> None:
    """One event shape, five reasons, every field read off the audit row.

    The frontend (lane F5) renders from exactly these keys: `query` and
    `reason` always; `duplicates`/`duplicates_turn` on a freshness refusal;
    `angle`/`why` on a dead end. Nothing is invented for a class that has
    neither.
    """
    from disco.retrieval.deep_research._progress_events import query_refused_payload
    from disco.retrieval.deep_research._search_outcomes import (
        ExhaustedAngle,
        QueryRefusal,
        TestedQuery,
        query_tokens,
        refusal_trail_rows,
    )

    earlier = TestedQuery(
        query="grid battery cost 2020",
        normalized="grid battery cost 2020",
        tokens=query_tokens("grid battery cost 2020"),
        turn=2,
        outcome="admitted 4",
    )
    rows = refusal_trail_rows(
        5,
        [
            QueryRefusal(query="grid battery cost 2020", earlier=earlier),
            QueryRefusal(query="grid battery costs since 2020", earlier=earlier),
            QueryRefusal(
                query="another dead-end attempt",
                earlier=None,
                exhausted=ExhaustedAngle(
                    angle="storage chemistry",
                    why="3 distinct queries reached the world for it and admitted nothing",
                    evidence="nothing banked yet",
                ),
            ),
            QueryRefusal(query="", earlier=None),
        ],
    )
    payloads = [query_refused_payload(row) for row in rows]

    assert [payload["reason"] for payload in payloads] == [
        "repeat",
        "near_duplicate",
        "exhausted",
        "empty",
    ]
    assert payloads[0]["duplicates_turn"] == 2
    assert payloads[2]["angle"] == "storage chemistry"
    assert "admitted nothing" in payloads[2]["why"]
    assert set(payloads[3]) == {"query", "reason"}


def test_the_report_rollup_matches_the_trail_rows() -> None:
    trail = [
        {"kind": "turn_charge", "turn": 0, "turn_counted": False, "turn_counted_reason": "x"},
        {"kind": "turn_charge", "turn": 1, "turn_counted": True, "turn_counted_reason": "y"},
        {"kind": "turn_charge", "turn": 2, "turn_counted": True, "turn_counted_reason": "y"},
        {"kind": "search_turn_summary", "turn": 1, "turn_counted": True},  # not a turn row
    ]
    rollup = turn_accounting_rollup(trail, total_turns=16)

    rows = [row for row in trail if row.get("kind") == "turn_charge"]
    assert rollup == {
        "model_turns": 2,
        "degraded_turns": 1,
        "refused_turns": 0,
        "of": 16,
    }
    assert rollup["model_turns"] + rollup["degraded_turns"] == len(rows)


def test_the_rollup_counts_turns_that_issued_nothing_because_all_was_refused() -> None:
    """T3: `degraded_turns == 0` used to make the notice claim every turn went to
    a search that came back with results — while the trace above it read
    "Not searched ×3". A turn whose every proposal the walls refused is charged
    to the model on purpose, so the ledger has to carry it as its own number.

    A turn that simply proposed no query carries the same charge reason and is
    NOT one of these; the refusal rows are what tell the two apart.
    """
    no_queries = "no query was issued: the turn was the model's to spend"
    trail = [
        {"kind": "query_rejected", "turn": 1, "query": "a reworded query"},
        {"kind": "turn_charge", "turn": 1, "turn_counted": True, "turn_counted_reason": no_queries},
        # turn 2 proposed nothing at all — same reason, no refusal rows.
        {"kind": "turn_charge", "turn": 2, "turn_counted": True, "turn_counted_reason": no_queries},
        {
            "kind": "turn_charge",
            "turn": 3,
            "turn_counted": True,
            "turn_counted_reason": "at least one query reached the world",
        },
    ]

    assert turn_accounting_rollup(trail, total_turns=8) == {
        "model_turns": 3,
        "degraded_turns": 0,
        "refused_turns": 1,
        "of": 8,
    }


# ---- 2. the re-issue queue --------------------------------------------------


async def test_a_degraded_query_becomes_the_hosts_work(clock: _Clock) -> None:
    state = _AgentState(budget=SourceBudget(10))
    await _run_turn(state, _DegradedRetrieval(), ["an untested angle"])

    assert state.reissue.queries == ("an untested angle",)


async def test_the_loop_drains_the_queue_itself_with_a_system_origin(clock: _Clock) -> None:
    state = _AgentState(budget=SourceBudget(10))
    retrieval = _RecoveringRetrieval()
    await _run_turn(state, retrieval, ["an untested angle"])
    assert state.reissue.queries == ("an untested angle",)

    captured, emit = _collect_events()
    drained = await drain_reissue_queue(
        state,
        1,
        retrieval_engine=retrieval,
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(2, 8),
    )

    assert drained is not None and drained.admitted == 1
    # The host ran it, not the model: the query went out a second time, verbatim.
    assert retrieval.queries == ["an untested angle", "an untested angle"]
    searches = [payload for kind, payload in captured if kind == "search"]
    assert searches and all(payload["origin"] == "system" for payload in searches)
    rows = [row for row in state.trail if row.get("kind") == "search"]
    assert rows[-1]["origin"] == "system" and rows[0]["origin"] == "model"
    # …and a drained query is no longer queued.
    assert state.reissue.queries == ()


def test_a_queued_query_is_blocked_while_its_engines_are_cooling(clock: _Clock) -> None:
    queue = ReissueQueue()
    queue.enqueue("cooling angle", turn=0, reason="provider_degraded", engines=("brave",))
    tr.note_rate_limited_engines(["brave: too many requests"])

    assert queue.take_ready(tr.engine_cooldown_seconds()) == []

    clock.t += 10_000.0  # the cooldown window expires

    assert [item.query for item in queue.take_ready(tr.engine_cooldown_seconds())] == [
        "cooling angle"
    ]


def test_a_model_re_issue_of_a_queued_query_is_refused_and_told_why(clock: _Clock) -> None:
    queue = ReissueQueue()
    queue.enqueue(
        "solid state battery cost", turn=0, reason="provider_degraded", engines=("brave",)
    )
    tr.note_rate_limited_engines(["brave: too many requests"])

    fresh, refused, _narrowed = partition_fresh_queries(
        [], ["solid state battery cost"], queue.refs(tr.engine_cooldown_seconds())
    )

    assert fresh == [] and len(refused) == 1
    assert refused[0].queued is not None
    assert "queued to run again behind" in refused[0].queued.detail
    assert "brave" in refused[0].queued.detail


def test_the_hosts_retry_budget_is_hard_and_the_angle_then_stands_unreached() -> None:
    """Without a spent budget the model and the host would retry an outage forever."""
    queue = ReissueQueue()
    queue.enqueue("dead angle", turn=0, reason="extraction_failure")
    item = queue.take_ready({})[0]

    assert queue.requeue(item, reason="extraction_failure") is True
    again = queue.take_ready({})[0]
    assert again.attempts == MAX_REISSUES
    assert queue.requeue(again, reason="extraction_failure") is False
    assert queue.abandoned == ("dead angle",)

    # …and it stays refused, so the model cannot hand it back for a fresh budget.
    _fresh, refused, _narrowed = partition_fresh_queries([], ["dead angle"], queue.refs({}))
    assert refused and refused[0].queued is not None
    assert "retry budget for it is spent" in refused[0].queued.detail
    assert "stands unreached" in refused[0].queued.detail


# ---- 3. the hold ------------------------------------------------------------


def test_the_pool_is_dead_only_when_no_engine_is_left(clock: _Clock) -> None:
    observed = frozenset({"yahoo", "brave"})

    assert hold_reason(observed, {}, 0.0) is None  # nothing cooling
    assert hold_reason(observed, {"brave": 30.0}, 0.0) is None  # yahoo still live
    assert (
        hold_reason(observed, {"brave": 30.0, "yahoo": 10.0}, 0.0) == "search_pool_cooling"
    )
    # Nothing observed: a starved bucket over a full probe interval, and only
    # then — momentary pacing is not a dead pool.
    assert hold_reason(frozenset(), {"brave": 30.0}, 0.0) is None
    assert (
        hold_reason(frozenset(), {"brave": 30.0}, PROBE_INTERVAL_S) == "search_rate_starved"
    )


def test_a_rejected_credential_is_never_a_hold(clock: _Clock) -> None:
    """auth_rejected starts no cooldown by design, so it cannot become a wait."""
    tr.note_rate_limited_engines(["tavily: auth rejected"])

    assert tr.engine_cooldown_seconds() == {}
    assert hold_reason(frozenset({"tavily"}), tr.engine_cooldown_seconds(), 0.0) is None


async def test_the_loop_holds_on_a_cooling_pool_and_resumes_when_it_clears(
    clock: _Clock,
) -> None:
    tr.note_rate_limited_engines(["yahoo: too many requests", "brave: too many requests"])
    captured, emit = _collect_events()

    outcome = await hold_for_dead_pool(
        observed=frozenset({"yahoo", "brave"}),
        sources_retained=7,
        position=(3, 16),
        queued_queries=["an untested angle"],
        starvation=StarvationClock(),
        emit=emit,
        should_cancel=None,
    )

    assert outcome.stopped is False and outcome.waited_s > 0
    kinds = [kind for kind, _payload in captured]
    assert kinds == ["hold", "hold_resumed"]
    payload = captured[0][1]
    assert payload["reason"] == "search_pool_cooling"
    assert [engine["name"] for engine in payload["engines"]] == ["brave", "yahoo"]
    assert all(engine["resume_at"] for engine in payload["engines"])
    assert payload["sources_retained"] == 7
    assert payload["turn"] == {"n": 3, "of": 16}
    assert payload["queued_queries"] == ["an untested angle"]
    assert captured[1][1]["engines_live"] == ["brave", "yahoo"]


async def test_stop_during_a_hold_lands_immediately(clock: _Clock) -> None:
    tr.note_rate_limited_engines(["yahoo: too many requests"])
    captured, emit = _collect_events()

    outcome = await hold_for_dead_pool(
        observed=frozenset({"yahoo"}),
        sources_retained=4,
        position=(2, 16),
        queued_queries=[],
        starvation=StarvationClock(),
        emit=emit,
        should_cancel=lambda: True,
    )

    assert outcome.stopped is True
    assert [kind for kind, _payload in captured] == ["hold"]


async def test_a_held_run_keeps_its_sources_and_stops_into_a_resumable_checkpoint(
    clock: _Clock,
) -> None:
    """Stop during the hold → the loop's `stopped` bound, pool intact."""
    router = _TurnRouter([_TURN0, _TURN0, _TURN0])
    _captured, emit = _collect_events()
    stop = {"now": False}

    async def emit_and_stop(kind: str, payload: dict[str, Any]) -> None:
        await emit(kind, payload)
        if kind == "hold":
            stop["now"] = True

    retained = [_passage(1), _passage(2)]
    outcome = await run_research_agent(
        "q",
        router=router,
        retrieval_engine=_RateLimitedRetrieval(),
        bound=_bound(max_research_turns=8),
        namespace="ns",
        emit=emit_and_stop,
        should_cancel=lambda: stop["now"],
        upload_passages=retained,
    )

    assert outcome.bounded_by == "stopped"
    # Everything gathered before the hold travels into the checkpoint, which is
    # what makes Stop-during-a-hold a choice rather than a loss.
    assert [passage.id for passage in outcome.passages] == [p.id for p in retained]
    # The hold cost no turn: the trail's charge rows are the only turn spend.
    rollup = turn_accounting_rollup(outcome.trail, total_turns=8)
    assert rollup["degraded_turns"] >= 1
    assert rollup["model_turns"] + rollup["degraded_turns"] <= 8
    assert any(row.get("kind") == "hold" for row in outcome.trail)


# ---- 4. the exhaustion messages radiate all four things ---------------------


def _four_parts(message: str) -> None:
    assert "STATE:" in message and "NEXT:" in message and "STILL AVAILABLE:" in message


def test_an_extraction_outage_error_names_the_repair_for_its_own_class() -> None:
    extraction = ExtractionOutcomes(attempted=60, failures={"auth_rejected": 59})
    message = exhaustion_error(
        budget="turn",
        extraction=extraction,
        trail=[],
        sources_retained=0,
        turns_used=16,
        turns_total=16,
    )

    _four_parts(message)
    assert "extraction provider failing: 59/60" in message  # why
    assert "16 of 16 research turns used" in message  # state
    assert "correct its API key" in message  # next, for THIS class
    assert "asking again after the fix" in message  # what remains


def test_a_wedged_extractor_and_a_refused_key_do_not_get_the_same_next_action() -> None:
    def message_for(error_class: str) -> str:
        return exhaustion_error(
            budget="turn",
            extraction=ExtractionOutcomes(attempted=60, failures={error_class: 59}),
            trail=[],
            sources_retained=0,
            turns_used=16,
            turns_total=16,
        )

    assert "restart it or restore its endpoint" in message_for("timeout")
    assert "correct its API key" in message_for("auth_rejected")
    assert "not an outage" in message_for("anti_bot")


def test_a_cooling_search_pool_error_names_the_engines_and_the_deadline() -> None:
    trail = [
        {"kind": "search", "query": f"q{index}", "yield_reason": "provider_degraded"}
        for index in range(4)
    ]
    message = exhaustion_error(
        budget="turn",
        extraction=ExtractionOutcomes(),
        trail=trail,
        sources_retained=0,
        turns_used=8,
        turns_total=8,
        cooling={"brave": 120.0, "yahoo": 240.0},
        queued=2,
    )

    _four_parts(message)
    assert "search provider failing: 4/4 queries degraded" in message
    assert "search engines brave, yahoo are cooling until" in message
    assert "2 queries were still queued" in message
    assert "extraction provider" not in message  # upstream first, one service named


def test_the_infrastructure_circuit_breaker_does_not_claim_a_spent_turn_budget() -> None:
    """The run never spent its turns; saying it did would hide the real fault."""
    message = exhaustion_error(
        budget="infrastructure",
        extraction=ExtractionOutcomes(attempted=40, failures={"timeout": 40}),
        trail=[],
        sources_retained=0,
        turns_used=0,
        turns_total=16,
        streak=16,
    )

    _four_parts(message)
    assert message.startswith(
        "research stopped after 16 consecutive research turns whose every query "
        "died in infrastructure"
    )
    assert "exhausted its turn budget" not in message


def test_a_run_that_really_found_nothing_is_not_blamed_on_infrastructure() -> None:
    message = exhaustion_error(
        budget="turn",
        extraction=ExtractionOutcomes(),
        trail=[],
        sources_retained=0,
        turns_used=8,
        turns_total=8,
    )

    _four_parts(message)
    assert "provider failing" not in message
    assert "it had nothing usable" in message and "not an outage" in message


# ---- 5. the prompt-byte guard ----------------------------------------------

#: The affordance L19 deleted, and the two system-gated paths the model must
#: never learn exist (rule 6: presence implies affordance).
_FORBIDDEN_IN_PROMPTS = re.compile(
    r"re-?issue|\bhold(s|ing)?\b|hold_resumed|\bship(s|ped|ping)\b|best candidate",
    re.IGNORECASE,
)


def test_no_infrastructure_feedback_invites_the_model_to_retry() -> None:
    degradation = tr.SearchDegradation(
        engines=("brave",), errors=(), attempts=3, rate_limited=False
    )
    messages = [
        zero_admission_feedback([("q", "provider_degraded")], [("q", degradation)]),
        zero_admission_feedback([("q", "extraction_failure")], []),
        zero_admission_feedback([("q", "no_hits")], []),
        zero_admission_feedback(
            [("q", "provider_degraded")],
            [("q", degradation)],
            queued=["q"],
            cooling={"brave": 90.0},
        ),
    ]

    for message in messages:
        assert not _FORBIDDEN_IN_PROMPTS.search(message), message


async def test_no_research_prompt_names_the_retry_affordance_or_the_hold(
    clock: _Clock,
) -> None:
    """The real prompt bytes of a run whose whole pool was cut off."""
    router = _TurnRouter([_TURN0] * 6)
    _captured, emit = _collect_events()
    with pytest.raises(ResearchAgentError):
        await run_research_agent(
            "q",
            router=router,
            retrieval_engine=_DegradedRetrieval(),
            bound=_bound(max_research_turns=2),
            namespace="ns",
            emit=emit,
        )

    prompts = [
        message.content
        for request in router.requests
        for message in request.messages
    ]
    assert prompts
    for prompt in prompts:
        found = _FORBIDDEN_IN_PROMPTS.search(prompt)
        assert found is None, f"prompt names a system-gated path: {found and found.group(0)}"


def test_the_observed_engine_universe_comes_from_what_the_run_saw() -> None:
    trail = [
        {
            "kind": "search",
            "retrieval_trace": {
                "provider_diagnostics": [{"unresponsive_engines": ["Yahoo", "brave"]}]
            },
        }
    ]
    hits = [SearchHit(url="https://x/1", title="t", snippet="s", source_engine="bing+mojeek")]

    assert observed_engines(trail, hits) == frozenset({"yahoo", "brave", "bing", "mojeek"})


def test_a_degraded_run_never_lets_the_search_turn_pretend_it_admitted_anything() -> None:
    """Guard on the fake itself: these tests only mean something if it degrades."""
    result = charge_for_search_turn(["provider_degraded"])
    assert result.counted is False


async def test_a_healthy_run_charges_every_turn_exactly_once(clock: _Clock) -> None:
    state = _AgentState(budget=SourceBudget(10))
    result, _captured = await _run_turn(state, _FakeRetrieval(), ["a real angle"])

    assert result.charge.counted is True and result.admitted > 0
    assert state.reissue.queries == ()


def test_the_report_carries_the_rollup_the_frontend_notice_reads() -> None:
    """`meta.turn_accounting` is how a finished report says whose turns those were.

    The per-turn verdict lives in the trail, and the trail rides on a Stop
    CHECKPOINT — a finished report never carries it, so without this rollup the
    UI cannot tell "spent every turn on work" from "a cooling pool ate them".
    """
    from disco.core import ReportSection
    from disco.retrieval.deep_research.engine import ReportFromRun

    report = ReportFromRun(
        query="q",
        summary="s" * 40,
        sections=[ReportSection(id="a", title="T", markdown="body " * 20)],
        cited_passages=[],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=0,
        bounded_by="turns",
        depth_tier="quick",
    )
    trail = [
        {"kind": "turn_charge", "turn": 0, "turn_counted": False},
        {"kind": "turn_charge", "turn": 1, "turn_counted": True},
    ]
    report.turn_accounting = turn_accounting_rollup(trail, total_turns=8)

    assert report.to_event().meta["turn_accounting"] == {
        "model_turns": 1,
        "degraded_turns": 1,
        "refused_turns": 0,
        "of": 8,
    }


# ---- 6. writer-side progress rounds ----------------------------------------


async def test_the_writer_rounds_emit_their_own_events_and_keep_the_old_phases() -> None:
    """`review`/`rework` are ADDED beside the `phase` events the UI already reads.

    The existing payloads must stay byte-identical: a heartbeat rework that
    silently changed an event the current UI depends on would be a regression
    dressed as progress.
    """
    from disco.retrieval.deep_research._progress_events import (
        emit_continuation,
        emit_review,
        emit_rework,
    )

    captured, emit = _collect_events()
    await emit_review(emit, 2, 3)
    await emit_rework(emit, 2, 2)
    await emit_continuation(emit, 1, 4)

    assert captured == [
        ("phase", {"phase": "reviewing", "attempt": 2}),
        ("review", {"k": 2, "of": 3}),
        ("phase", {"phase": "writing", "attempt": 3}),
        ("rework", {"k": 2, "of": 2}),
        ("continuation", {"k": 1, "of": 4}),
    ]


async def test_a_continued_report_reports_each_continuation_round() -> None:
    """The writer's continuation loop is the longest silent stretch of a run.
    Every round the server cut off (`finish_reason == "length"`) is announced
    against the fixed bound before the next call opens."""
    from disco.retrieval.deep_research.writer import (
        _MAX_CONTINUATIONS,
        _continue_if_cut_off,
        _Draft,
    )
    from test_synthesis_repair import _ScriptedRouter as _WriterRouter

    # Draft cut off, first continuation cut off again, second one completes.
    router = _WriterRouter([(" tail one [[p1]].", "length"), (" tail two [[p1]].", "stop")])
    captured, emit = _collect_events()

    draft, rounds = await _continue_if_cut_off(
        router,
        [LLMMessage(role="user", content="WRITE THE REPORT")],
        _Draft(markdown="## Findings\nA start [[p1]] that was cut", cut_off=True),
        rounds_used=0,
        max_tokens=1_000,
        conversation_id=None,
        emit=emit,
        should_cancel=None,
    )

    assert rounds == 2 and not draft.cut_off
    assert [payload for kind, payload in captured if kind == "continuation"] == [
        {"k": 1, "of": _MAX_CONTINUATIONS},
        {"k": 2, "of": _MAX_CONTINUATIONS},
    ]


def test_the_audit_row_does_not_call_a_spent_budget_the_same_as_a_queued_query() -> None:
    """Work still scheduled and an angle now unreached are different findings."""
    from disco.retrieval.deep_research._search_outcomes import refusal_trail_rows

    queue = ReissueQueue()
    queue.enqueue("dead angle", turn=0, reason="extraction_failure")
    for _ in range(MAX_REISSUES):  # spend the host's retry budget on it
        for item in queue.take_ready({}):
            queue.requeue(item, reason="extraction_failure")
    assert queue.abandoned == ("dead angle",)
    queue.enqueue("live angle", turn=1, reason="provider_degraded")

    _fresh, refused, _narrowed = partition_fresh_queries(
        [], ["live angle", "dead angle"], queue.refs({})
    )
    rows = refusal_trail_rows(3, refused)

    assert [row["rejected"] for row in rows] == ["queued", "retry_budget_spent"]


# ---- 7. a narrowed query says so on the wire --------------------------------
#
# The freshness wall refuses a repeat and ISSUES a narrowing of one — a query
# that repeats a tested query with a scope added (`site:`, a quoted phrase, a
# DOI, `pdf`, a 4+-digit number). That verdict lived only in the
# `query_narrowed` trail row, and the trail reaches the UI on a Stop checkpoint
# only, so on a finished report the trace showed the tested query and its
# narrowing as two identical-looking `Searching:` lines.


async def test_a_narrowed_query_carries_what_it_narrowed_and_on_what_scope(
    clock: _Clock,
) -> None:
    """The wall's own verdict, on the same event the query already rides."""
    tested = "Lazard LCOE LCOS 2024 storage versus gas peaker"
    narrowing = "Lazard LCOE LCOS 2024 storage versus gas peaker site:eia.gov"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(tested))

    fresh, refused, narrowed = partition_fresh_queries(
        state.trail, (narrowing,)
    )
    assert (fresh, refused) == ([narrowing], [])

    captured, emit = _collect_events()
    await execute_search_turn(
        state,
        fresh,
        1,
        retrieval_engine=_FakeRetrieval(),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(2, 8),
        narrowed={item.query: item for item in narrowed},
    )

    searches = [payload for kind, payload in captured if kind == "search"]
    assert len(searches) == 1
    assert searches[0]["narrowed_from"] == tested
    assert searches[0]["narrowing"] == ["site:eia.gov"]
    # No new action name: it is still an ordinary `search`.
    assert searches[0]["query"] == narrowing and searches[0]["origin"] == "model"


async def test_a_query_the_wall_never_matched_carries_neither_key(
    clock: _Clock,
) -> None:
    """Absent, not null and not empty — one shape to read, no flag to interpret."""
    state = _AgentState(budget=SourceBudget(10))

    captured, emit = _collect_events()
    await execute_search_turn(
        state,
        ["an angle nothing has tested"],
        0,
        retrieval_engine=_FakeRetrieval(),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(1, 8),
        narrowed={},
    )

    searches = [payload for kind, payload in captured if kind == "search"]
    assert searches and "narrowed_from" not in searches[0]
    assert "narrowing" not in searches[0]


async def test_only_the_narrowed_query_of_a_turn_carries_the_verdict(
    clock: _Clock,
) -> None:
    """A turn mixes narrowings with plain fresh queries; only one is a verdict."""
    tested = "NREL ATB 2024 utility-scale battery capital cost"
    narrowing = f"{tested} pdf"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(tested))

    fresh, _refused, narrowed = partition_fresh_queries(
        state.trail, (narrowing, "an entirely different angle")
    )

    captured, emit = _collect_events()
    await execute_search_turn(
        state,
        fresh,
        1,
        retrieval_engine=_FakeRetrieval(),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(2, 8),
        narrowed={item.query: item for item in narrowed},
    )

    searches = [payload for kind, payload in captured if kind == "search"]
    assert [payload.get("narrowed_from") for payload in searches] == [tested, None]
    assert [payload.get("narrowing") for payload in searches] == [["pdf"], None]


async def test_the_trail_row_and_the_event_cannot_disagree(clock: _Clock) -> None:
    """Both are built from the same `NarrowedQuery`, so they say the same thing."""
    tested = "SMIC 7nm N+2 yield TechInsights teardown"
    narrowing = f"{tested} 2025"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(tested))

    fresh, _refused, narrowed = partition_fresh_queries(state.trail, (narrowing,))
    rows = narrowing_trail_rows(1, narrowed)

    captured, emit = _collect_events()
    await execute_search_turn(
        state,
        fresh,
        1,
        retrieval_engine=_FakeRetrieval(),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(2, 8),
        narrowed={item.query: item for item in narrowed},
    )

    searches = [payload for kind, payload in captured if kind == "search"]
    assert rows[0]["narrowed_from"] == searches[0]["narrowed_from"]
    assert rows[0]["narrowing"] == searches[0]["narrowing"]


# ---- 8. a query this run already searched never goes out twice --------------
#
# Measured (09-03 batch, run-03): 22 of 105 queries the researcher issued were
# exact repeats, and the acceptance harness flagged repeated searches on 7 of 8
# runs. The freshness wall stands in front of the model's PLAN and refuses what
# it can see; the wall below stands in front of the WIRE and refuses what that
# one never got to judge — a resumed run carries the prior run's trail forward
# while its re-issue queue starts empty, so a query it already issued has
# nothing left refusing it. Every repeat is a search spent against engines that
# suspend at volume, for results the pool already holds.


async def test_a_query_this_run_already_searched_never_reaches_the_provider(
    clock: _Clock,
) -> None:
    query = "NREL ATB 2024 utility-scale battery overnight capital cost"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(query, turn=2, reason="no_hits"))
    retrieval = _FakeRetrieval()

    result, _captured = await _run_turn(state, retrieval, [query], turn=5)

    assert retrieval.requests == []
    assert [outcome.outcome for outcome in result.outcomes] == [ALREADY_SEARCHED]
    # Neither currency moves: no search issued, no source slot spent.
    assert state.searches == 0 and state.budget.remaining == 10


async def test_the_walled_repeat_is_recorded_with_the_turn_it_was_first_issued_on(
    clock: _Clock,
) -> None:
    """The `query_rejected` shape the other walls use — never a `search` row."""
    query = "Lazard LCOE LCOS 2024 storage versus gas peaker"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(query, turn=2, reason="no_hits"))

    _result, captured = await _run_turn(state, _FakeRetrieval(), [query], turn=5)

    row = [entry for entry in state.trail if entry.get("kind") == "query_rejected"][-1]
    assert row["rejected"] == ALREADY_SEARCHED
    assert row["duplicates"] == query and row["duplicates_turn"] == 2
    assert row["duplicates_outcome"] == "no_hits"
    refused = [payload for kind, payload in captured if kind == "query_refused"]
    assert refused and refused[0]["duplicates_turn"] == 2


async def test_the_walled_repeat_tells_the_model_the_turn_and_the_next_angle(
    clock: _Clock,
) -> None:
    """Why, the state now, the exact next action, and what stays allowed."""
    query = "SMIC 7nm N+2 yield TechInsights teardown"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(query, turn=3, reason="no_hits"))
    state.coverage["open"] = ["cost per wafer is uncorroborated"]

    await _run_turn(state, _FakeRetrieval(), [query], turn=6)

    assert "was searched on turn 3" in state.feedback
    assert "no source slot was spent" in state.feedback
    assert "cost per wafer is uncorroborated" in state.feedback
    assert "NARROWING" in state.feedback


async def test_a_turn_of_nothing_but_walled_repeats_is_no_search_and_no_thrash(
    clock: _Clock,
) -> None:
    """What the acceptance harness reads: no search on the wire, no empty turn.

    The harness counts searches from `search` events and no-progress turns from
    `search_turn_summary` rows. A walled repeat writes neither, so it is
    neither a search it can call repeated nor a turn it can call no-progress.
    """
    query = "solid state battery EV commercialization timeline"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(query, turn=1, reason="no_hits"))

    result, captured = await _run_turn(state, _FakeRetrieval(), [query], turn=4)

    assert [kind for kind, _payload in captured if kind == "search"] == []
    assert [row for row in state.trail if row.get("kind") == "search_turn_summary"] == []
    # The turn is still the model's to spend — it just issued nothing.
    assert result.charge.counted is True
    assert "no query was issued" in result.charge.reason


async def test_the_hosts_own_reissue_of_a_searched_query_still_goes_through(
    clock: _Clock,
) -> None:
    """The loop draining its re-issue queue is the host's retry, not a repeat."""
    query = "an angle the provider refused"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(query, turn=1, reason="provider_degraded"))
    retrieval = _FakeRetrieval()

    captured, emit = _collect_events()
    await execute_search_turn(
        state,
        [query],
        2,
        retrieval_engine=retrieval,
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(3, 8),
        origin="system",
    )

    assert [req.query for req in retrieval.requests] == [query]
    searches = [payload for kind, payload in captured if kind == "search"]
    assert searches and searches[0]["origin"] == "system"


async def test_a_narrowing_of_an_untested_issue_still_goes_through(
    clock: _Clock,
) -> None:
    """A scope the earlier issue lacked reaches sources it could not.

    The freshness wall can classify a query as a NARROWING of one tested query
    while it is also the verbatim form of an earlier issue that never reached
    the world. That is the one repeat the host wants run, so the wall here
    stands aside for it.
    """
    query = "site:atb.nrel.gov utility scale battery overnight capital cost"
    state = _AgentState(budget=SourceBudget(10))
    state.trail.extend(_tested_trail(query, turn=1, reason="provider_degraded"))
    retrieval = _FakeRetrieval()

    captured, emit = _collect_events()
    await execute_search_turn(
        state,
        [query],
        4,
        retrieval_engine=retrieval,
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(5, 8),
        narrowed={
            query: NarrowedQuery(
                query=query,
                narrowed_from="utility scale battery overnight capital cost",
                added=("site:atb.nrel.gov",),
            )
        },
    )

    assert [req.query for req in retrieval.requests] == [query]
