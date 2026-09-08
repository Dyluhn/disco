"""A researcher can read admitted evidence without expanding search or turn budgets."""

import json

import pytest
from disco.retrieval.deep_research._source_inspection import EXCERPT_CHARS
from disco.retrieval.deep_research._source_lookup import locate_source
from disco.retrieval.deep_research._turn_accounting import turn_accounting_rollup
from disco.retrieval.deep_research._turn_protocol import _parse_turn
from disco.retrieval.source_excerpts import relevant_excerpt
from test_research_agent import _bound, _FakeRetrieval, _passage, _run, _TurnRouter


def request(source_id="p1", **fields):
    return json.dumps(
        {
            "brief": "Check the qualification in the original evidence.",
            "decision_summary": "Read the limitations before choosing the next search.",
            "coverage": {"covered": [], "open": ["limits"], "contradictions_checked": []},
            "queries": [],
            "ready_to_write": False,
            "inspect": [{"source_id": source_id, **fields}],
        }
    )


async def test_inspection_reaches_late_text_and_costs_the_existing_turn_budget():
    text = "General introduction. " * 250 + "Critical qualification: network use is excluded."
    source = _passage(1).model_copy(update={"text": text})
    router = _TurnRouter([request(start=4500), request(start=0), request(start=2200)])
    retrieval = _FakeRetrieval()
    outcome, _events = await _run(
        router,
        retrieval,
        bound=_bound(max_research_turns=3),
        upload_passages=[source],
    )
    assert outcome.bounded_by == "turns"
    assert retrieval.requests == []
    assert len(router.requests) == 3
    second_context = router.requests[1].messages[-1].content
    assert "Critical qualification: network use is excluded." in second_context
    rows = [row for row in outcome.trail if row["kind"] == "source_inspection"]
    assert all(row["text"] == text[row["start"] : row["end"]] for row in rows)
    assert all(len(row["text"]) <= EXCERPT_CHARS for row in rows)
    assert len(outcome.passages) == 1
    assert turn_accounting_rollup(outcome.trail, total_turns=3) == {
        "model_turns": 3,
        "degraded_turns": 0,
        "refused_turns": 0,
        "inspection_turns": 3,
        "of": 3,
    }


async def test_unknown_source_and_out_of_range_offset_return_bounded_errors_without_fetching():
    router = _TurnRouter([request("https://unadmitted.test"), request(start=999999)])
    retrieval = _FakeRetrieval()
    outcome, _ = await _run(
        router,
        retrieval,
        bound=_bound(max_research_turns=2),
        upload_passages=[_passage(1)],
    )
    rows = [row for row in outcome.trail if row["kind"] == "source_inspection"]
    assert len(rows) == 2 and all(not row["ok"] for row in rows)
    assert "not in the admitted pool" in rows[0]["error"]
    assert "outside the source" in rows[1]["error"]
    assert retrieval.requests == []


@pytest.mark.parametrize(
    "changes",
    [
        {"start": -1},
        {"start": True},
        {"focus": "x" * 301},
    ],
)
def test_malformed_inspection_is_a_protocol_error(changes):
    turn, error = _parse_turn(request(**changes), expect_brief=True)
    assert turn is None and error


def test_inspection_cannot_claim_readiness_before_reading_the_result():
    payload = json.loads(request())
    payload["ready_to_write"] = True
    turn, error = _parse_turn(json.dumps(payload), expect_brief=True)
    assert turn is None and "read the result next turn" in error


def test_specific_quantity_is_not_replaced_by_scattered_date_and_unit_matches():
    text = (
        "Published on day 10. Storage capacity is measured in MWh. Station connected. "
        + "General introduction. " * 12
        + "An earlier 10 MWh station connected in May. This is a separate operating installation. "
        + "Operational details. " * 8
    )
    excerpt = relevant_excerpt(text, "10 MWh station connected", max_chars=180)
    assert "earlier 10 MWh station" in excerpt.text
    assert excerpt.text == text[excerpt.start : excerpt.end]


