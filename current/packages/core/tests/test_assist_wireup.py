"""Wire-through: the `assist` flag must SURVIVE from RouterAgent.step into the
CompletionRequest the router actually dispatches.

This is the gap the unit tests missed. `test_f1_provider_recovery.py` proves the
provider recovers tool calls *when* `req.assist is True`, and the field-existence
tests prove `CompletionRequest.assist` defaults to False — but nothing proved the
AGENT puts the operator's gate decision ONTO the request. F1 is dead code unless
RouterAgent.step(assist=...) flows the value through. These tests close that hole
end-to-end: RouterAgent -> DefaultLLMRouter -> provider, asserting on the req the
provider actually `seen`."""

from __future__ import annotations

import pytest
from disco.core.llm import DefaultLLMRouter, OperatingMode, OverflowSignal
from disco.core.llm.policy import Difficulty
from disco.core.loop import RouterAgent
from disco.core.view import View
from llm_fakes import simple_config
from loop_fakes import SequenceProvider

pytestmark = pytest.mark.asyncio


def _agent_and_provider(text="hello"):
    """A RouterAgent whose router forwards to a SequenceProvider we can inspect.
    The provider's `seen` list captures every CompletionRequest dispatched, so
    `seen[-1].assist` is the value that actually reached the wire."""
    provider = SequenceProvider([{"text": text}])
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    return RouterAgent(router), provider


async def _step(agent, *, assist):
    return await agent.step(
        View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0),
        [],
        mode=OperatingMode.INTERACTIVE,
        overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
        assist=assist,
    )


async def test_step_assist_true_reaches_request():
    """assist=True at the step call → req.assist=True on the wire. This is the
    F1-critical path: without it the recovery layer never fires for a weak model."""
    agent, provider = _agent_and_provider()
    await _step(agent, assist=True)
    assert provider.seen, "router never dispatched a request"
    assert provider.seen[-1].assist is True


async def test_step_assist_false_reaches_request():
    """assist=False → req.assist=False → byte-identical behavior to today (no
    recovery). Proves the gate genuinely OFF, not merely defaulted."""
    agent, provider = _agent_and_provider()
    await _step(agent, assist=False)
    assert provider.seen[-1].assist is False


async def test_step_assist_defaults_off():
    """A caller that never passes `assist` (every legacy call site) gets False —
    capable models keep the recovery layer dormant by default."""
    agent, provider = _agent_and_provider()
    await agent.step(
        View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0),
        [],
        mode=OperatingMode.INTERACTIVE,
        overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
    )
    assert provider.seen[-1].assist is False
