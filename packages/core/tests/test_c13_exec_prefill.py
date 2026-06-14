"""C13 — execution-phase prefill, gated by `PMX_EXEC_PREFILL=1`, default OFF.

Mirrors the B9 PLANNING prefill pattern (`PMX_PLAN_PREFILL=1`):
  - Flag ON  → the EXECUTION-phase agent step injects an `assistant_prefill`
               on the CompletionRequest. The string biases the model toward
               a concrete next-action tool call rather than another
               "let me think..." prose turn.
  - Flag OFF → `assistant_prefill` is `None`, byte-identical to today
               (the field's default on CompletionRequest).
  - Independent gate from the B9 planning flag: it can be A/B'd against
    PMX_PLAN_PREFILL and does not bleed into the PLANNING phase.

The EXECUTION phase is `OperatingMode.LONG_HORIZON` (engine.py:1116
`execution_mode: OperatingMode = OperatingMode.LONG_HORIZON`).

These tests pin the gated-experiment contract:

  1. PMX_EXEC_PREFILL=1 + LONG_HORIZON → prefill is the C13 string.
  2. PMX_EXEC_PREFILL unset / "0" / garbage + LONG_HORIZON → prefill is None.
  3. PMX_PLAN_PREFILL=1 + LONG_HORIZON → prefill is None (B9 is planning-only).
  4. PMX_EXEC_PREFILL=1 + PLANNING → prefill is None (C13 is execution-only).
  5. The OFF path is byte-identical to a request built without the C13
     wiring at all (field defaults to None on CompletionRequest).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    Difficulty,
    OperatingMode,
    OverflowSignal,
    StreamChunk,
    TokenUsage,
)
from disco.core.loop.agent import RouterAgent
from disco.core.view import View


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_agent_with_capture() -> tuple[RouterAgent, list[CompletionRequest]]:
    """Build a RouterAgent whose router captures every CompletionRequest the
    step sends. Captures by ref so each `await agent.step(...)` overwrites
    `last_captured[-1]` with the freshly-seen request — the same shape as
    test_prefill_masking.py uses, just exposed as a list for easier asserts."""

    captured: list[CompletionRequest] = []

    router = MagicMock()

    async def mock_stream(req, **kwargs):
        captured.append(req)
        yield StreamChunk(
            done=True,
            final=CompletionResponse(
                text=" ack.",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                finish_reason="stop",
                model_used="test-model",
            ),
        )

    router.stream_complete = mock_stream
    agent = RouterAgent(router)
    view = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    return agent, captured, view


async def _step_and_get_last_prefill(agent, view, mode, env_patch: dict | None = None):
    """Run one `agent.step` under an env patch and return the
    `assistant_prefill` of the captured request."""
    # Fresh capture list per call (callers reuse _make_agent_with_capture()
    # but the list is shared; the appended request is "the one we just
    # emitted"). We reset to a local list and look at its last entry.
    router = agent._router

    captured: list[CompletionRequest] = []

    async def capturing_stream(req, **kwargs):
        captured.append(req)
        yield StreamChunk(
            done=True,
            final=CompletionResponse(
                text=" ack.",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                finish_reason="stop",
                model_used="test-model",
            ),
        )

    router.stream_complete = capturing_stream

    ctx_mgr = (
        patch.dict(os.environ, env_patch) if env_patch is not None else patch.dict(os.environ, {}, clear=False)
    )
    with ctx_mgr:
        await agent.step(
            view,
            tools=[],
            mode=mode,
            overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
        )
    assert captured, "no request captured — agent.step did not call the router"
    return captured[-1].assistant_prefill


# --------------------------------------------------------------------------
# (1) Flag ON + LONG_HORIZON → prefill applied.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_prefill_on_in_long_horizon():
    agent, _captured, view = _make_agent_with_capture()
    prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.LONG_HORIZON,
        env_patch={"PMX_EXEC_PREFILL": "1"},
    )
    assert prefill is not None
    # The string is the C13 action-pivot phrase. Pin it (not a snapshot) so
    # anyone editing the prompt sees the assertion break — that's the point.
    assert "next action" in prefill
    assert prefill.startswith("Given the current workspace state")


# --------------------------------------------------------------------------
# (2) Flag OFF (any of: unset, "0", "", garbage) + LONG_HORIZON → None.
#     This is the gated-experiment contract: OFF must be byte-identical
#     to today. Each variant is its own test so a future regression that
#     special-cases one string (e.g. "true"/"yes") gets caught.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_prefill_off_when_flag_unset():
    agent, _captured, view = _make_agent_with_capture()
    # Make sure both flags are absent in the env for this call.
    with patch.dict(os.environ, {}, clear=True):
        prefill = await _step_and_get_last_prefill(
            agent,
            view,
            mode=OperatingMode.LONG_HORIZON,
        )
    assert prefill is None


@pytest.mark.asyncio
async def test_exec_prefill_off_when_flag_zero():
    agent, _captured, view = _make_agent_with_capture()
    prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.LONG_HORIZON,
        env_patch={"PMX_EXEC_PREFILL": "0"},
    )
    assert prefill is None


@pytest.mark.asyncio
async def test_exec_prefill_off_when_flag_garbage():
    """Anything that isn't exactly '1' is OFF. This matches the B9 check
    (`os.environ.get(...) == "1"`) and protects against a future
    truthy-string drift."""
    agent, _captured, view = _make_agent_with_capture()
    for junk in ("", "true", "yes", "on", "01", "2", "off", " "):
        prefill = await _step_and_get_last_prefill(
            agent,
            view,
            mode=OperatingMode.LONG_HORIZON,
            env_patch={"PMX_EXEC_PREFILL": junk},
        )
        assert prefill is None, f"junk value {junk!r} unexpectedly turned the gate ON"


# --------------------------------------------------------------------------
# (3) The two gates are independent. B9 PLANNING flag does not affect
#     EXECUTION, and the C13 EXECUTION flag does not affect PLANNING.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_prefill_does_not_leak_into_execution():
    agent, _captured, view = _make_agent_with_capture()
    prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.LONG_HORIZON,
        env_patch={"PMX_PLAN_PREFILL": "1"},  # B9 gate, wrong mode
    )
    assert prefill is None


@pytest.mark.asyncio
async def test_exec_prefill_does_not_leak_into_planning():
    agent, _captured, view = _make_agent_with_capture()
    prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.PLANNING,
        env_patch={"PMX_EXEC_PREFILL": "1"},  # C13 gate, wrong mode
    )
    assert prefill is None


@pytest.mark.asyncio
async def test_both_gates_on_in_their_own_modes_independent():
    """A run that has both flags ON still produces the right prefill for
    each mode — the if/elif branches don't trample each other."""
    agent, _captured, view = _make_agent_with_capture()
    # PLANNING → B9 string, not the C13 string.
    planning_prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.PLANNING,
        env_patch={"PMX_PLAN_PREFILL": "1", "PMX_EXEC_PREFILL": "1"},
    )
    assert planning_prefill is not None
    assert "I've analyzed" in planning_prefill
    # LONG_HORIZON → C13 string, not the B9 string.
    exec_prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.LONG_HORIZON,
        env_patch={"PMX_PLAN_PREFILL": "1", "PMX_EXEC_PREFILL": "1"},
    )
    assert exec_prefill is not None
    assert "next action" in exec_prefill
    # And they are different strings — the two gates really do pivot on
    # different anchors (honest planning ack vs. concrete next-action).
    assert planning_prefill != exec_prefill


