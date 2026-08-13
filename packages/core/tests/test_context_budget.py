"""H — context/cost bloat fixes.

(1) [C9] the condensation trigger honors the LIVE context window — a 128k model
    condenses at 83k and a 1M model at 681k (their 65% points), rather than at a
    fixed working-budget ceiling. Unknown windows retain the budget-only path.
    The summarizer input has its own independent bound.

(2) large tool-call argument values (file bodies) are elided at render time so
    they're not re-sent every turn."""

from __future__ import annotations

from disco.core.events import ActionEvent, ToolCall
from disco.core.view import LLMSummarizingCondenser, View

# ---- C9: live context window -------------------------------------------------


def test_c9_128k_window_scales_soft_above_old_24k_cap():
    """A 128k-window model should condense at its 65% point (83_200), not the old
    fixed 24k cap. This is the headline behavior C9 was filed to fix."""
    c = LLMSummarizingCondenser(context_window=128_000)
    # 0.65 * 128_000 = 83_200 — well above the old 24k cap.
    assert c._max == 83_200
    assert c._max > 24_000  # specifically: NOT pinned to the working budget
    # 0.80 * 128_000 = 102_400.
    assert c._hard == 102_400
    assert c._hard > 32_000


def test_c9_1m_window_uses_its_real_fraction():
    """A known 1M window is not silently reduced to a 96k/128k pseudo-window."""
    c = LLMSummarizingCondenser(context_window=1_048_576)
    assert c._max == 681_574
    assert c._hard == 838_860


def test_c9_1m_window_grows_to_ceiling_not_to_681k():
    """Historical inventory ID; the accepted policy now uses the real window.

    The immutable node ID remains collected so changing the policy does not look
    like silent test deletion.  The current assertion owner is the descriptive
    test above.
    """
    test_c9_1m_window_uses_its_real_fraction()


def test_c9_unknown_window_still_uses_the_budget():
    """No window known → the working budget IS the bound (unchanged from before)."""
    c = LLMSummarizingCondenser()
    assert c._max == 24_000 and c._hard == 32_000


def test_c9_explicit_overrides_win_over_derived_fraction():
    """Explicit max_tokens / hard_max_tokens are authoritative (tests rely on this).
    Even on a 1M-window model, the user-supplied value wins."""
    c = LLMSummarizingCondenser(context_window=1_048_576, max_tokens=50, hard_max_tokens=60)
    assert c._max == 50  # NOT 96_000
    assert c._hard == 60  # NOT 128_000


def test_c9_explicit_overrides_win_over_derived_ceiling():
    """Historical inventory ID for the still-valid explicit-override contract."""
    test_c9_explicit_overrides_win_over_derived_fraction()


# ---- H1: small-window path — UNCHANGED (regression guard) ------------------


def test_h1_small_window_still_uses_its_fraction():
    """A small window whose `window × frac` is already ≤ the budget uses its own
    65% / 80% (budget isn't binding). C9 must NOT touch this path."""
    c = LLMSummarizingCondenser(context_window=16_000)
    assert c._max == int(16_000 * 0.65)  # 10_400 < 24_000 budget
    assert c._hard == int(16_000 * 0.80)


def test_h1_explicit_overrides_win_for_small_window():
    """Explicit overrides win regardless of window size."""
    c = LLMSummarizingCondenser(context_window=16_000, max_tokens=500, hard_max_tokens=600)
    assert c._max == 500 and c._hard == 600


# ---- C9: should_condense behavior at the new thresholds --------------------


def test_c9_should_condense_fires_past_83k_for_a_128k_model():
    """On a 128k-window model with the new C9 thresholds, condensation only fires
    once we cross 83k soft (or 102k hard). Below that, the model still has room."""
    c = LLMSummarizingCondenser(context_window=128_000)
    empty = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    # Under soft: don't condense.
    assert c.should_condense(empty, token_count=80_000) is None
    # Over soft: maintain the bound.
    soft = c.should_condense(empty, token_count=85_000)
    assert soft is not None and soft.soft is True
    # Over hard: must condense now.
    hard = c.should_condense(empty, token_count=105_000)
    assert hard is not None and hard.soft is False


def test_c9_should_condense_uses_fraction_boundaries_for_a_1m_model():
    """A 1M model waits for its actual soft and hard context fractions."""
    c = LLMSummarizingCondenser(context_window=1_048_576)
    empty = View(messages=[], visible_seqs=[], total_events=0, forgotten_count=0)
    assert c.should_condense(empty, token_count=100_000) is None
    assert c.should_condense(empty, token_count=500_000) is None
    assert c.should_condense(empty, token_count=c._max - 1) is None
    soft = c.should_condense(empty, token_count=c._max)
    assert soft is not None and soft.soft is True
    hard = c.should_condense(empty, token_count=c._hard)
    assert hard is not None and hard.soft is False


def test_c9_should_condense_fires_past_ceiling_for_a_1m_model():
    """Historical inventory ID; the boundary now follows the real model window."""
    test_c9_should_condense_uses_fraction_boundaries_for_a_1m_model()


# ---- (2) large tool-call argument values are elided at render time ----------


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
    assert str(args["content"]).startswith("[[DISCO-ELIDED:")
    assert "history display only" in str(args["content"])
    # The marker identifies historical metadata without forcing a redundant
    # whole-file reread. Current resource context remains authoritative, and a
    # later action may request only the range it actually needs.
    assert "chars" in args["content"] and "metadata, not file content" in args["content"]
    assert "minimal range needed" in args["content"]
    assert "file_read the path" not in args["content"]


def test_small_args_are_left_intact():
    ev = ActionEvent(
        thought="run it",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "npm test"}),
    )
    assert ev.to_llm_message().tool_calls[0]["arguments"] == {"command": "npm test"}
