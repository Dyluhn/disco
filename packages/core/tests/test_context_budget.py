"""H — context/cost bloat fixes. (1) the condensation trigger is CAPPED at a fixed
working budget regardless of the model's (possibly huge) window, so a 1M-window
model still condenses at ~24k instead of ~681k; (2) large tool-call argument values
(file bodies) are elided at render time so they're not re-sent every turn."""

from __future__ import annotations

from disco.core.events import ActionEvent, ToolCall
from disco.core.view import LLMSummarizingCondenser


def test_huge_window_is_capped_at_the_working_budget():
    # DeepSeek-class 1M window: 0.65× = 681k — but the budget caps it at 24k/32k.
    c = LLMSummarizingCondenser(context_window=1_048_576)
    assert c._max == 24_000  # NOT 681_574
    assert c._hard == 32_000  # NOT 838_860


def test_small_window_still_uses_its_fraction():
    # A small window below the budget uses its own 65%/80% (budget isn't binding).
    c = LLMSummarizingCondenser(context_window=16_000)
    assert c._max == int(16_000 * 0.65)  # 10_400 < 24_000 budget
    assert c._hard == int(16_000 * 0.80)


def test_no_window_falls_back_to_the_budget():
    c = LLMSummarizingCondenser()
    assert c._max == 24_000 and c._hard == 32_000


def test_should_condense_fires_at_the_budget_for_a_giant_window():
    c = LLMSummarizingCondenser(context_window=1_048_576)
    # A 60k context on a 1M model now triggers (it didn't before — 60k << 681k).
    from disco.core.view import View

    empty = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    assert c.should_condense(empty, token_count=60_000) is not None
    assert c.should_condense(empty, token_count=20_000) is None  # under budget → fine


def test_large_file_body_is_elided_in_the_action_message():
    big = "x" * 50_000  # a 50 KB file body
    ev = ActionEvent(
        thought="writing the app",
        tool_call=ToolCall(tool_name="file_write", arguments={"path": "app.js", "content": big}),
    )
    msg = ev.to_llm_message()
    args = msg.tool_calls[0]["arguments"]
    assert args["path"] == "app.js"  # small arg untouched
    assert big not in str(args["content"])  # the 50k body is gone from the prompt
    assert "elided" in args["content"] and "file_read" in args["content"]  # recoverable marker


def test_small_args_are_left_intact():
    ev = ActionEvent(
        thought="run it",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "npm test"}),
    )
    assert ev.to_llm_message().tool_calls[0]["arguments"] == {"command": "npm test"}
