"""An inspection repair retains its reply and every still-valid action."""

import json

import pytest
from _writer_doubles import RecordingRouter
from disco.retrieval.deep_research._output_ceiling import TurnCeiling
from disco.retrieval.deep_research._turn_protocol import _one_model_turn


@pytest.mark.parametrize(
    "next_action",
    [
        {"inspect": [{"source_id": "record", "find": "network file"}]},
        {"queries": ["primary network filesystem restrictions"]},
        {"ready_to_write": True},
    ],
)
async def test_inspection_error_preserves_response_and_available_actions(next_action):
    common = {
        "decision_summary": "Read the retained network restriction before writing.",
        "coverage": {
            "covered": [{"angle": "host requirement", "evidence_ids": ["record"]}],
            "open": ["network qualification"],
            "contradictions_checked": [],
        },
        "queries": [],
        "ready_to_write": False,
    }
    invalid = json.dumps(
        {
            **common,
            "inspect": [{"source_id": "record", "find": "network file", "start": -1}],
        }
    )
    corrected = json.dumps({**common, **next_action})
    router = RecordingRouter([invalid, corrected])
    result = await _one_model_turn(
        router,
        "Use the research action contract.",
        "One source retained; four turns remain.",
        expect_brief=False,
        namespace="inspection-recovery",
        ceiling=TurnCeiling(8000),
    )
    assert result.turn is not None and result.error is None
    assert router.calls == 2
    messages = router.requests[1].messages
    assert messages[-2].role == "assistant" and messages[-2].content == invalid
    feedback = messages[-1].content
    assert '"start" must be a non-negative character offset' in feedback
    assert '"inspect"' in feedback and '"queries"' in feedback
    assert '"ready_to_write": true' in feedback
    assert "No action from it was applied" in feedback
    assert "matching exactly this schema" not in feedback


@pytest.mark.parametrize("expect_brief", [False, True])
def test_initial_and_repair_contracts_offer_the_same_three_parseable_actions(expect_brief):
    from disco.retrieval.deep_research._source_lookup import SOURCE_LOOKUP_INSTRUCTION
    from disco.retrieval.deep_research._turn_protocol import (
        _Attempt,
        _parse_turn,
        _reask,
        _turn_response_format,
    )
    from disco.retrieval.deep_research.agent import _system_prompt

    contract = _turn_response_format()
    assert SOURCE_LOOKUP_INSTRUCTION in contract
    assert "Do not combine find" not in contract
    assert contract in _system_prompt(None)
    assert contract in _reask(
        _Attempt(None, "invalid inspection", None, "malformed_turn"), expect_brief=expect_brief
    )
    examples = [json.loads(line) for line in contract.splitlines() if line.startswith("{")]
    assert len(examples) == 3
    turns = [_parse_turn(json.dumps(row), expect_brief=expect_brief) for row in examples]
    assert all(turn is not None and error is None for turn, error in turns)
    assert [bool(turn.queries) for turn, _ in turns] == [True, False, False]
    assert [bool(turn.inspections) for turn, _ in turns] == [False, True, False]
    assert [turn.ready_to_write for turn, _ in turns] == [False, False, True]
