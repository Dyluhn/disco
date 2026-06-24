"""`agent._clean_thought` — strip leaked, stacked surface-form prefixes off a
model thought before it is stored as the ActionEvent thought.

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
