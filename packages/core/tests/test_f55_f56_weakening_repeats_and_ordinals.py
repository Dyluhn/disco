"""F55 and F56 — the fifth confirmed multi-fire surface, and the ordinal it renders.

Regression evidence for the 2026-08-07l repair of two defects that the 07j
boundary's own fresh live corpus exposed and that no earlier test could see.

# F55 — `reject_plan_weakening` renders `weakening_guidance` verbatim

The 07j corpus at `9740ccd5` contains exactly two byte-identical repeat groups,
neither on any of the four surfaces that boundary repaired:

  * `d2-r1/p4_ff_react_continue`          seqs 109, 115, 121  — **3x**
  * `d3-r1/p4_ff_python_cancel_recovery`  seqs 31, 34         —   2x

Both are the plan-revision weakening refusal. `weakening_guidance` is a pure
function of the predicate diff (`plan_revisions.py:289`) with no access to run
history; `reject_plan_weakening` (line 358) wrapped it in a `<system-reminder>`
and emitted it unchanged. That is F53's exact shape — an authoring helper with
no log access handing text to an emitting seam that renders it without
repetition awareness — at a different pair of seams.

The repair is the one this campaign has now applied four times: a DURABLE
ledger-derived count read at the EMITTING seam, escalating on repeat, never a
window that resets. `reject_plan_weakening` already emitted
`meta={"blocking": PLAN_WEAKENING_BLOCKER}`, so the label the count keys on was
already durable in the log before this repair — nothing new is stored.

# F56 — the escalation clause renders "the 2th time"

Found by the 07k review reading the two raw bodies behind 07j's one CONFIRMED
target rather than trusting the instrument's `distinct=2` verdict:

    ... This is the 2th time the plan verification conditions have failed in
    this run; the previous 1 did not clear it.

`_again()` (`notices.py:149`) and `_serve_handoff_guidance`
(`turn_control_support.py:162`) both used a naive `{n}th`, correct only for
4-20. It is agent-facing text and it shipped live in 07j's sealed corpus.

**Why no existing test caught it.** The clause appears ONLY on the repeat path,
so every first-firing test renders `""` instead; and every byte-identity check
compares firings to each other rather than to English, so two correctly-differing
bodies both containing "2th" pass. The GROUNDED FEEDBACK owner block binds this
seam, and a campaign that spent four boundaries arguing a repeated message must
READ differently cannot ship the escalation clause ungrammatical.

# Import discipline

New symbols are imported INSIDE the test bodies, never at module level. They do
not exist at `b8aa2554`, and a module-level import turns the whole file into one
collection error when it is run against the old bytes — a much weaker red than
each body failing on the property it actually asserts. (Convention inherited
from `test_f47_answered_question_notice.py`.)
"""

from __future__ import annotations

from disco.core import (
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
)
from disco.core.events import PlanStep
from disco.core.loop.control import Disp

# ---------------------------------------------------------------------------
# Fakes — the minimum surface each seam actually touches
# ---------------------------------------------------------------------------


class _FakePlanner:
    """`reject_plan_weakening`'s only planner call."""

    def __init__(self) -> None:
        self.discarded: list[int] = []

    def discard_plan_predicates(self, revision: int) -> None:
        self.discarded.append(revision)


class _FakeLoop:
    """Captures emitted events instead of persisting them."""

    def __init__(self, events: list[Event] | None = None) -> None:
        self.emitted: list[Event] = []
        self._log: list[Event] = events or []
        self._planner = _FakePlanner()
        self._identical_plan_revisions = 0

    async def _emit(self, event: Event) -> Event:
        self.emitted.append(event)
        return event

    async def _events(self) -> list[Event]:
        return self._log

    def env_bodies(self) -> list[str]:
        """Only the ENVIRONMENT half — the text the seam itself renders."""
        return [
            e.message.content
            for e in self.emitted
            if isinstance(e, MessageEvent)
            and e.message is not None
            and e.source is EventSource.ENVIRONMENT
        ]


class _FakePredicate:
    def __init__(self, label: str) -> None:
        self.label = label
        self.path = label

    def __repr__(self) -> str:
        return self.label


class _FakeDiff:
    """The shape `weakening_guidance` reads: dropped + invalid_renames."""

    def __init__(
        self,
        dropped: list[object] | None = None,
        invalid_renames: list[object] | None = None,
    ) -> None:
        self.dropped = dropped or []
        self.invalid_renames = invalid_renames or []


def _plan(revision: int = 3) -> PlanEvent:
    return PlanEvent(
        summary="ship it",
        steps=[PlanStep(title="do the thing")],
        revision=revision,
    )


