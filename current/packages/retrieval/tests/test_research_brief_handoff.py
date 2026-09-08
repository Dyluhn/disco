"""Research brief revisions survive the model-to-writer and recovery boundaries."""

import json

import pytest
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from test_research_agent import _DONE, _TURN0, _FakeRetrieval, _run, _TurnRouter


@pytest.mark.parametrize("brief_update", ["revised", "omitted", "unchanged"])
async def test_current_brief_reaches_finish_and_recovery(brief_update: str) -> None:
    initial = json.loads(_TURN0)["brief"]
    done = json.loads(_DONE)
    if brief_update == "revised":
        done["brief"] = (
            "Evidence p0 limits the initial interpretation; the conclusion is qualified."
        )
    elif brief_update == "omitted":
        done.pop("brief")
    else:
        done["brief"] = initial
    expected = done.get("brief", initial)
    router = _TurnRouter([_TURN0, json.dumps(done)])
    retrieval = _FakeRetrieval()
    outcome, captured = await _run(router, retrieval)

    assert outcome.brief == expected
    briefs = [payload["text"] for kind, payload in captured if kind == "brief"]
    assert briefs == ([initial, expected] if brief_update == "revised" else [initial])
    restored = _AgentState(budget=SourceBudget(10))
    restored.trail = outcome.trail.copy()
    restored.restore_trail_state()
    assert restored.brief == expected
    assert [request.query for request in retrieval.requests] == ["X overview", "X criticism"]
    assert len(router.requests) == 2
    assert outcome.bounded_by is None
