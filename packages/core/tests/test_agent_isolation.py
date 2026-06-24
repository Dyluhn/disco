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


def _truncated_router(text="…Let me take a real"):
    # A prose turn the provider cut off mid-sentence (finish_reason=="length").
    provider = SequenceProvider([{"text": text, "finish_reason": "length"}])
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


# ---- W-31: truncated prose (finish_reason=="length") is NOT a complete turn --


async def test_truncated_prose_step_is_flagged_and_not_finished():
    """W-31 — a tool-less prose turn the provider cut off mid-sentence
    (finish_reason=="length") must be FLAGGED truncated and must NOT be treated
    as finished, even for a prose-finishing agent (Research). Surfacing a cut-off
    fragment as a complete answer is the bug."""
    for agent in (
        ResearchAgent(_truncated_router()),  # prose_finishes=True
        BuildAgent(_truncated_router()),     # prose_finishes=False
    ):
        step = await _step(agent, OperatingMode.LONG_HORIZON)
        assert step.truncated is True
        assert step.tool_call is None
        assert step.finished is False  # cut-off fragment is never a completed turn
        assert "Let me take a real" in step.thought  # partial text preserved


async def test_normal_finish_reason_stop_is_not_truncated():
    """W-31 guard: a normal prose turn (finish_reason=="stop") is UNAFFECTED —
    not flagged truncated, and Research still finishes / Build still doesn't."""
    research = await _step(ResearchAgent(_router("the answer is 42")), OperatingMode.LONG_HORIZON)
    assert research.truncated is False
    assert research.finished is True

    build = await _step(BuildAgent(_router("working on it…")), OperatingMode.LONG_HORIZON)
    assert build.truncated is False
    assert build.finished is False


# ---- fix-slides-wedge: unclosed <think> is structural truncation ------------


def _unclosed_think_router(text="<think>\nLet me design the deck. First I will"):
    # A reasoning model (e.g. MiniMax) that inlines its chain-of-thought as
    # literal `<think>` tags in `content` and exhausts the output cap mid-thought
    # — WITHOUT reporting finish_reason=="length" (default "stop" here). This is
    # the exact agent-slides-build wedge: the turn never closed its `<think>`, so
    # it never reached a tool call, yet the plain length check treated it as a
    # clean, completed no-op and the loop silently re-stepped a 2-minute dump.
    provider = SequenceProvider([{"text": text, "finish_reason": "stop"}])
    return DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})


async def test_unclosed_think_prose_is_flagged_truncated_despite_stop():
    """fix-slides-wedge — a tool-less turn whose visible content opens `<think>`
    but never closes it was cut off mid-reasoning. It must be FLAGGED truncated
    (so the loop fires its "you were cut off — take the action now" steer instead
    of mis-reading it as a finished no-op) EVEN WHEN finish_reason=="stop"."""
    for agent in (
        ResearchAgent(_unclosed_think_router()),  # prose_finishes=True
        BuildAgent(_unclosed_think_router()),     # prose_finishes=False
    ):
        step = await _step(agent, OperatingMode.LONG_HORIZON)
        assert step.truncated is True
        assert step.tool_call is None
        assert step.finished is False  # an unclosed think is never a completed turn


async def test_closed_think_prose_is_not_truncated():
    """Guard against false positives: a fully-closed `<think>…</think>` followed
    by prose (finish_reason=="stop") is a COMPLETE turn — not flagged truncated.
    Research still finishes; Build still doesn't."""
    closed = "<think>\nweighing options\n</think>\nHere is the plan."
    provider = SequenceProvider([{"text": closed, "finish_reason": "stop"}])
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    provider2 = SequenceProvider([{"text": closed, "finish_reason": "stop"}])
    router2 = DefaultLLMRouter(simple_config(), {"ollama": provider2, "openrouter": provider2})

    research = await _step(ResearchAgent(router), OperatingMode.LONG_HORIZON)
    assert research.truncated is False
    assert research.finished is True

    build = await _step(BuildAgent(router2), OperatingMode.LONG_HORIZON)
    assert build.truncated is False
    assert build.finished is False


async def test_lone_finish_call_is_preserved():
    """Guard: a finish on its own (no batched action) is still taken — the
    drop-batched-finish rule only fires when a real action is present."""
    from disco.core.llm import ProposedToolCall

    agent = BuildAgent(_batched_router([ProposedToolCall(tool_name="finish", arguments={})]))
    step = await _step(agent, OperatingMode.LONG_HORIZON)
    assert step.tool_call is not None
    assert step.tool_call.tool_name == "finish"