def _prior_weakening_refusal(blocking: str) -> MessageEvent:
    """A durable prior firing, recognised by the `blocking` label alone.

    This is exactly the meta `reject_plan_weakening` already emitted before the
    repair, which is why the count needs no new stored state.
    """
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="<system-reminder>prior</system-reminder>"),
        meta={"blocking": blocking},
    )


# ---------------------------------------------------------------------------
# F56 — the ordinal
# `loop/finish/content_gate_parts/notices.py::_again`
# `loop/turn_control_support.py::_serve_handoff_guidance`
# ---------------------------------------------------------------------------


def test_ordinal_renders_english_not_naive_th():
    """The whole table, including the 11/12/13 trap that a naive `%10` rule gets
    wrong in the opposite direction from a naive `th`."""
    from disco.core.loop.ordinals import ordinal

    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"
    assert ordinal(20) == "20th"
    assert ordinal(21) == "21st"
    assert ordinal(22) == "22nd"
    assert ordinal(23) == "23rd"
    assert ordinal(24) == "24th"
    assert ordinal(101) == "101st"
    assert ordinal(111) == "111th"
    assert ordinal(112) == "112th"
    assert ordinal(113) == "113th"


def test_again_clause_is_grammatical_at_the_observed_multiplicity():
    """The exact body shipped to a live agent in 07j's sealed corpus
    (`e2-r1` seq 142) said "the 2th time"."""
    # The `what` clause is DERIVED from the production symbol that owns it (F61,
    # repaired 2026-08-07t). Until then it was a call argument at both ends —
    # production passed the literal, this test retyped it — so nothing bound the
    # two and the Attestation-Binding gate was silent about it BY CONSTRUCTION.
    from disco.core.loop.finish.content_gate_parts.notices import (
        _PLAN_VERIFICATION_CONDITIONS_FAILED,
        _again,
    )

    clause = _again(2, _PLAN_VERIFICATION_CONDITIONS_FAILED)
    assert "2th" not in clause
    assert "the 2nd time" in clause
    # The rest of the sentence is unchanged — this repair is the prose only.
    assert "the previous 1 did not clear it." in clause


def test_again_clause_is_grammatical_at_three_and_twentyone():
    from disco.core.loop.finish.content_gate_parts.notices import _again

    assert "the 3rd time" in _again(3, "x")
    assert "3th" not in _again(3, "x")
    assert "the 21st time" in _again(21, "x")
    assert "21th" not in _again(21, "x")
    # 11-13 keep "th" — the trap in the other direction.
    assert "the 11th time" in _again(11, "x")


def test_again_first_firing_still_renders_nothing():
    """The first-firing body must stay byte-identical to every body written
    before this repair, so pre-2026-08-07l logs remain recognisable by content."""
    from disco.core.loop.finish.content_gate_parts.notices import _again

    assert _again(1, "x") == ""
    assert _again(0, "x") == ""


def test_serve_handoff_guidance_ordinal_is_grammatical():
    from disco.core.loop.turn_control_support import _serve_handoff_guidance

    body = _serve_handoff_guidance(2, [])
    assert "2th" not in body
    assert "this is the 2nd handoff of this run" in body


def test_serve_handoff_guidance_first_firing_is_unchanged():
    """The `repeats <= 1` branch carries no ordinal at all and must not move."""
    from disco.core.loop.turn_control_support import (
        _SERVE_HANDOFF_OPENING,
        _serve_handoff_guidance,
    )

    # ATTESTATION-BINDING INVARIANT (F58): the opening sentence is DERIVED from
    # the constant the first-firing branch composes, not restated here. A
    # restatement would keep this test green after a reword while the product
    # emitted something else entirely.
    body = _serve_handoff_guidance(1, [])
    assert _SERVE_HANDOFF_OPENING in body
    assert "handoff of this run" not in body


# ---------------------------------------------------------------------------
# F55 — `loop/plan_revisions.py::reject_plan_weakening`
# d2-r1/p4_ff_react_continue seqs 109/115/121 (3x); d3-r1/... seqs 31/34 (2x).
# ---------------------------------------------------------------------------


async def test_weakening_refusal_does_not_repeat_itself():
    """The corpus defect exactly: the same diff refused twice in one run rendered
    the same bytes, because `weakening_guidance` is a pure function of the diff
    and nothing else reached the emitter."""
    from disco.core.loop.plan_revisions import PLAN_WEAKENING_BLOCKER, reject_plan_weakening

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])

    first = _FakeLoop()
    assert await reject_plan_weakening(first, _plan(), diff) is Disp.CONTINUE

    second = _FakeLoop(events=[_prior_weakening_refusal(PLAN_WEAKENING_BLOCKER)])
    assert await reject_plan_weakening(second, _plan(), diff) is Disp.CONTINUE

    assert first.env_bodies()[0] != second.env_bodies()[0]


