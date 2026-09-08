"""The runaway-generation wall on deep-research model calls.

Observed live: a hosted model entered a degenerate generation loop on a research
turn and streamed real token chunks for over 80 minutes on ONE call. It escaped
every existing bound — research turns set no `max_tokens` so thinking is never
capped, the progress-based idle detector was satisfied by the real chunks, and
the turn budget only ticks between turns.

These tests pin the wall and its failure handling: the ceiling is on every
otherwise-unbounded call, a ceiling hit is ONE malformed turn carrying a precise
re-ask (never a fatal error on its own), it counts toward the existing
3-consecutive-malformed bound, and it leaves one audit row so the harness can
count runaway events. The sweep at the bottom keeps the hole from reopening.
"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import agent as agent_mod
from disco.retrieval.deep_research import decompose as decompose_mod
from disco.retrieval.deep_research._output_ceiling import (
    OUTPUT_CEILING_REASK,
    research_turn_max_tokens,
)
from disco.retrieval.deep_research.agent import ResearchAgentError
from test_research_agent import _READY, _TURN0, _bound, _FakeRetrieval, _run

DEFAULT_CEILING = 24_000

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

# What a runaway actually looks like on the wire: the model stopped producing a
# turn and started producing tokens, and the provider cut it off at the ceiling.
_RUNAWAY_TEXT = '{"brief": "The question asks about X' + " and also and also" * 40


class _CeilingRouter(LLMRouter):
    """Scripted `(text, finish_reason)` responses, so a test can produce a call
    the ceiling cut off rather than one the model ended."""

    def __init__(self, responses: list[tuple[str, FinishReason]]) -> None:
        self._responses = list(responses)
        self.requests: list[CompletionRequest] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        self.requests.append(request)
        text, finish_reason = self._responses.pop(0) if self._responses else (_READY, "stop")
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
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


def _runaway(count: int) -> list[tuple[str, FinishReason]]:
    return [(_RUNAWAY_TEXT, "length")] * count


def _ceiling_rows(trail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in trail if entry.get("kind") == "output_ceiling"]


# ---- the ceiling is on the call ---------------------------------------------


async def test_research_turn_request_carries_the_default_output_ceiling() -> None:
    """Every research-turn call ships a ceiling — the defect was `None` here."""
    router = _CeilingRouter([(_TURN0, "stop"), (_READY, "stop")])

    await _run(router, _FakeRetrieval())

    assert router.requests
    assert all(request.max_tokens == DEFAULT_CEILING for request in router.requests)


async def test_output_ceiling_env_override_is_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_RESEARCH_TURN_MAX_TOKENS", "31337")
    router = _CeilingRouter([(_TURN0, "stop"), (_READY, "stop")])

    await _run(router, _FakeRetrieval())

    assert all(request.max_tokens == 31337 for request in router.requests)


def test_the_legacy_env_prefix_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PMX_RESEARCH_TURN_MAX_TOKENS", "9000")
    assert research_turn_max_tokens() == 9000


@pytest.mark.parametrize("raw", ["", "  ", "lots", "0", "-1"])
def test_a_junk_or_uncapping_knob_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A typo'd knob must never fail a run — and must never remove the wall."""
    monkeypatch.setenv("DISCO_RESEARCH_TURN_MAX_TOKENS", raw)
    assert research_turn_max_tokens() == DEFAULT_CEILING


async def test_the_plan_decomposition_call_carries_the_same_ceiling() -> None:
    router = _CeilingRouter([("first probe\nsecond probe", "stop")])

    await decompose_mod.decompose_query(router, "the state of X", max_subq=2)

    assert [request.max_tokens for request in router.requests] == [DEFAULT_CEILING]


# ---- a ceiling hit is bounded failure, not a fatal one -----------------------


async def test_a_runaway_turn_is_one_malformed_turn_with_the_ceiling_reask() -> None:
    """The wall fires, the model is told precisely what happened, it answers the
    re-ask correctly, and the run carries on — one turn spent, not a dead run."""
    router = _CeilingRouter([*_runaway(1), (_TURN0, "stop"), (_READY, "stop")])

    outcome, _ = await _run(router, _FakeRetrieval())

    reask = router.requests[1].messages[-1].content
    assert OUTPUT_CEILING_REASK in reask
    assert "exceeded the output ceiling" in reask
    assert "answer ONLY with the JSON turn format" in reask
    assert '"brief"' in reask  # the first-turn schema still rides along
    assert outcome.brief and outcome.bounded_by is None
    assert len(outcome.passages) == 4


