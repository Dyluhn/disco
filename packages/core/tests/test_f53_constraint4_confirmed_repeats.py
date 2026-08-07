"""The four CONFIRMED byte-identical repeats of the 2026-08-07h live corpus, plus
F53's cross-seam leak (GROUNDED FEEDBACK constraint 4).

# Why these are different from F51's

F51's five surfaces were repaired against a corpus that PROVED them multi-fire,
and the residue behind them was accepted as a *hypothetical* upper bound: 68
enumerated rendering units that COULD fire twice, none observed doing it. The
2026-08-07h corpus ended that. Over 27 live cells it caught four byte-identical
repeat groups actually delivered to agents on current bytes, and the 07i review
found a fifth fact the instrument structurally could not report:

* `emit_plan_verifier_failure_notice` — TWICE, in two different cells
  (`p4_ff_react_steer@99201` seqs 114/131, `p4_ff_python_cancel_recovery@99704`
  seqs 71/74).
* `_plan_nudge` — `p4_ff_node_restart@99603` seqs 11/86.
* `_blocked_prompt` — `p4_ff_node_restart@99603` seqs 238/289.
* F53 — that last body was AUTHORED by `governed_contract_refusal` (an F51
  surface, repaired) and merely RENDERED by `_blocked_prompt` (not repaired),
  so the repaired surface's text repeated byte-identically by riding an
  unrepaired seam.

These are confirmed defects with cells, seeds and sequence numbers, not
enumeration members.

# The property under test

Constraint 4 binds every surface that CAN FIRE MORE THAN ONCE PER RUN, and the
count that satisfies it must be **ledger-derived and must not reset**. Two of
these three surfaces already had repair machinery available and did not use it
correctly:

* `emit_plan_verifier_failure_notice` sits in a module that owns `_notice_repeat`
  and `_again`, already carried the `blocking` label they count on, and had three
  siblings calling them. It simply never called them.
* `_plan_nudge`'s RENDERER already took a `repeats`, and its caller already
  computed one — from a SEGMENT WINDOW that restarts at every re-entry into
  planning. A window that resets cannot satisfy a constraint scoped to the run,
  and the seqs 11/86 pair is what that looks like in production.

# Red-on-old-bytes

New symbols are imported INSIDE test bodies, deliberately, exactly as
`test_f51_constraint4_multifire.py` does: a module-level import of something that
does not exist at `2aa67140` would collapse the file into one collection error,
and "the file did not import" is much weaker evidence than "this body asserts a
property the old bytes do not have". Each test carries its own red.
"""

from __future__ import annotations

import hashlib

from disco.core import (
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.events import PlanStep, PlanVerifierFailure
from disco.core.loop.control import Disp
from event_fakes import user_msg, with_seqs

# ---------------------------------------------------------------------------
# Fakes — the minimum surface each seam actually touches
# ---------------------------------------------------------------------------


class _FakeLoop:
    """Captures emitted events instead of persisting them."""

    def __init__(self, events: list[Event] | None = None) -> None:
        self.emitted: list[Event] = []
        self._log: list[Event] = events or []
        self._plan_nudges = 0
        self._autonomous = False

    async def _emit(self, event: Event) -> Event:
        self.emitted.append(event)
        return event

    async def _events(self) -> list[Event]:
        return self._log

    def _workflow_router_phase_active(self) -> bool:
        return False

    async def _post_noop_valve(self) -> Disp:
        return Disp.CONTINUE

    def bodies(self) -> list[str]:
        return [
            e.message.content
            for e in self.emitted
            if isinstance(e, MessageEvent) and e.message is not None
        ]

    def env_bodies(self) -> list[str]:
        """Only the ENVIRONMENT half — the text the seam itself renders."""
        return [
            e.message.content
            for e in self.emitted
            if isinstance(e, MessageEvent)
            and e.message is not None
            and e.source is EventSource.ENVIRONMENT
        ]


class _FakeResult:
    def __init__(self, predicate: str, reason: str) -> None:
        self.predicate = predicate
        self.reason = reason

    def __repr__(self) -> str:
        return self.predicate


def _plan() -> PlanEvent:
    return PlanEvent(summary="ship it", steps=[PlanStep(title="do the thing")], revision=2)


def _failure(fp: str = "verifier_fp_1") -> PlanVerifierFailure:
    return PlanVerifierFailure(
        plan_revision=2,
        plan_event_id="evt_plan",
        predicate_fingerprints=["p1"],
        spec_fingerprint="spec1",
        failure_fingerprint=fp,
        failure_kinds=["predicate_failed"],
        attempt_for_approved_plan=1,
        approvals_with_same_predicates=1,
    )


def _prior_notice(blocking: str) -> MessageEvent:
    """A durable prior firing, recognised by the `blocking` label alone."""
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="<system-reminder>prior</system-reminder>"),
        meta={"blocking": blocking},
    )


