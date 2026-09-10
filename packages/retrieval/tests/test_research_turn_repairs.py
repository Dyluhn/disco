"""Lane L22-c — the three malformed-turn clusters, fixed at their causes.

Every sample below is a VERBATIM research-turn reply from
`~/AI-Work/disco-research-acceptance-2026-08-30/torture/`, pasted with its
source path. Across all rounds the research turns produced three parse-error
clusters and no others:

* 37× ``"coverage" fields covered, open, and contradictions_checked must be
  arrays`` — 18 replies simply omitted ``contradictions_checked`` and 18 put it
  at the TOP level, one brace early. In every one of them ``covered`` and
  ``open`` WERE arrays, so the message also named the wrong two fields.
* 17× ``"ready_to_write" must be a boolean`` — 13 omitted the key and 4 closed
  it (with ``queries``) inside ``coverage``, one brace late.
* 8× the output-ceiling re-ask, 4 of them on replies with ZERO characters: the
  reasoning phase ate the whole allowance, and the re-ask went out with the
  same ceiling carrying a message about a response that did not exist.

Each turn above was otherwise complete — a decision, a coverage map and good
queries — and each cost a turn plus a re-ask.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research._exhaustion import malformed_turn_failure
from disco.retrieval.deep_research._output_ceiling import (
    EMPTY_TURN_REASK,
    RESEARCH_CEILING_CAP,
    TurnCeiling,
    research_turn_max_tokens,
    turn_failure_shape,
)
from disco.retrieval.deep_research.agent import _one_model_turn, _parse_turn

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

# ---- verbatim torture samples ----------------------------------------------

# round6-B/run-05/model_io.jsonl line 2 — `contradictions_checked` omitted.
_OMITTED_CONTRADICTIONS = (
    '{"decision_summary": "The first three searches failed due to a host '
    "extraction fault, not because the queries were bad. Per host feedback, I "
    "will re-issue the same three queries verbatim to map the core angles "
    '(vacancy, valuations, delinquencies) before adding conversion and '
    'regional-bank queries.", "coverage": {"covered": [], "open": ["US office '
    'vacancy rates 2020-2026 (actual data and forecasts)", "Office property '
    'valuation/price indices (Green Street, NCREIF, Moody\'s)", "Office loan '
    'delinquency rates (CMBS, bank portfolios)", "Office-to-residential '
    'conversion counts, policies, and outcomes", "Regional bank exposure to '
    'office CRE (loan concentrations, stress tests)", "Predictions vs actuals '
    'across all dimensions", "Independent criticism and negative results"]}, '
    '"queries": ["US office vacancy rate 2020 2021 2022 2023 2024 2025 CBRE JLL '
    'data", "US office property price index 2020-2025 Green Street NCREIF '
    'Moody\'s", "office loan delinquency rate CMBS 2023 2024 2025"], '
    '"ready_to_write": false}'
)

# round3-B/run-04/model_io.jsonl line 1 — `contradictions_checked` one brace
# early (top level), and `ready_to_write` omitted entirely.
_CONTRADICTIONS_AT_TOP_LEVEL = (
    '{"decision_summary": "Continue mapping the remaining major angles: '
    "climate/drought, earthquake-storm, systems collapse, and internal revolt. "
    'Issue targeted searches for each, prioritizing named scholars and primary '
    'evidence.", "coverage": {"covered": ["Sea Peoples theory and '
    'Egyptian/Ugaritic evidence (initial overview)"], "open": ["Climate/drought '
    'evidence (3.2 ka event, pollen, isotopes)", "Earthquake-storm theory (Amos '
    'Nur)", "Systems collapse and trade disruption (Tainter)", "Internal '
    'revolt/social unrest", "Multifactorial synthesis and criticisms", "Primary '
    'Egyptian inscriptions (Medinet Habu) and Ugaritic texts upstream"]}, '
    '"contradictions_checked": [], "queries": ["Late Bronze Age collapse '
    'drought 3.2 ka event Kaniewski pollen evidence", "Amos Nur earthquake '
    'storm Late Bronze Age collapse theory evidence", "Joseph Tainter collapse '
    'of complex societies Late Bronze Age systems collapse", "internal revolt '
    'social unrest Late Bronze Age collapse evidence"]}'
)

# round9-B/run-04/model_io.jsonl line 6 — `ready_to_write` omitted.
_OMITTED_READY = (
    '{"decision_summary": "I will now target the primary Sea Peoples textual '
    "evidence (Egyptian Medinet Habu inscriptions and the Ugarit letters RS "
    "34.129/RS 18.147), then move to seismic triggers and internal rebellion. I "
    "will also reserve a later turn for scholarly critiques and cross-validation "
    'of the drought evidence.", "coverage": {"covered": ["Systemic/palatial '
    'collapse model (Knapp & Manning 2016; Cline 2022)", "Climate '
    'change/drought/famine evidence (Kaniewski 2019)"], "open": ["Sea Peoples '
    'primary textual evidence (Egyptian and Ugaritic)", "Seismic/volcanic '
    'triggers", "Internal rebellion and socio-economic disruption", '
    '"Archaeological destruction layers at key sites", "Scholarly critiques and '
    'counterevidence"], "contradictions_checked": []}, "queries": ["Medinet '
    'Habu Sea Peoples inscription translation Ramesses III", "RS 34.129 Ugarit '
    'letter translation Hittite king", "earthquake storm Late Bronze Age '
    'collapse evidence Amos Nur", "Late Bronze Age collapse internal rebellion '
    'social unrest systems collapse"]}'
)

# round17-A/run-02/model_io.jsonl line 1 — `queries` and `ready_to_write` one
# brace late (inside `coverage`), with a stray tab and `}` after the object.
_NESTED_IN_COVERAGE = (
    "Mapped early US rulings — now filling the UK and EU statutory gaps before "
    'drilling into settlements.{"brief": "Question asks comparative legal '
    "treatment of using copyrighted works to train generative AI across US, UK, "
    "EU through mid-2026. Need to map statutory frameworks, regulator guidance, "
    "major court rulings, and settlements, then distinguish settled law vs open "
    'issues.", "decision_summary": "Have initial US district rulings and '
    "Copyright Office report leads; need to map EU DSM Directive TDM regime and "
    "AI Act copyright duties, and UK CDPA/consultation framework, plus key "
    "EU/UK case law (Hamburg LAION, Getty v Stability UK) before deeper "
    'settlement dive.", "coverage": {"covered": [], "open": ["US statutory '
    'framework and Copyright Office position on training as fair use", "UK CDPA '
    "TDM exception, 2024-2025 government consultation, and case law (Getty v "
    'Stability UK)"], "contradictions_checked": [], "queries": ["EU AI Act '
    'Article 53 GPAI copyright transparency obligation official text 2024", "UK '
    "IPO DSIT AI and copyright consultation December 2024 text and data mining "
    'exception outcome", "Hamburg Regional Court LAION Kneschke copyright AI '
    'training dataset ruling September 2024"], "ready_to_write": false}\t}'
)


# ---- cluster 1: a missing optional list is not a malformed turn -------------


def test_the_omitted_contradictions_list_parses_as_empty() -> None:
    turn, error = _parse_turn(_OMITTED_CONTRADICTIONS, expect_brief=False)

    assert error is None
    assert turn is not None
    assert turn.coverage["contradictions_checked"] == []
    assert len(turn.coverage["open"]) == 7
    assert turn.queries[0].startswith("US office vacancy rate 2020")
    assert turn.ready_to_write is False


def test_a_contradictions_list_braced_one_field_early_is_moved_back_in() -> None:
    turn, error = _parse_turn(_CONTRADICTIONS_AT_TOP_LEVEL, expect_brief=False)

    assert error is None
    assert turn is not None
    assert turn.coverage["contradictions_checked"] == []
    assert turn.coverage["covered"][0]["angle"].startswith("Sea Peoples theory")
    assert 'moved "contradictions_checked" into "coverage"' in turn.repairs[0]


def test_a_coverage_field_that_is_present_and_wrong_still_fails_by_name() -> None:
    """The precision the old message lost: name the ONE field, and what arrived."""
    turn, error = _parse_turn(
        '{"decision_summary": "d", "coverage": {"covered": [], "open": '
        '"vacancy", "contradictions_checked": []}, "queries": ["q"], '
        '"ready_to_write": false}',
        expect_brief=False,
    )

    assert turn is None
    assert error is not None
    assert '"coverage.open" must be a JSON array' in error
    assert "got str" in error
    assert "covered" not in error  # the field that was fine is not blamed


# ---- cluster 2: an omitted readiness flag means "not ready" ------------------


def test_the_omitted_readiness_flag_reads_as_not_ready() -> None:
    turn, error = _parse_turn(_OMITTED_READY, expect_brief=False)

    assert error is None
    assert turn is not None
    assert turn.ready_to_write is False
    assert len(turn.queries) == 3  # capped at _MAX_QUERIES_PER_TURN
    assert turn.repairs == ('read a missing "ready_to_write" as false',)


def test_queries_and_readiness_braced_one_field_late_are_lifted_out() -> None:
    turn, error = _parse_turn(_NESTED_IN_COVERAGE, expect_brief=True)

    assert error is None
    assert turn is not None
    assert turn.brief.startswith("Question asks comparative legal treatment")
    assert turn.ready_to_write is False
    assert turn.queries[0].startswith("EU AI Act Article 53")
    assert "queries" not in turn.coverage
    assert 'lifted "queries" out of "coverage"' in turn.repairs[0]
    assert 'lifted "ready_to_write" out of "coverage"' in turn.repairs[1]


def test_a_present_non_boolean_readiness_flag_still_fails_by_name() -> None:
    turn, error = _parse_turn(
        '{"decision_summary": "d", "coverage": {"covered": [], "open": [], '
        '"contradictions_checked": []}, "queries": ["q"], "ready_to_write": "yes"}',
        expect_brief=False,
    )

    assert turn is None
    assert error is not None
    assert '"ready_to_write" must be a boolean' in error
    assert "got str" in error


def test_a_real_value_is_never_overwritten_by_a_misplaced_one() -> None:
    """Repair only fills a hole; it never decides between two readings."""
    turn, error = _parse_turn(
        '{"decision_summary": "d", "coverage": {"covered": [], "open": [], '
        '"contradictions_checked": ["real"], "queries": ["nested"]}, '
        '"queries": ["outer"], "contradictions_checked": ["misplaced"], '
        '"ready_to_write": false}',
        expect_brief=False,
    )

    assert error is None
    assert turn is not None
    assert turn.queries == ("outer",)
    assert turn.coverage["contradictions_checked"] == ["real"]
    assert turn.repairs == ()


# ---- cluster 3: an empty reply gets the empty re-ask and more room -----------


class _ScriptedRouter(LLMRouter):
    """Scripted `(text, finish_reason, output_tokens)` replies; records requests."""

    def __init__(self, responses: list[tuple[str, FinishReason, int]]) -> None:
        self._responses = list(responses)
        self.requests: list[CompletionRequest] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        self.requests.append(request)
        text, finish_reason, output_tokens = (
            self._responses.pop(0) if self._responses else (_OMITTED_READY, "stop", 300)
        )
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=output_tokens),
            finish_reason=finish_reason,
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


async def test_an_empty_reply_at_the_ceiling_gets_the_empty_reask_and_more_room() -> None:
    """The measured defect (round3-B/run-02, round9-B/run-01 twice), inverted.

    A reply with zero characters at ``finish_reason="length"`` is not told its
    silence was a JSON problem, and the call that replaces it is not sent into
    the ceiling that just proved too small.
    """
    base = research_turn_max_tokens()
    router = _ScriptedRouter([("", "length", base)])
    ceiling = TurnCeiling.start()

    result = await _one_model_turn(
        router,
        "system",
        "user",
        expect_brief=False,
        namespace="conv",
        ceiling=ceiling,
    )

    assert result.turn is not None  # the re-ask, with room, produced a real turn
    first, retry = router.requests[0], router.requests[1]
    assert first.max_tokens == base
    assert retry.max_tokens is not None and retry.max_tokens > base
    reask = retry.messages[-1].content or ""
    assert EMPTY_TURN_REASK in reask
    assert "could not be used" not in reask
    assert "not valid JSON" not in reask


async def test_the_grown_ceiling_survives_into_the_next_turn() -> None:
    """A budget proven too small is never re-issued, this turn or any later one."""
    base = research_turn_max_tokens()
    ceiling = TurnCeiling.start()
    router = _ScriptedRouter([("", "length", base), ("", "length", base)])

    await _one_model_turn(
        router, "system", "user", expect_brief=False, namespace="conv", ceiling=ceiling
    )
    assert ceiling.tokens > base

    grown = ceiling.tokens
    router_two = _ScriptedRouter([])
    await _one_model_turn(
        router_two, "system", "user", expect_brief=False, namespace="conv", ceiling=ceiling
    )
    assert router_two.requests[0].max_tokens == grown


def test_a_complete_but_unusable_reply_keeps_its_ceiling() -> None:
    """More room is not what an off-schema reply was short of."""
    ceiling = TurnCeiling.start()
    ceiling.grow_after(finish_reason="stop", content_chars=900, output_tokens=250)
    assert ceiling.tokens == research_turn_max_tokens()


def test_the_ceiling_never_grows_past_its_cap() -> None:
    ceiling = TurnCeiling.start()
    for _ in range(50):
        ceiling.grow_after(finish_reason="length", content_chars=0, output_tokens=0)
    assert ceiling.tokens == RESEARCH_CEILING_CAP


@pytest.mark.parametrize(
    ("finish_reason", "content_chars", "shape"),
    [
        ("length", 0, "ceiling_hit"),
        ("stop", 0, "empty_response"),
        ("length", 1800, "truncated_turn"),
        ("stop", 1800, "malformed_turn"),
    ],
)
def test_the_failure_shape_reads_content_before_the_providers_self_report(
    finish_reason: str, content_chars: int, shape: str
) -> None:
    """Hosted providers disagree about which reason a starved reply reports."""
    assert (
        turn_failure_shape(finish_reason=finish_reason, content_chars=content_chars) == shape
    )


# ---- the terminal wall has an angle -----------------------------------------


def test_the_terminal_malformed_error_names_the_ceiling_lever_when_room_ran_out() -> None:
    failure = malformed_turn_failure(
        streak=3,
        reasks_per_turn=1,
        last_error="no turn arrived: the reply carried zero characters",
        last_shape="ceiling_hit",
        ceiling_tokens=36_000,
        sources_retained=0,
        turns_used=3,
        turns_total=8,
    )

    assert failure.failure_class == "model_protocol"
    assert "could not sustain the turn protocol" in failure.why
    assert "across 6 calls" in failure.why
    assert "no sources were admitted" in failure.state
    assert "3 of 8 research turns used" in failure.state
    assert "36000 tokens" in failure.next
    assert "DISCO_RESEARCH_TURN_MAX_TOKENS" in failure.next
    assert "stay on this conversation" in failure.allowed
    # The prose an operator reads is rendered FROM the fields, never beside them.
    assert failure.detail.startswith(failure.why)
    for part in (failure.state, failure.next, failure.allowed):
        assert part in failure.detail


def test_the_terminal_malformed_error_names_the_driver_when_the_shape_was_wrong() -> None:
    failure = malformed_turn_failure(
        streak=3,
        reasks_per_turn=1,
        last_error='"decision_summary" must be a non-empty string',
        last_shape="malformed_turn",
        ceiling_tokens=24_000,
        sources_retained=4,
        turns_used=3,
        turns_total=8,
    )

    assert "DISCO_RESEARCH_TURN_MAX_TOKENS" not in failure.next
    assert "strict-JSON protocol" in failure.next
    assert "model_io trace" in failure.next
    assert "4 sources are in the evidence pool" in failure.state