async def test_a_runaway_that_happens_to_parse_is_still_discarded() -> None:
    """`finish_reason == "length"` is authoritative.

    The turn parser scans for the outermost braces, so a loop repeating
    JSON-shaped text can yield a "parseable" turn out of a generation that never
    terminated. At this cap no honest turn reaches the wall, so a cut-off
    response is a runaway whatever its bytes decode to — accepting one would let
    a looping model steer the investigation.
    """
    router = _CeilingRouter([(_TURN0, "length"), (_TURN0, "stop"), (_READY, "stop")])

    outcome, _ = await _run(router, _FakeRetrieval())

    assert OUTPUT_CEILING_REASK in router.requests[1].messages[-1].content
    assert _ceiling_rows(outcome.trail) == [
        {
            "kind": "output_ceiling",
            "stage": "research_turn",
            "turn": 0,
            "tokens": DEFAULT_CEILING,
        }
    ]


async def test_three_consecutive_runaway_turns_raise_the_research_agent_error() -> None:
    """A model looping three turns in a row IS a driver failure: the run errors
    loudly rather than shipping a report built on nothing."""
    router = _CeilingRouter(_runaway(6))  # 3 turns × (attempt + re-ask)

    with pytest.raises(ResearchAgentError, match="could not sustain the turn protocol"):
        await _run(router, _FakeRetrieval())

    assert len(router.requests) == 6


async def test_a_recovered_runaway_does_not_burn_the_malformed_streak() -> None:
    """The streak counts CONSECUTIVE failures — a run that recovers, runs away
    again, and recovers again is not a provider failure."""
    router = _CeilingRouter(
        [
            *_runaway(1),
            (_TURN0, "stop"),
            *_runaway(2),  # a whole turn lost: attempt + re-ask
            (_READY, "stop"),
        ]
    )

    outcome, _ = await _run(router, _FakeRetrieval())

    assert outcome.bounded_by is None
    assert len(_ceiling_rows(outcome.trail)) == 2


# ---- telemetry ---------------------------------------------------------------


async def test_one_trail_row_per_runaway_turn_names_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row per turn that hit the wall — not one per call — carrying the cap
    actually in force, so the harness can count runaway events."""
    monkeypatch.setenv("DISCO_RESEARCH_TURN_MAX_TOKENS", "1200")
    router = _CeilingRouter([*_runaway(2), (_TURN0, "stop"), (_READY, "stop")])

    outcome, _ = await _run(router, _FakeRetrieval())

    assert _ceiling_rows(outcome.trail) == [
        {"kind": "output_ceiling", "stage": "research_turn", "turn": 0, "tokens": 1200}
    ]
    # …and the turn it cost is still recorded as the malformed turn it was.
    assert [entry["turn"] for entry in outcome.trail if entry.get("kind") == "malformed"] == [0]


async def test_no_ceiling_row_on_an_ordinary_run() -> None:
    """Including a run with an ordinary parse failure: an unusable response the
    model ENDED is not a runaway, and must not be counted as one."""
    router = _CeilingRouter([("not json at all", "stop"), (_TURN0, "stop"), (_READY, "stop")])

    outcome, _ = await _run(router, _FakeRetrieval(), bound=_bound())

    assert _ceiling_rows(outcome.trail) == []
    assert "could not be used" in router.requests[1].messages[-1].content
    assert OUTPUT_CEILING_REASK not in router.requests[1].messages[-1].content
    assert outcome.bounded_by is None


# ---- the sweep: the hole cannot reopen --------------------------------------


_PACKAGE_DIR = Path(agent_mod.__file__).parent


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _completion_request_calls() -> Iterator[tuple[Path, ast.Call]]:
    for path in sorted(_PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == "CompletionRequest":
                yield path, node


def _bounded(call: ast.Call) -> bool:
    """True iff this construction statically pins a non-None `max_tokens`.

    A `**kwargs` splat is NOT provably bounded and fails the sweep: an
    unverifiable ceiling is exactly how the original hole was opened.
    """
    for keyword in call.keywords:
        if keyword.arg is None:
            return False
        if keyword.arg == "max_tokens":
            value = keyword.value
            return not (isinstance(value, ast.Constant) and value.value is None)
    return False


def test_no_deep_research_completion_request_ships_unbounded() -> None:
    """Every model call in this package carries an output ceiling.

    Static, not behavioural: a new call site added without `max_tokens` fails
    here even if no test ever drives it.
    """
    calls = list(_completion_request_calls())

    assert len(calls) >= 4, "the sweep found no call sites — it stopped scanning"
    unbounded = [
        f"{path.relative_to(_PACKAGE_DIR)}:{call.lineno}"
        for path, call in calls
        if not _bounded(call)
    ]
    assert unbounded == []