async def test_unlinked_coverage_gets_feedback_before_requesting_readiness():
    payload = json.loads(request())
    payload["coverage"]["covered"] = ["network restriction"]
    router = _TurnRouter([json.dumps(payload), request()])
    retrieval = _FakeRetrieval()
    await _run(router, retrieval, bound=_bound(max_research_turns=2), upload_passages=[_passage(1)])
    context = router.requests[1].messages[-1].content
    assert "COVERAGE FEEDBACK" in context
    assert "network restriction" in context
    assert '"evidence_ids": ["source ID"]' in context
    assert retrieval.requests == []


async def test_guidance_arriving_in_the_last_turn_is_retained_before_writing():
    pending = []
    router = _TurnRouter([request()])
    original = router.complete

    async def complete(*args, **kwargs):
        answer = await original(*args, **kwargs)
        pending.append("Keep the network limitation in the recommendation")
        return answer

    router.complete = complete

    def pop():
        items = list(pending)
        pending.clear()
        return items

    outcome, events = await _run(
        router,
        _FakeRetrieval(),
        bound=_bound(max_research_turns=1),
        upload_passages=[_passage(1)],
        pop_steers=pop,
    )
    assert any(
        row.get("kind") == "steer" and "network limitation" in row["text"] for row in outcome.trail
    )
    assert pending == []


