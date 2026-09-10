"""A stopped complete-value response can omit a final container delimiter."""

import json

import pytest
from disco.retrieval.deep_research._output_ceiling import TurnCeiling
from disco.retrieval.deep_research._turn_protocol import _one_model_turn, _parse_turn
from test_research_agent import _TurnRouter


def complete_decision():
    return {
        "brief": 'Read the quoted note: "]}" and "literal" markers.',
        "decision_summary": "The evidence supports a qualified answer.",
        "coverage": {
            "covered": [
                {
                    "angle": "scope",
                    "finding": "Only the tested conditions were measured.",
                    "evidence_ids": ["p1"],
                }
            ],
            "open": [],
            "contradictions_checked": ["The alternative remains unmeasured."],
        },
        "queries": [],
        "ready_to_write": True,
    }


async def test_stopped_missing_final_brace_keeps_complete_values_without_a_reask():
    expected = complete_decision()
    text = json.dumps(expected)[:-1]
    router = _TurnRouter([text])
    result = await _one_model_turn(
        router,
        "research",
        "question",
        expect_brief=True,
        namespace="closure",
        ceiling=TurnCeiling(12000),
    )
    assert result.turn is not None
    assert result.turn.brief == expected["brief"]
    assert result.turn.coverage == expected["coverage"]
    assert result.turn.ready_to_write
    assert result.turn.queries == ()
    assert len(router.requests) == 1
    assert any("closing" in repair for repair in result.turn.repairs)


async def test_closure_and_existing_misplaced_key_repair_compose():
    expected = complete_decision()
    expected["coverage"]["ready_to_write"] = expected.pop("ready_to_write")
    text = json.dumps(expected)[:-1]
    router = _TurnRouter([text])
    result = await _one_model_turn(
        router,
        "research",
        "question",
        expect_brief=True,
        namespace="closure",
        ceiling=TurnCeiling(12000),
    )
    assert result.turn is not None and result.turn.ready_to_write
    assert len(router.requests) == 1
    assert len(result.turn.repairs) == 2


@pytest.mark.parametrize("ending", [",", ":", '"unterminated', "]"])
async def test_incomplete_or_mismatched_values_still_require_model_repair(ending):
    invalid = json.dumps(complete_decision())[:-1] + ending
    valid = json.dumps(complete_decision())
    router = _TurnRouter([invalid, valid])
    result = await _one_model_turn(
        router,
        "research",
        "question",
        expect_brief=True,
        namespace="closure",
        ceiling=TurnCeiling(12000),
    )
    assert result.turn is not None
    assert len(router.requests) == 2


async def test_provider_length_finish_never_applies_a_reconstructed_decision():
    expected = complete_decision()

    class TruncatedFirst(_TurnRouter):
        async def complete(self, request, *, context=None):
            response = await super().complete(request, context=context)
            if len(self.requests) == 1:
                return response.model_copy(update={"finish_reason": "length"})
            return response

    router = TruncatedFirst([json.dumps(expected)[:-1], json.dumps(expected)])
    result = await _one_model_turn(
        router,
        "research",
        "question",
        expect_brief=True,
        namespace="closure",
        ceiling=TurnCeiling(12000),
    )
    assert result.turn is not None
    assert len(router.requests) == 2


def test_valid_json_with_a_quoted_code_fence_is_parsed_before_unwrapping():
    expected = complete_decision()
    expected["brief"] = (
        "An example contains a literal \u0060\u0060\u0060json null \u0060\u0060\u0060 marker."
    )
    parsed, error = _parse_turn(json.dumps(expected), expect_brief=True)
    assert error is None
    assert parsed.brief == expected["brief"]
