"""A repeated duplicate-`serve` refusal must say something new.

Counted-promotion failure 2026-07-27 (`p4_ff_static_continue` seed 600002,
ACTIONLESS_THRASH). The agent handed off, then called `serve` twice more and got
a BYTE-IDENTICAL reminder each time. `serve` is non-productive, so each attempt
burned an actionless turn and the third tripped the valve — on a run that went
on to FINISH successfully.

The first reminder was correct and actionable, so this is not a broken contract;
it is an information defect. Repeating a sentence a model has already mis-read
gives it nothing to act on differently, and never mentions that the next repeat
ends the run.

The actionless cap itself is untouched. A genuinely stuck agent still lands in
the same valve at the same count.
"""

from __future__ import annotations

from disco.core.loop.turn_control import (
    _SERVE_DUPLICATE_GUIDANCE,
    _serve_duplicate_guidance,
)


def test_the_first_refusal_is_unchanged():
    # No regression for the common case: one stray duplicate reads as before.
    assert _serve_duplicate_guidance(1) == _SERVE_DUPLICATE_GUIDANCE


def test_a_repeat_stops_repeating_itself():
    second = _serve_duplicate_guidance(2)
    assert second != _SERVE_DUPLICATE_GUIDANCE


def test_a_repeat_names_the_cost_and_the_remaining_moves():
    second = _serve_duplicate_guidance(2)
    # It must say the attempts are spending the run down …
    assert "ENDS this run" in second
    assert "not counted as work" in second
    # … and name the only two things left to do.
    assert "`finish`" in second
    assert "file_write" in second
    assert "Do not call `serve` again" in second


def test_the_count_is_stated_so_each_repeat_carries_new_information():
    assert "2 times" in _serve_duplicate_guidance(2)
    assert "3 times" in _serve_duplicate_guidance(3)
    assert _serve_duplicate_guidance(2) != _serve_duplicate_guidance(3)


def test_every_variant_stays_an_ambient_system_reminder():
    # Never a user-tone scolding — same framing rule as the other loop nudges.
    for repeats in (1, 2, 3, 9):
        text = _serve_duplicate_guidance(repeats)
        assert text.startswith("<system-reminder>")
        assert text.rstrip().endswith("</system-reminder>")


def test_it_never_tells_the_agent_to_finish_unconditionally():
    # The escalation must not become a forced finish: finishing stays gated on
    # the plan and its verification actually being complete.
    second = _serve_duplicate_guidance(2)
    assert "if the plan and its verification are complete" in second
