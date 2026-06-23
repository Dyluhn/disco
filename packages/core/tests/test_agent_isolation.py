"""Research↔Build isolation: completion semantics are owned by the AGENT class
(ResearchAgent vs BuildAgent), not a shared mode flag. This is the structural
fix for the GAP B leak where a Build change silently broke Research."""

from __future__ import annotations

import pytest
from disco.core.llm import DefaultLLMRouter, OperatingMode, OverflowSignal
from disco.core.llm.policy import Difficulty
from disco.core.loop import BuildAgent, ResearchAgent, RouterAgent
from disco.core.view import View
from llm_fakes import simple_config
from loop_fakes import SequenceProvider

pytestmark = pytest.mark.asyncio


def _router(text="hello"):
    provider = SequenceProvider([{"text": text}])  # tool-less prose response
    return DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})


async def _step(agent, mode):
    return await agent.step(
        View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0),
        [],
        mode=mode,
        overflow_signal=OverflowSignal(difficulty=Difficulty.ROUTINE),
    )


# ---- the core isolation property --------------------------------------------


async def test_research_agent_prose_is_finished():
    # A tool-less prose turn IS the answer for Research — finished, in ANY mode.
    agent = ResearchAgent(_router("the answer is 42"))
    for mode in (OperatingMode.INTERACTIVE, OperatingMode.LONG_HORIZON):
        step = await _step(agent, mode)
        assert step.finished is True
        assert step.tool_call is None


async def test_build_agent_prose_is_NOT_finished():
    # A tool-less prose turn is just talk for Build — never finished, in ANY
    # mode. The run ends only via the `finish` tool. This is the property that
    # stops a mid-run reply from silently ending a build.
    agent = BuildAgent(_router("working on it…"))
    for mode in (OperatingMode.INTERACTIVE, OperatingMode.LONG_HORIZON):
        step = await _step(agent, mode)
        assert step.finished is False
        assert step.tool_call is None


async def test_completion_is_owned_by_class_not_mode():
    # The SAME mode yields OPPOSITE completion for the two agents — proving the
    # decision is the agent's, not the mode's. (Before the split, both shared
    # one `if mode == INTERACTIVE` line, so a Build change leaked into Research.)
    research = await _step(ResearchAgent(_router()), OperatingMode.LONG_HORIZON)
    build = await _step(BuildAgent(_router()), OperatingMode.LONG_HORIZON)
    assert research.finished is True
    assert build.finished is False


# ---- per-surface defaults ---------------------------------------------------


async def test_research_agent_is_deterministic_build_agent_jitters():
    assert ResearchAgent(_router())._temperature == 0.0  # grounded chat — no jitter
    assert BuildAgent(_router())._temperature > 0.0  # anti-fewshot on long runs


async def test_base_router_agent_back_compat_default():
    # Direct RouterAgent(router) callers keep the original v1 prose=finished
    # convention (so existing code/tests are unchanged).
    assert RouterAgent(_router())._prose_finishes is True


# ---- W-32 (secondary): a batched `finish` must not preempt real work --------


def _batched_router(tool_calls):
    from loop_fakes import SequenceProvider

    provider = SequenceProvider([{"text": "", "tool_calls": tool_calls}])
    return DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})


async def test_batched_finish_alongside_real_action_drops_finish():
    """W-32 secondary — a model that BATCHES `finish` ALONGSIDE a real action in
    ONE response must not let the finish preempt the work. RouterAgent.step picks
    the first NON-finish call so the action executes; the affirmative finish must
    come on its OWN later turn (where the finish gates run). Both orderings
    ([finish, action] and [action, finish]) prefer the action."""
    from disco.core.llm import ProposedToolCall

    for tool_calls in (
        [
            ProposedToolCall(tool_name="finish", arguments={}),
            ProposedToolCall(tool_name="shell", arguments={"command": "echo build"}),
        ],
        [
            ProposedToolCall(tool_name="shell", arguments={"command": "echo build"}),
            ProposedToolCall(tool_name="finish", arguments={}),
        ],
    ):
        agent = BuildAgent(_batched_router(tool_calls))
        step = await _step(agent, OperatingMode.LONG_HORIZON)
        assert step.tool_call is not None
        assert step.tool_call.tool_name == "shell"  # the real action, NOT finish
        assert step.finished is False


async def test_lone_finish_call_is_preserved():
    """Guard: a finish on its own (no batched action) is still taken — the
    drop-batched-finish rule only fires when a real action is present."""
    from disco.core.llm import ProposedToolCall

    agent = BuildAgent(_batched_router([ProposedToolCall(tool_name="finish", arguments={})]))
    step = await _step(agent, OperatingMode.LONG_HORIZON)
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "finish"
