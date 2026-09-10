"""`agent._clean_thought` — strip leaked, stacked surface-form prefixes and
inline think spans off a model thought before it is stored or displayed.

Live defect (build mode, MiniMax — conv_4a1e9405dba54aadb90e03589870630e): the
View feeds prior thoughts back with a rotating "Reasoning:" / "Thought:" surface
form (anti-overfit decoration). MiniMax echoed that decoration into its next
thought, and because each turn re-decorates, the prefix STACKED — stored thoughts
read "Reasoning: Reasoning: Reasoning: …". Stripping the leaked leading decorators
at storage time cleans the event AND breaks the feedback loop.
"""

from __future__ import annotations

from disco.core.loop.agent import _clean_thought


def test_stacked_reasoning_prefixes_collapse() -> None:
    assert (
        _clean_thought("Reasoning: Reasoning: Reasoning: The planner returned an empty plan.")
        == "The planner returned an empty plan."
    )


def test_mixed_thought_and_reasoning_prefixes_stripped() -> None:
    """The exact live shape: a 'Thought:' followed by stacked 'Reasoning:'."""
    assert (
        _clean_thought("Thought: Reasoning: Reasoning: I'll fill in concrete steps now.")
        == "I'll fill in concrete steps now."
    )


def test_single_prefix_stripped() -> None:
    assert _clean_thought("Reasoning: Looking at the layout.") == "Looking at the layout."


def test_clean_thought_is_unchanged() -> None:
    """A thought with no leaked decoration passes through untouched."""
    text = "Build verified — HTTP 200, no console errors."
    assert _clean_thought(text) == text


def test_midsentence_reasoning_is_preserved() -> None:
    """Only LEADING decorators are stripped — a legitimate mid-text 'Reasoning:'
    stays."""
    text = "The plan tracker is stale. Reasoning: it never got the delta."
    assert _clean_thought(text) == text


def test_idempotent() -> None:
    once = _clean_thought("Reasoning: Reasoning: done")
    assert _clean_thought(once) == once == "done"


def test_empty_string() -> None:
    assert _clean_thought("") == ""


def test_closed_think_span_stripped_from_visible_thought() -> None:
    assert _clean_thought("<think>reasoning here</think>Got it, building X.") == (
        "Got it, building X."
    )


def test_think_only_thought_collapses_to_empty() -> None:
    assert _clean_thought("<think>only reasoning</think>").strip() == ""


def test_decorator_strip_still_collapses_repeated_reasoning() -> None:
    assert _clean_thought("Reasoning: Reasoning: hello") == "hello"


def test_think_span_and_decorator_both_strip() -> None:
    assert _clean_thought("<think>x</think>Reasoning: done") == "done"


def test_plain_thought_without_tags_or_decorators_is_unchanged() -> None:
    text = "Got it, building X."
    assert _clean_thought(text) == text