# ---------------------------------------------------------------------------
# Surface 1 — `content_gate_parts/notices.emit_plan_verifier_failure_notice`
# p4_ff_react_steer@99201 seqs 114/131; p4_ff_python_cancel_recovery@99704 seqs 71/74.
# The ONLY surface confirmed multi-fire in TWO different cells.
# ---------------------------------------------------------------------------


async def test_plan_verifier_failure_notice_does_not_repeat_itself():
    """A verifier re-run against an UNCHANGED plan revision renders an unchanged
    body by construction: the revision, the failure fingerprint and the unmet
    block are all the same inputs. Only a count breaks the tie."""
    from disco.core.loop.finish.content_gate_parts.notices import (
        emit_plan_verifier_failure_notice,
    )

    results = [_FakeResult("exists('dist/app.js')", "file not found")]

    loop = _FakeLoop()
    await emit_plan_verifier_failure_notice(loop, _plan(), _failure(), results)
    first = loop.env_bodies()[0]

    # Second fire with the first firing's durable notice on the log.
    loop2 = _FakeLoop(with_seqs([user_msg("go"), _prior_notice("plan_verifier_failed")]))
    await emit_plan_verifier_failure_notice(loop2, _plan(), _failure(), results)
    second = loop2.env_bodies()[0]

    assert first != second, (
        "F51's siblings in this very module take a repeat count off the durable "
        "`blocking` label; this notice carried the label and never counted, and "
        "fired byte-identically in TWO cells of the 07h corpus"
    )
    assert "2" in second
    assert "did not clear it" in second, "count acknowledged AND escalating (constraint 4)"


async def test_plan_verifier_failure_notice_keeps_naming_what_it_derives():
    """Constraint 1 is not traded away for constraint 4: the revision, the failure
    fingerprint and the per-predicate reasons must survive the escalation."""
    from disco.core.loop.finish.content_gate_parts.notices import (
        emit_plan_verifier_failure_notice,
    )

    loop = _FakeLoop(with_seqs([_prior_notice("plan_verifier_failed")]))
    await emit_plan_verifier_failure_notice(
        loop,
        _plan(),
        _failure("verifier_fp_9"),
        [_FakeResult("exists('dist/app.js')", "file not found")],
    )
    body = loop.env_bodies()[0]

    assert "plan revision 2" in body
    assert "verifier_fp_9" in body
    assert "file not found" in body


async def test_plan_verifier_failure_count_is_keyed_on_its_own_blocking_label():
    """The count is per-CONDITION. Another fail-closed notice's blocking label must
    not inherit this one's escalation — the same rule F51 locked in per-fingerprint."""
    from disco.core.loop.finish.content_gate_parts.notices import (
        emit_plan_verifier_failure_notice,
    )

    loop = _FakeLoop(with_seqs([_prior_notice("dod_unmet"), _prior_notice("dod_unmet")]))
    await emit_plan_verifier_failure_notice(
        loop, _plan(), _failure(), [_FakeResult("p", "r")]
    )

    assert "did not clear it" not in loop.env_bodies()[0], (
        "a different blocking condition is a first fire, not a repeat"
    )


# ---------------------------------------------------------------------------
# Surface 2 — `planning_gates._nudge_planner` / `engine_contracts._plan_nudge`
# p4_ff_node_restart@99603 seqs 11/86.
#
# THE RESET IS THE DEFECT. The renderer took a count and the caller computed one;
# the caller computed it from a segment window that restarts at every re-entry
# into planning and hard-returns 0 when a plan lands. Two nudges either side of a
# re-entry therefore both rendered "nudge 1".
# ---------------------------------------------------------------------------


def _durable_nudge() -> MessageEvent:
    from disco.core.loop.engine_contracts import _PLAN_NUDGE_DIAGNOSTIC

    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="<system-reminder>nudge</system-reminder>"),
        meta={"diagnostic": _PLAN_NUDGE_DIAGNOSTIC},
    )