async def test_weakening_refusal_first_firing_is_byte_identical_to_the_legacy_text():
    """First firings stay recognisable by content, so the 07j corpus's own
    seqs 109/31 still match the pre-repair literal."""
    from disco.core.loop.plan_revisions import reject_plan_weakening, weakening_guidance

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])
    loop = _FakeLoop()
    await reject_plan_weakening(loop, _plan(), diff)

    assert loop.env_bodies()[0] == (
        f"<system-reminder>\n{weakening_guidance(diff)}\n</system-reminder>"
    )


async def test_weakening_refusal_escalates_at_the_observed_three_times():
    """The corpus's worst group was 3x. The third body must differ from BOTH
    earlier ones, not merely from the first — a count that saturates at 2 would
    have passed a two-body test and still repeated at the observed multiplicity."""
    from disco.core.loop.plan_revisions import PLAN_WEAKENING_BLOCKER, reject_plan_weakening

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])
    bodies: list[str] = []
    log: list[Event] = []
    for _ in range(3):
        loop = _FakeLoop(events=list(log))
        await reject_plan_weakening(loop, _plan(), diff)
        bodies.append(loop.env_bodies()[0])
        log.append(_prior_weakening_refusal(PLAN_WEAKENING_BLOCKER))

    assert len(set(bodies)) == 3, "three firings must render three distinct bodies"
    assert "2nd" in bodies[1]
    assert "3rd" in bodies[2]


async def test_weakening_refusal_count_reads_the_durable_log():
    """Ledger-derived, not an instance counter: a fresh loop object whose LOG
    already carries two refusals renders the third body, because a restart or a
    condensation must not reset the escalation."""
    from disco.core.loop.plan_revisions import PLAN_WEAKENING_BLOCKER, reject_plan_weakening

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])
    loop = _FakeLoop(
        events=[
            _prior_weakening_refusal(PLAN_WEAKENING_BLOCKER),
            _prior_weakening_refusal(PLAN_WEAKENING_BLOCKER),
        ]
    )
    await reject_plan_weakening(loop, _plan(), diff)
    assert "3rd" in loop.env_bodies()[0]


async def test_weakening_refusal_count_is_keyed_on_its_own_blocking_label():
    """A different fail-closed surface's durable label must not inflate this
    count — the failure mode that would make every surface escalate together."""
    from disco.core.loop.plan_revisions import reject_plan_weakening

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])
    loop = _FakeLoop(
        events=[
            _prior_weakening_refusal("user_literal_missing"),
            _prior_weakening_refusal("plan_verification_evidence_invalid"),
        ]
    )
    await reject_plan_weakening(loop, _plan(), diff)

    body = loop.env_bodies()[0]
    assert "2nd" not in body and "3rd" not in body


async def test_weakening_refusal_still_discards_and_still_continues():
    """The repair is additive at the rendering seam: the durable side effects
    (predicate discard, RUNNING status, recoverable CONTINUE) are unchanged."""
    from disco.core.events import StatusEvent
    from disco.core.loop.plan_revisions import (
        PLAN_WEAKENING_BLOCKER,
        PLAN_WEAKENING_DETAIL,
        reject_plan_weakening,
    )

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])
    loop = _FakeLoop(events=[_prior_weakening_refusal(PLAN_WEAKENING_BLOCKER)])
    assert await reject_plan_weakening(loop, _plan(revision=7), diff) is Disp.CONTINUE

    assert loop._planner.discarded == [7]
    statuses = [e for e in loop.emitted if isinstance(e, StatusEvent)]
    assert [s.detail for s in statuses] == [PLAN_WEAKENING_DETAIL]
    blocking = [
        e.meta.get("blocking") for e in loop.emitted if isinstance(e, MessageEvent)
    ]
    assert blocking == [PLAN_WEAKENING_BLOCKER]


async def test_weakening_refusal_escalation_names_the_cost_not_just_the_count():
    """The GROUNDED FEEDBACK projection rule: the escalation names ONE next move.
    A bare "you have seen this N times" is the shape the campaign rejected."""
    from disco.core.loop.plan_revisions import PLAN_WEAKENING_BLOCKER, reject_plan_weakening

    diff = _FakeDiff(dropped=[_FakePredicate("file_exists:index.html")])
    loop = _FakeLoop(events=[_prior_weakening_refusal(PLAN_WEAKENING_BLOCKER)])
    await reject_plan_weakening(loop, _plan(), diff)

    body = loop.env_bodies()[0]
    # Still carries the derived diff — the escalation is additive, not a swap.
    assert "file_exists:index.html" in body
    # And names what re-proposing costs.
    assert "renamed_from" in body