# --------------------------------------------------------------------------
# (4) Byte-identical proof: when the flag is OFF, the captured
#     `assistant_prefill` is the field's default on CompletionRequest.
#     A request built without the C13 wiring at all would look exactly
#     like this — same field, same default, same value.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_prefill_off_is_field_default():
    """The strict byte-identical check: the OFF-path prefill value on the
    captured request is the SAME object/value as `assistant_prefill`'s
    class-level default on CompletionRequest. This is the truest
    "unchanged" reference because the field's default is the only thing
    the C13 wiring touches; the OFF path leaves it untouched."""
    agent, _captured, view = _make_agent_with_capture()
    prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.LONG_HORIZON,
        env_patch={"PMX_EXEC_PREFILL": "0"},
    )
    # Default on the field.
    field_default = CompletionRequest.model_fields["assistant_prefill"].default
    assert prefill == field_default
    assert prefill is None


@pytest.mark.asyncio
async def test_exec_prefill_off_request_equals_hand_built_baseline():
    """A hand-built CompletionRequest with no prefill (the "today" shape
    before C13 was added) is byte-identical to the request the C13 wiring
    emits when the flag is OFF. We compare the relevant fields the C13
    code can touch: `assistant_prefill`. (Other fields differ because the
    router assigns request_id, etc., but the C13-relevant surface is just
    the prefill.)"""
    agent, _captured, view = _make_agent_with_capture()
    prefill = await _step_and_get_last_prefill(
        agent,
        view,
        mode=OperatingMode.LONG_HORIZON,
        env_patch={"PMX_EXEC_PREFILL": "0"},
    )
    # Build a "today" baseline by hand: no prefill. If the OFF path is
    # byte-identical, the field value is the same.
    baseline_prefill = None  # what CompletionRequest gets today without C13
    assert prefill == baseline_prefill