def _replanned_log() -> list[Event]:
    """One prior nudge, then the run cycles back through planning.

    This is the `p4_ff_node_restart@99603` shape: `plan_approved` and a fresh
    `planning` marker sit between seq 11 and seq 86, so the segment counter
    returns 0 at the second nudge and the body renders as a first firing.
    """
    return with_seqs(
        [
            user_msg("build it"),
            StatusEvent(status=ConversationStatus.RUNNING, detail="planning"),
            _durable_nudge(),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            StatusEvent(status=ConversationStatus.RUNNING, detail="planning"),
        ]
    )


async def test_plan_nudge_does_not_reset_when_the_run_re_enters_planning():
    """The confirmed defect. On old bytes both nudges render byte-identically
    because the segment window resets; the count must be whole-run."""
    from disco.core.loop.planning_gates import _nudge_planner

    first_loop = _FakeLoop(with_seqs([user_msg("build it")]))
    assert await _nudge_planner(first_loop) is Disp.CONTINUE
    first = first_loop.env_bodies()[0]

    second_loop = _FakeLoop(_replanned_log())
    assert await _nudge_planner(second_loop) is Disp.CONTINUE
    second = second_loop.env_bodies()[0]

    assert first != second, (
        "seqs 11/86 of p4_ff_node_restart@99603: a nudge count scoped to the "
        "planning SEGMENT resets on re-entry, so the second nudge rendered as a "
        "first firing. Constraint 4 is scoped to the RUN"
    )
    assert "planning nudge 2" in second
    assert "in this run" in second, "the wording must match the scope of the count"


async def test_plan_nudge_first_firing_is_byte_identical_to_the_legacy_constant():
    """Durable logs written before 2026-08-07b carry the first firing's exact text
    and the nudge counters recognise those events by content. The repair must not
    move that byte."""
    from disco.core.loop.engine_contracts import _PLAN_NUDGE
    from disco.core.loop.planning_gates import _nudge_planner

    loop = _FakeLoop(with_seqs([user_msg("build it")]))
    await _nudge_planner(loop)

    assert loop.env_bodies()[0] == _PLAN_NUDGE


async def test_plan_nudge_count_reads_the_durable_log_not_the_segment():
    """Three prior nudges scattered across three segments read as the fourth fire."""
    from disco.core.loop.planning_gates import _nudge_planner

    log = with_seqs(
        [
            user_msg("build it"),
            _durable_nudge(),
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
            _durable_nudge(),
            StatusEvent(status=ConversationStatus.RUNNING, detail="planning"),
            _durable_nudge(),
        ]
    )
    loop = _FakeLoop(log)
    await _nudge_planner(loop)

    assert "planning nudge 4" in loop.env_bodies()[0]


async def test_router_phase_nudge_still_keeps_its_own_separate_count():
    """The router nudge has never been counted by the planning-nudge counters and
    that must stay true — distinct labels, distinct escalations."""
    from disco.core.loop.planning_gates import _nudge_planner

    loop = _FakeLoop(with_seqs([user_msg("go"), _durable_nudge(), _durable_nudge()]))
    loop._workflow_router_phase_active = lambda: True  # type: ignore[method-assign]
    await _nudge_planner(loop)

    body = loop.env_bodies()[0]
    assert "WORKFLOW ROUTER" in body
    assert "router nudge" not in body, "two planning nudges must not escalate the router nudge"


# ---------------------------------------------------------------------------
# Surface 3 — `valve_landing._blocked_prompt`
# p4_ff_node_restart@99603 seqs 238/289.
# A pure function of (reason, guidance) with no access to the log at all.
# ---------------------------------------------------------------------------


def _landing():
    from disco.core.loop.valve_landing import ValveLandingMixin

    class _Landing(ValveLandingMixin):
        def __init__(self, loop: _FakeLoop) -> None:
            self._loop = loop  # type: ignore[assignment]

    return _Landing


def _prior_landing(reason: str, guidance: str = "") -> MessageEvent:
    """A durable prior landing prompt, as `land_blocked` writes it."""
    from disco.core.loop.turn_control_support import _BLOCKED_LANDING_META_KEY
    from disco.core.loop.valve_landing import _GUIDANCE_FP_META_KEY, _guidance_fingerprint

    meta: dict[str, object] = {
        _BLOCKED_LANDING_META_KEY: True,
        "blocked_reason": reason,
        "legacy_status": "STUCK",
    }
    fp = _guidance_fingerprint(guidance)
    if fp:
        meta[_GUIDANCE_FP_META_KEY] = fp
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="<system-reminder>prior</system-reminder>"),
        meta=meta,  # type: ignore[arg-type]
    )


