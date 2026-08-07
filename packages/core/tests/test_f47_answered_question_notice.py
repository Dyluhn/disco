"""F47 — answered-question repetition is culpable only after notice.

Regression evidence for the 2026-08-06x repair of the mechanism that failed the
Stage-5 ten-cell pilot at 2026-08-06v. Every case below is built from a real
06v cell's own recorded shape, named in the test.

# The invariant this file enforces

    Answered-question repetition is culpable only after notice; broken-tool
    repetition is culpable immediately.

    A prior occurrence that FAILED *is* its own notice — the agent holds the
    failure as its own observation. A prior occurrence that SUCCEEDED is not a
    notice — the agent holds an answer. So for every equivalence class on which
    the harness counts repetitions of SUCCESSFUL calls, the product must compute
    the same class and hand the answer back before the repeat is held against
    the run.

`ThrashOracle` counts answered-question repetitions on two classes. Before this
repair the loop's W-39 memo computed one of them, so two of the three 06v thrash
reds were faulted for ignoring advice that was never given:

  * `p4_ff_static_continue` @97601 — memo fired inside the group (seq 164,
    before the fatal repeat at 170). The cap worked. **Still red after this
    repair** — see the negative control at the bottom; that is what proves this
    is a repair and not an exemption.
  * `p4_ff_node_restart`  @97652 — memos at 215 and 261, both BEFORE the counted
    group at 275/284/287. Window mismatch: the memo's anti-spam was scoped to
    the freshness streak, the oracle counts consecutive identical actions.
  * `diag_script_run`     @97903 — ZERO memos. One script under three spellings;
    the oracle grouped all three, `tool_call_fingerprint` grouped none. Class
    blindness — the drift `tool_fingerprint.py` says must never exist.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ToolCall,
    ToolResult,
)
from disco.core.loop.dedup import (
    _W39_REMINDER_SENTINEL,
    _W39_SCRIPT_REMINDER_SENTINEL,
    _W39_SHELL_TOOLS,
    _w39_shell_verify_reminder,
)

# NOTE: the 2026-08-07f plan-tracking symbols (`_W39_PLAN_TOOLS`,
# `_W39_NOTICE_TOOLS`, `_W39_PLAN_REMINDER_SENTINEL`,
# `_w39_plan_progress_reminder`) are imported INSIDE the test bodies below, not
# here. They do not exist at `4ce28e50`, and a module-level import of them turns
# the entire file — including the five pre-existing 06x regression tests — into
# one collection error when the suite is run against the old bytes. That is a
# much weaker red than each new body failing on the property it actually asserts,
# and it would have hidden whether the OLD tests still pass on the OLD tree.
from disco.core.script_identity import (
    describe_script_fingerprint,
    foreground_script_fingerprints,
)
from event_fakes import user_msg, with_seqs

# The 06v cells' own commands, verbatim from their `events.jsonl`.
C97903_FIRST = "cd /workspace && python3 primes.py"
C97903_SECOND = "cd /workspace && python3 primes.py | wc -l && python3 primes.py | tail -1"
C97903_THIRD = "python3 primes.py | tee /tmp/primes.out | wc -l && tail -n 1 /tmp/primes.out"
C97903_RESULT = "2\n3\n5\n7\n11\n13\n17\n19\n23\n29\n31\n37\n41\n43\n47\n53\n59\n61\n67\n71"


def _shell(call_id: str, command: str) -> ActionEvent:
    return ActionEvent(
        thought="verifying",
        tool_call=ToolCall(tool_name="shell", call_id=call_id, arguments={"command": command}),
    )


def _obs(action: ActionEvent, content: str = "ok", *, success: bool = True) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="shell",
            success=success,
            content=content if success else "",
            error=None if success else "command failed",
        ),
        action_id=action.id,
    )


def _reminder_msg(text: str) -> MessageEvent:
    return MessageEvent(
        source=EventSource.ENVIRONMENT, message=LLMMessage(role="user", content=text)
    )


def _remind(events: list[Event]) -> tuple[bool, int, str]:
    """Ask the memo about the LAST event, which must be the current action."""
    current = events[-1]
    assert isinstance(current, ActionEvent) and current.tool_call is not None
    return _w39_shell_verify_reminder(
        current.tool_call.tool_name, current.tool_call.arguments, events
    )


# ---------------------------------------------------------------------------
# The shared equivalence class — the A6.3 §3.1 single-owner rule, extended
# ---------------------------------------------------------------------------


def test_script_identity_groups_the_97903_spellings_as_one_question():
    """The three commands 97903 actually issued are ONE script identity.

    This is the class the oracle grouped and faulted on. If this assertion ever
    fails, the memo and `ThrashOracle` have drifted apart again.
    """
    first = foreground_script_fingerprints(C97903_FIRST)
    second = foreground_script_fingerprints(C97903_SECOND)
    third = foreground_script_fingerprints(C97903_THIRD)
    assert first and second and third
    assert first == second == third
    # The oracle's own fingerprint for that cell, from its classification.json.
    assert sorted(first) == ['["python","/workspace/primes.py",[]]']
    assert describe_script_fingerprint(sorted(first)[0]) == "python /workspace/primes.py"


def test_one_command_running_a_script_twice_is_one_identity_not_two():
    """`... | wc -l && python3 primes.py | tail -1` invokes the script twice.

    That is ONE question asked once, not two occurrences — the oracle dedupes
    with `sorted(set(direct_script_invocations(...)))` and the memo must agree,
    or a single command would consume two of the cap's three slots.
    """
    assert len(foreground_script_fingerprints(C97903_SECOND)) == 1


def test_background_starts_are_not_the_answered_question_class():
    """A background start is a different shape with its own cleanup credit."""
    assert foreground_script_fingerprints("python3 server.py &") == frozenset()


# ---------------------------------------------------------------------------
# @97903 — class blindness. RED on the old bytes: no memo existed for this class.
# ---------------------------------------------------------------------------


def test_97903_shape_notice_precedes_the_cap_exceeding_repeat():
    first = _shell("c1", C97903_FIRST)
    second = _shell("c2", C97903_SECOND)
    events = with_seqs([user_msg("build it"), first, _obs(first, C97903_RESULT), second])

    should, prior_seq, text = _remind(events)

    assert should is True, "the second spelling re-asks an answered question"
    assert _W39_SCRIPT_REMINDER_SENTINEL in text
    assert "python /workspace/primes.py" in text
    assert prior_seq == events[1].seq
    # A6.3 §2 — the memo hands back the ANSWER, not merely the fact of a prior run.
    assert "ending in 71" in text or "71" in text
    # It must not pretend the two commands were the same string.
    assert _W39_REMINDER_SENTINEL not in text


def test_97903_third_spelling_still_covered_by_the_notice_already_given():
    """Occurrence 3 is the fatal one (cap 2). It needs a notice to PRECEDE it,
    not a second copy of the same advice."""
    first = _shell("c1", C97903_FIRST)
    second = _shell("c2", C97903_SECOND)
    third = _shell("c3", C97903_THIRD)
    notice = _reminder_msg(
        f"<system-reminder>\n{_W39_SCRIPT_REMINDER_SENTINEL} You already ran "
        f"`python /workspace/primes.py` at step 2 ...\n</system-reminder>"
    )
    events = with_seqs(
        [
            user_msg("build it"),
            first,
            _obs(first, C97903_RESULT),
            second,
            _obs(second, C97903_RESULT),
            notice,
            third,
        ]
    )
    should, _prior_seq, _text = _remind(events)
    assert should is False, "already told; a second identical advisory is spam"
    # ...but the notice IS in the record before the fatal occurrence, which is
    # what the invariant requires.
    assert any(
        isinstance(e, MessageEvent)
        and e.message is not None
        and isinstance(e.message.content, str)
        and _W39_SCRIPT_REMINDER_SENTINEL in e.message.content
        and (e.seq or 0) < (events[-1].seq or 0)
        for e in events
    )


def test_script_class_respects_the_freshness_boundary():
    """A workspace mutation between the runs means re-verifying is legitimate.

    Fail-safe direction: the memo goes silent, it never asserts "nothing has
    changed" when something has.
    """
    first = _shell("c1", C97903_FIRST)
    write = ActionEvent(
        thought="edit",
        tool_call=ToolCall(
            tool_name="file_write",
            call_id="w1",
            arguments={"path": "primes.py", "content": "x"},
        ),
    )
    second = _shell("c2", C97903_SECOND)
    events = with_seqs(
        [user_msg("build it"), first, _obs(first, C97903_RESULT), write, second]
    )
    should, _prior_seq, _text = _remind(events)
    assert should is False


def test_script_class_requires_the_prior_run_to_have_SUCCEEDED():
    """Broken-tool repetition needs no notice — the failure is its own notice.

    This is the other half of the invariant, and it is why the repair does not
    collapse the two classes into one.
    """
    first = _shell("c1", C97903_FIRST)
    second = _shell("c2", C97903_SECOND)
    events = with_seqs(
        [user_msg("build it"), first, _obs(first, "", success=False), second]
    )
    should, _prior_seq, _text = _remind(events)
    assert should is False


def test_verify_probe_priors_are_excluded_exactly_as_the_oracle_excludes_them():
    """`_script_command` drops `verify_probe` actions from the semantic group.

    The memo must group the same population, or the coverage duty is not met in
    the direction that matters: pointing at a probe the oracle never counted.
    """
    probe = _shell("c1", C97903_FIRST)
    probe.meta["verify_probe"] = True
    second = _shell("c2", C97903_SECOND)
    events = with_seqs([user_msg("build it"), probe, _obs(probe, C97903_RESULT), second])
    should, _prior_seq, _text = _remind(events)
    assert should is False


# ---------------------------------------------------------------------------
# @97652 — window mismatch. RED on the old bytes: the memo's one shot was spent
# upstream of the group the oracle counted, so the group ran silent.
# ---------------------------------------------------------------------------


def test_97652_shape_notice_rearms_inside_the_counted_group():
    """A memo spent EARLIER in the same freshness streak must not silence the
    group the oracle actually counts."""
    command = "python3 -c \"import urllib.request as U;U.urlopen('http://127.0.0.1:8080/')\""
    early = _shell("e1", command)
    spent_notice = _reminder_msg(
        f"<system-reminder>\n{_W39_REMINDER_SENTINEL} You already ran "
        f"`{command}` earlier (step 2) and it passed...\n</system-reminder>"
    )
    group_first = _shell("g1", command)
    group_second = _shell("g2", command)
    events = with_seqs(
        [
            user_msg("build it"),
            early,
            _obs(early, "OK 200"),
            spent_notice,
            group_first,
            _obs(group_first, "OK 200"),
            group_second,
        ]
    )

    should, prior_seq, text = _remind(events)

    assert should is True, "the counted group must not run silent"
    assert _W39_REMINDER_SENTINEL in text
    # It points at the occurrence INSIDE the group, not the spent one upstream.
    assert prior_seq == events[4].seq


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL — @97601. Notice given inside the group; the agent repeated
# anyway. The memo must stay silent, and the run must still be red.
# ---------------------------------------------------------------------------


def test_97601_negative_control_notice_given_then_repeated_is_not_excused():
    """The pre-repair shape that was adjudicated CORRECTLY must be unchanged.

    97601 received its memo at seq 164, before the fatal repeat at 170, and the
    cap fired. Nothing in this repair excuses that cell: the memo does not fire
    again, the command is not suppressed, and the oracle's cap is untouched. A
    repair that turned this green would be an exemption wearing a repair's
    clothes.
    """
    command = "python3 /tmp/verify.py"
    result = "OK http://localhost:5000/ 200 bytes=11938 h1+continue present"
    first = _shell("v1", command)
    second = _shell("v2", command)
    notice = _reminder_msg(
        f"<system-reminder>\n{_W39_REMINDER_SENTINEL} You already ran `{command}` "
        f"earlier (step 2) and it passed...\n</system-reminder>"
    )
    third = _shell("v3", command)
    events = with_seqs(
        [
            user_msg("continue the page"),
            first,
            _obs(first, result),
            second,
            _obs(second, result),
            notice,
            third,
        ]
    )

    should, _prior_seq, _text = _remind(events)

    assert should is False, "already told inside the group — the repeat is the agent's"


# ---------------------------------------------------------------------------
# F47, 2026-08-07f — the PLAN-TRACKING class. The notice half's REACH, not its
# identity owner.
#
# The 2026-08-06x repair proved the two sides SHARE an identity owner, and that
# was true and is still true. What it did not prove is that a notice can actually
# be REACHED for every class the oracle counts. `ThrashOracle` counts
# SHAPE_IDENTICAL_STREAK over every tool; the notice was emitted only for
# `_W39_SHELL_TOOLS`. So the coverage test passed while the guarantee was false
# for every non-shell tool — the same Enumerated-Class Invariant shape as F51.
#
# Measured live at 2026-08-07d: `p4_ff_node_restart`@98651 issued an IDENTICAL
# `update_plan_progress` at seqs 229/232/235 against a cap of 2, each returning
# `success=True` / `plan progress: 2/2 done`, and the FIRST feedback body arrived
# at seq 237 — after the cap was already exceeded.
# ---------------------------------------------------------------------------

PLAN_ARGS = {"steps": [{"index": 1, "state": "done"}, {"index": 2, "state": "done"}]}
PLAN_RESULT = "plan progress: 2/2 done"


def _plan_call(call_id: str, tool: str = "update_plan_progress", args: dict | None = None):
    return ActionEvent(
        thought="marking progress",
        tool_call=ToolCall(
            tool_name=tool, call_id=call_id, arguments=dict(PLAN_ARGS if args is None else args)
        ),
    )


def _plan_obs(action: ActionEvent, *, success: bool = True) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=success,
            content=PLAN_RESULT if success else "",
            error=None if success else "plan update failed",
        ),
        action_id=action.id,
    )


def _plan_remind(events: list[Event]) -> tuple[bool, int, str]:
    from disco.core.loop.dedup_plan_notice import _w39_plan_progress_reminder

    current = events[-1]
    assert isinstance(current, ActionEvent) and current.tool_call is not None
    return _w39_plan_progress_reminder(
        current.tool_call.tool_name, current.tool_call.arguments, events
    )


def test_98651_shape_notice_precedes_the_cap_exceeding_plan_repeat():
    """The live defect, as its own regression: notice BEFORE culpability.

    Three identical `update_plan_progress` calls against a cap of 2. The second
    call is the one the notice must precede — before this repair the memo could
    not fire for a non-shell tool at all, so the agent reached the third call
    having been told nothing.
    """
    from disco.core.loop.dedup_plan_notice import _W39_PLAN_REMINDER_SENTINEL

    first = _plan_call("p1")
    second = _plan_call("p2")
    events = with_seqs([user_msg("finish the plan"), first, _plan_obs(first), second])

    should, prior_seq, text = _plan_remind(events)

    assert should is True, "answered-question repetition must be given notice BEFORE the cap"
    assert prior_seq == 2, "the notice names the actual prior step"
    assert _W39_PLAN_REMINDER_SENTINEL in text
    assert "update_plan_progress" in text
    assert PLAN_RESULT in text, "hand back the answer the agent already has, not just its existence"


def test_the_plan_notice_is_repetition_aware_and_escalates(monkeypatch):
    """GROUNDED FEEDBACK constraint 4 — this surface can fire more than once per
    run, so its body must carry the count and the two fires must differ."""
    first = _plan_call("p1")
    second = _plan_call("p2")
    events = with_seqs([user_msg("go"), first, _plan_obs(first), second])
    _should, _seq, second_text = _plan_remind(events)

    third = _plan_call("p3")
    events2 = with_seqs(
        [
            user_msg("go"),
            first,
            _plan_obs(first),
            second,
            _plan_obs(second),
            _reminder_msg(second_text),
            third,
        ]
    )
    should3, _seq3, third_text = _plan_remind(events2)

    assert should3 is True, "the occurrence pivot must re-arm for the NEXT repeat"
    assert second_text != third_text, (
        "constraint 4: a surface that can fire twice per run must not emit the same "
        "bytes twice — this is exactly the F51 defect one level down"
    )
    assert "2 times" in second_text and "3 times" in third_text


def test_the_plan_notice_requires_the_prior_call_to_have_SUCCEEDED():
    """A FAILED prior call is broken-tool repetition, which F47 says is culpable
    immediately. Dressing it as "you already have this answer" would be a false
    assurance — the agent holds a failure, not an answer."""
    first = _plan_call("p1")
    second = _plan_call("p2")
    events = with_seqs(
        [user_msg("go"), first, _plan_obs(first, success=False), second]
    )

    should, _seq, _text = _plan_remind(events)

    assert should is False


def test_the_plan_notice_respects_the_shared_currency_predicate():
    """Constraint 2 — one currency predicate, two consumers. A real workspace
    mutation after the prior call ends its currency, so the notice must go
    silent rather than assert "no change since then" across a boundary the
    grader has already closed."""
    first = _plan_call("p1")
    write = ActionEvent(
        thought="edit",
        tool_call=ToolCall(
            tool_name="file_write", call_id="w1", arguments={"path": "a.py", "content": "x"}
        ),
    )
    second = _plan_call("p2")
    events = with_seqs(
        [
            user_msg("go"),
            first,
            _plan_obs(first),
            write,
            ObservationEvent(
                tool_result=ToolResult(
                    call_id="w1", tool_name="file_write", success=True, content="written"
                ),
                action_id=write.id,
            ),
            second,
        ]
    )

    should, _seq, _text = _plan_remind(events)

    assert should is False, "a closed currency boundary must silence the echo, not widen it"


def test_a_DIFFERENT_plan_call_is_a_different_question():
    """Identity is `tool_call_fingerprint`, the owner the oracle grades with — so
    marking a different step is not a repeat and gets no notice."""
    first = _plan_call("p1")
    second = _plan_call(
        "p2", args={"steps": [{"index": 1, "state": "done"}, {"index": 2, "state": "pending"}]}
    )
    events = with_seqs([user_msg("go"), first, _plan_obs(first), second])

    should, _seq, _text = _plan_remind(events)

    assert should is False


def test_the_notice_gate_admits_every_class_the_oracle_counts_repeats_on():
    """The REACH duty, which is what actually failed at 98651.

    `_prepare_observation` gates on `_W39_NOTICE_TOOLS`. A tool class that the
    oracle counts identical repeats on but that is absent from this set is a
    class where culpability is charged with no notice reachable — and nothing
    else in the suite would go red. Asserted against the set the loop ACTUALLY
    gates on, never a copy of it.
    """
    from disco.core.loop.dedup import _W39_NOTICE_TOOLS, _W39_PLAN_TOOLS

    assert _W39_SHELL_TOOLS <= _W39_NOTICE_TOOLS
    assert _W39_PLAN_TOOLS <= _W39_NOTICE_TOOLS
    assert "update_plan_progress" in _W39_NOTICE_TOOLS, "the 98651 class"
    # The two proposal tools are deliberately OUT: `_auto_approve_revision`
    # already emits `_IDENTICAL_PLAN_NUDGE_TEXT` for a byte-identical revision and
    # a second notice would double-fire on one event. Pinned so the exclusion
    # stays a stated decision rather than drifting into an accident.
    assert "propose_plan_update" not in _W39_NOTICE_TOOLS
    assert "submit_plan" not in _W39_NOTICE_TOOLS