async def test_literal_lookup_keeps_context_offsets_and_original_turn_allowance():
    import hashlib

    text = "Unrelated introduction. " * 300 + "α result 1.2 units. " + "Details. " * 300
    text += "The bibliography DOI contains 1.2 but is not a measurement."
    source = _passage(1).model_copy(update={"text": text})
    router = _TurnRouter(
        [
            request(find="1.2", focus="Check the measured result", start=None),
            request(start=text.index("α result"), focus="Read the qualification", find=""),
        ]
    )
    retrieval = _FakeRetrieval()
    outcome, _ = await _run(
        router, retrieval, bound=_bound(max_research_turns=2), upload_passages=[source]
    )
    rows = [row for row in outcome.trail if row["kind"] == "source_inspection"]
    found = rows[0]
    assert found["find"] == "1.2" and len(found["matches"]) == 2
    assert found["matches"][0]["offset"] == text.index("1.2")
    assert "α result 1.2 units" in found["matches"][0]["text"]
    assert "bibliography" in found["matches"][1]["text"]
    for row in rows:
        assert row["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert row["text"] == text[row["start"] : row["end"]]
        previews = row.get("matches", [])
        assert len(row["text"]) + sum(len(m["text"]) for m in previews) <= EXCERPT_CHARS
        assert all(m["text"] == text[m["start"] : m["end"]] for m in previews)
    assert retrieval.requests == [] and len(outcome.passages) == 1
    assert len(router.requests) == 2 and outcome.bounded_by == "turns"
    assert '"find": "1.2"' in router.requests[1].messages[-1].content


def test_literal_match_limit_and_case_sensitive_absence_are_truthful():
    from disco.retrieval.deep_research._source_inspection import Inspection, source_inspection_rows

    text = "Measured result 1.2 units. " * 100
    source = _passage(1).model_copy(update={"text": text})
    rows = source_inspection_rows(
        {"p1": source}, (Inspection("p1", find="1.2"), Inspection("p1", find="measured"))
    )
    found, absent = rows
    assert len(found["matches"]) == 8 and found["more_matches"] is True
    assert len(found["text"]) + sum(len(m["text"]) for m in found["matches"]) <= EXCERPT_CHARS
    assert absent["ok"] is False and absent["matches"] == []
    assert "case-sensitive" in absent["error"] and "text" not in absent


@pytest.mark.parametrize(
    "fields",
    [
        {"find": ""},
        {"find": " "},
        {"find": True},
        {"find": "x" * 301},
        {"find": "result", "focus": "result"},
        {"find": "result", "start": 0},
    ],
)
def test_ambiguous_or_empty_literal_lookup_is_actionable_protocol_error(fields):
    turn, error = _parse_turn(request(**fields), expect_brief=True)
    if fields in (
        {"find": ""},
        {"find": "result", "focus": "result"},
        {"find": "result", "start": 0},
    ):
        assert error is None and turn is not None
    else:
        assert turn is None and error and "find" in error


@pytest.mark.parametrize("fields", [{"find": "result"}, {"start": 0}, {"focus": "result"}])
def test_lookup_is_preserved_in_declared_research_decision(fields):
    from disco.retrieval.deep_research._turn_protocol import _turn_payload

    turn, error = _parse_turn(request(**fields), expect_brief=True)
    assert error is None
    payload = _turn_payload(turn)
    assert all(payload["inspect"][0][key] == value for key, value in fields.items())
    restored, error = _parse_turn(json.dumps(payload), expect_brief=True)
    assert error is None and restored == turn


@pytest.mark.parametrize("search_fails", [False, True])
async def test_search_and_inspection_share_one_bounded_turn(search_fails):
    payload = json.loads(request(focus="reported figures"))
    payload["queries"] = ["a new source for the comparison"]
    router = _TurnRouter([json.dumps(payload)])
    retrieval = _FakeRetrieval()
    original = retrieval.retrieve

    async def retrieve(*args, **kwargs):
        if search_fails:
            raise RuntimeError("provider transport unavailable")
        return await original(*args, **kwargs)

    retrieval.retrieve = retrieve
    outcome, _ = await _run(
        router, retrieval, bound=_bound(max_research_turns=1), upload_passages=[_passage(1)]
    )
    inspections = [row for row in outcome.trail if row["kind"] == "source_inspection"]
    assert len(inspections) == 1 and inspections[0]["ok"]
    assert len(router.requests) == 1
    assert outcome.bounded_by == "turns"
    assert turn_accounting_rollup(outcome.trail, total_turns=1)["model_turns"] == 1
    assert bool(retrieval.requests) is not search_fails


async def test_mixed_action_checkpoint_resumes_without_repeating_completed_work(
    tmp_path, monkeypatch
):
    from disco.retrieval.deep_research._recovery_state import AgentCheckpoint, RecoveryCheckpoint
    from disco.retrieval.deep_research.recovery import read_checkpoint, write_checkpoint
    from test_research_agent import _DONE

    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    payload = json.loads(request(focus="reported figures"))
    payload["queries"] = ["a new source for the comparison"]
    bound = _bound(max_research_turns=2)
    saved = []

    async def checkpoint(state, cursor):
        saved.append((AgentCheckpoint.capture(state).model_copy(deep=True), cursor.model_copy()))

    retrieval = _FakeRetrieval()
    stopped, _ = await _run(
        _TurnRouter([json.dumps(payload)]),
        retrieval,
        bound=bound,
        upload_passages=[_passage(1)],
        checkpoint=checkpoint,
        should_cancel=lambda: bool(saved and saved[-1][0].turns_charged == 1),
    )
    assert stopped.bounded_by == "stopped"
    state, cursor = saved[-1]
    recovery = RecoveryCheckpoint(
        conversation_id="conv_mixed",
        run_id="same_execution",
        query="the state of X",
        depth_tier="quick",
        stage="research",
        bound=bound,
        state=state,
        cursor=cursor,
    )
    restored = read_checkpoint(recovery.conversation_id, write_checkpoint(recovery))
    searches = len(retrieval.requests)
    resumed = _TurnRouter([_DONE])
    finished, _ = await _run(resumed, retrieval, bound=bound, recovery=restored)
    assert finished.bounded_by != "stopped"
    assert len(retrieval.requests) == searches
    inspections = [row for row in finished.trail if row["kind"] == "source_inspection"]
    assert len(inspections) == 1 and inspections[0]["ok"]
    assert turn_accounting_rollup(finished.trail, total_turns=2)["model_turns"] == 2
    assert "reported figures" in resumed.requests[0].messages[-1].content


@pytest.mark.parametrize("duplicate_request", [False, True])
async def test_inspection_evidence_survives_intervening_reads_without_duplicate_context(
    duplicate_request,
):
    from test_research_agent import _DONE

    first = "Unique first-source qualification. " + "background. " * 300
    second = "Unique second-source comparison. " + "other details. " * 300
    repeat = json.loads(request("p1", start=0))
    if duplicate_request:
        repeat["inspect"] *= 2
    router = _TurnRouter(
        [request("p1", start=0), request("p2", start=0), json.dumps(repeat), _DONE]
    )
    outcome, _ = await _run(
        router,
        _FakeRetrieval(),
        bound=_bound(max_research_turns=4),
        upload_passages=[
            _passage(1).model_copy(update={"text": first}),
            _passage(2).model_copy(update={"text": second}),
        ],
    )
    for call in router.requests[2:]:
        context = call.messages[-1].content.split("RETAINED SOURCE READS", 1)[1]
        assert context.count(first[:EXCERPT_CHARS]) == 1
        assert context.count(second[:EXCERPT_CHARS]) == 1
    assert len([row for row in outcome.trail if row["kind"] == "source_inspection"]) == 3 + int(
        duplicate_request
    )


async def test_literal_lookup_can_continue_after_an_earlier_match_within_the_same_budget():
    text = "First result: α. " + "Background. " * 300 + "Second result: β."
    second = text.index("Second result")
    router = _TurnRouter(
        [
            request(find="result", start=0),
            request(find="result", start=second),
            request(find="result", start=len(text) - 2),
        ]
    )
    retrieval = _FakeRetrieval()
    outcome, _ = await _run(
        router,
        retrieval,
        bound=_bound(max_research_turns=3),
        upload_passages=[_passage(1).model_copy(update={"text": text})],
    )
    rows = [row for row in outcome.trail if row["kind"] == "source_inspection"]
    assert [m["offset"] for m in rows[0]["matches"]] == [text.index("result"), second + 7]
    assert [m["offset"] for m in rows[1]["matches"]] == [second + 7]
    assert rows[1]["search_start"] == second
    assert rows[2]["ok"] is False and rows[2]["matches"] == []
    assert f"at or after character {len(text) - 2}" in rows[2]["error"]
    assert retrieval.requests == [] and outcome.bounded_by == "turns"
    assert len(router.requests) == 3
    assert turn_accounting_rollup(outcome.trail, total_turns=3)["inspection_turns"] == 3


# ---------------------------------------------------------------------------
# A reviewer's literal search reads the page, not the extraction's artefacts.
# ---------------------------------------------------------------------------


def test_literal_lookup_finds_a_figure_markdown_emphasis_wrapped() -> None:
    # Measured on Q4-GLM: both review rounds searched the Federal Register
    # passage for "207 kWh" and recorded absence, because the table cell reads
    # "207 _kWh/year_". The report then said the standards could not be
    # verified from a source that states them.
    text = "Table II.2\n| Electric Smooth Element | 207 _kWh/year_ |\n"

    start, metadata = locate_source(text, focus="", start=None, find="207 kWh", max_chars=2200)

    assert start is not None
    assert metadata["matches"]
    offset = metadata["matches"][0]["offset"]
    assert text[offset : offset + len("207 _kWh")] == "207 _kWh"


def test_literal_lookup_finds_a_word_the_extraction_split() -> None:
    text = "Overall, the PPs reduced the total volume of stormwater ou tflow by 42%."

    split, _ = locate_source(text, focus="", start=None, find="ou tflow by 42", max_chars=2200)
    whole, _ = locate_source(text, focus="", start=None, find="outflow by 42", max_chars=2200)

    assert split is not None
    assert whole == split


def test_a_truthful_absence_says_what_normalised_form_was_searched() -> None:
    text = "| Electric Smooth Element | 207 _kWh/year_ |"

    start, metadata = locate_source(text, focus="", start=None, find="207 MWh", max_chars=2200)

    assert start is None
    assert metadata["searched_normalised"] == "207MWh"
    assert "207MWh" in metadata["error"]