async def test_blocked_landing_does_not_repeat_itself():
    from disco.core.loop.valve_landing import ValveLandingMixin  # noqa: F401

    reason = "host:verify_marker_abc"
    landing_cls = _landing()

    loop = _FakeLoop()
    await landing_cls(loop).land_blocked(reason=reason, guidance="fix the target")
    first = loop.env_bodies()[0]

    loop2 = _FakeLoop(with_seqs([user_msg("go"), _prior_landing(reason, "fix the target")]))
    await landing_cls(loop2).land_blocked(reason=reason, guidance="fix the target")
    second = loop2.env_bodies()[0]

    assert first != second, (
        "seqs 238/289 of p4_ff_node_restart@99603: `_blocked_prompt` was a pure "
        "function of (reason, guidance) and had no access to the run's history"
    )
    assert "did not clear it" in second


async def test_blocked_landing_first_firing_is_unchanged():
    """A first landing must render exactly what it rendered before the repair —
    the escalation is additive, never a rewrite of the base text."""
    landing_cls = _landing()
    loop = _FakeLoop()
    await landing_cls(loop).land_blocked(reason="the loop breaker fired", guidance="ctx")
    body = loop.env_bodies()[0]

    assert body.startswith("<system-reminder>\nYou are blocked because the loop breaker fired.\n")
    assert "Context: ctx" in body
    assert "did not clear it" not in body


async def test_blocked_landing_count_is_keyed_on_the_reason():
    """A different blocking condition is a different question and starts its own count."""
    landing_cls = _landing()
    loop = _FakeLoop(with_seqs([user_msg("go"), _prior_landing("something_else", "other")]))
    await landing_cls(loop).land_blocked(reason="host:verify_marker_abc", guidance="fix it")

    assert "did not clear it" not in loop.env_bodies()[0]


async def test_landing_counts_the_environment_half_only():
    """Each landing emits the prompt (ENVIRONMENT) and the explanation (AGENT)
    under the SAME meta. Counting both would double every escalation."""
    landing_cls = _landing()
    loop = _FakeLoop()
    await landing_cls(loop).land_blocked(reason="r", guidance="g")

    # Feed this landing's OWN emitted events back as the durable log.
    loop2 = _FakeLoop(with_seqs(list(loop.emitted)))
    await landing_cls(loop2).land_blocked(reason="r", guidance="g")

    body = loop2.env_bodies()[0]
    assert "2 times in this run" in body, (
        "one prior landing is the SECOND fire, not the third — the AGENT-sourced "
        "explanation carries the same landing meta and must not be counted"
    )


# ---------------------------------------------------------------------------
# F53 — the cross-seam leak.
#
# `governed_contract_refusal` is an F51 surface and IS repetition-aware. Its HALT
# path emits nothing: it hands `_land_blocked` a guidance string that
# `_blocked_prompt` renders. The repaired surface's own text therefore reached the
# agent byte-identically twice by riding an unrepaired seam.
#
# The repair is at the EMITTING seam, which is where F51's reasoning puts it. What
# changes is WHAT the emitting seam escalates on: the identity of the text it was
# handed, not only its own reason.
# ---------------------------------------------------------------------------


_HALT_GUIDANCE = (
    "Target verification repeated the same governed failure with no "
    "productive authority change. there is no handoff for the current target at all"
)


async def test_handed_guidance_cannot_repeat_byte_identically():
    """F53's measured instance. The same authored guidance handed to the landing
    seam twice must not render the same body twice."""
    landing_cls = _landing()

    loop = _FakeLoop()
    await landing_cls(loop).land_blocked(reason="host:fp1", guidance=_HALT_GUIDANCE)
    first = loop.env_bodies()[0]

    loop2 = _FakeLoop(with_seqs([user_msg("go"), _prior_landing("host:fp1", _HALT_GUIDANCE)]))
    await landing_cls(loop2).land_blocked(reason="host:fp1", guidance=_HALT_GUIDANCE)
    second = loop2.env_bodies()[0]

    assert first != second
    assert _HALT_GUIDANCE in first and _HALT_GUIDANCE in second, (
        "the authored fact survives — repetition-awareness is ADDED at the emitting "
        "seam, never traded against the upstream surface's content"
    )
    assert "this same blocked context" in second, (
        "the escalation must name the HANDED text, which is the specific fact F53 "
        "says the agent was never told"
    )


async def test_handed_guidance_escalates_even_under_a_DIFFERENT_reason():
    """The sharp edge of F53. An upstream surface can hand the same text while the
    landing reason differs (a different failure fingerprint). Keying only on the
    reason would miss exactly the leak F53 names."""
    landing_cls = _landing()

    loop = _FakeLoop(with_seqs([user_msg("go"), _prior_landing("host:fp_OTHER", _HALT_GUIDANCE)]))
    await landing_cls(loop).land_blocked(reason="host:fp_THIS", guidance=_HALT_GUIDANCE)
    body = loop.env_bodies()[0]

    assert "this same blocked context" in body, (
        "the handed text repeated even though the reason did not; the emitting seam "
        "must escalate on what it was HANDED"
    )


async def test_a_different_handed_text_does_not_inherit_the_escalation():
    """The guidance count is per-TEXT. Different authored guidance is a different
    thing to say and starts its own count."""
    landing_cls = _landing()

    loop = _FakeLoop(with_seqs([user_msg("go"), _prior_landing("host:fp1", "something else")]))
    await landing_cls(loop).land_blocked(reason="host:fp1", guidance=_HALT_GUIDANCE)
    body = loop.env_bodies()[0]

    assert "this same blocked context" not in body
    # The reason DID repeat, so the reason-scoped escalation is the correct one here.
    assert "did not clear it" in body


async def test_guidance_fingerprint_is_recorded_durably_on_the_landing():
    """The escalation must survive a restart, so the identity it keys on lives in
    the durable event meta rather than in loop state."""
    from disco.core.loop.valve_landing import _GUIDANCE_FP_META_KEY, _guidance_fingerprint

    landing_cls = _landing()
    loop = _FakeLoop()
    await landing_cls(loop).land_blocked(reason="r", guidance=_HALT_GUIDANCE)

    env = [
        e
        for e in loop.emitted
        if isinstance(e, MessageEvent) and e.source is EventSource.ENVIRONMENT
    ]
    assert env[0].meta.get(_GUIDANCE_FP_META_KEY) == _guidance_fingerprint(_HALT_GUIDANCE)


# ---------------------------------------------------------------------------
# The counters themselves — ledger-derived, never an instance counter
# ---------------------------------------------------------------------------


async def test_landing_repeats_reads_the_durable_log():
    from disco.core.loop.valve_landing import _guidance_fingerprint

    landing_cls = _landing()
    fp = _guidance_fingerprint(_HALT_GUIDANCE)

    empty = landing_cls(_FakeLoop())
    assert await empty._landing_repeats(reason="r", guidance_fp=fp) == (1, 1)

    log = with_seqs(
        [
            _prior_landing("r", _HALT_GUIDANCE),
            _prior_landing("r", _HALT_GUIDANCE),
            _prior_landing("other", "other text"),
        ]
    )
    loaded = landing_cls(_FakeLoop(log))
    assert await loaded._landing_repeats(reason="r", guidance_fp=fp) == (3, 3)


def test_guidance_fingerprint_ignores_surrounding_whitespace_and_empties():
    from disco.core.loop.valve_landing import _guidance_fingerprint

    assert _guidance_fingerprint("") == ""
    assert _guidance_fingerprint("   ") == ""
    assert _guidance_fingerprint(" abc ") == _guidance_fingerprint("abc")
    assert _guidance_fingerprint("abc") == hashlib.sha256(b"abc").hexdigest()[:24]


async def test_notice_repeat_counts_the_blocking_label_off_the_log():
    from disco.core.loop.finish.content_gate_parts.notices import _notice_repeat

    assert await _notice_repeat(_FakeLoop(), "plan_verifier_failed") == 1
    loop = _FakeLoop(
        with_seqs([_prior_notice("plan_verifier_failed"), _prior_notice("plan_verifier_failed")])
    )
    assert await _notice_repeat(loop, "plan_verifier_failed") == 3
    assert await _notice_repeat(_FakeLoop(with_seqs([_prior_notice("dod_unmet")])), "plan_verifier_failed") == 1
